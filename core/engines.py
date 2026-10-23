"""各以图搜图引擎的适配器。

刻意不依赖 PicImageSearch：服务器上只需要 requests 和 Pillow，解析逻辑自己维护，
升级或改版时更好定位。所有引擎都用「直接上传图片字节」的方式，不依赖 QQ 图链
是否还在有效期内。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from requests import ReadTimeout

from .extract import (
    clean_url,
    extract_identity,
    platform_of,
    promote_sotwe,
    relevance_rank,
)
from .httpclient import (
    HttpError,
    download_image,
    is_auth_failure,
    json_or_raise,
    make_session,
)
from .keypool import KeyPool

SERP_ENDPOINT = "https://serpapi.com/search.json"
SERP_IMAGE_ENDPOINT = "https://serpapi.com/image"
SERP_UPLOAD_LIMIT = 500 * 1024
SAUCENAO_ENDPOINT = "https://saucenao.com/search.php"
SIGHTENGINE_ENDPOINT = "https://api.sightengine.com/1.0/check.json"

YANDEX_HOSTS = ("https://yandex.ru", "https://yandex.com.tr", "https://yandex.com")
BAIDU_UPLOAD = "https://graph.baidu.com/upload"


class EngineError(RuntimeError):
    """引擎调用失败，消息直接进 errors 字段给用户看。"""


@dataclass
class Candidate:
    """一条搜索结果。"""

    engine: str
    url: str = ""
    title: str = ""
    source: str = ""
    image_url: str = ""
    similarity: float | None = None
    distance: int | None = None
    status: str = "unverified"  # match / suspect / unverified / mismatch
    note: str = ""
    platform: str = ""
    name: str = ""
    handle: str = ""

    def finalize(self) -> "Candidate":
        """补全平台与身份字段。"""
        self.url = clean_url(self.url)
        self.title = (self.title or "").strip()[:150]
        self.source = (self.source or urlparse(self.url).netloc or "").strip()
        self.platform = platform_of(self.url, self.source, self.title)
        name, handle = extract_identity(self.url, self.title)
        self.name = self.name or name
        self.handle = self.handle or handle
        return self

    @property
    def rank(self) -> int:
        return relevance_rank(self.url, self.source)


def _timeout(seconds: float) -> tuple[float, float]:
    return (min(10.0, seconds), seconds)


# ---------------------------------------------------------------------------
# Google Lens（SerpAPI）
# ---------------------------------------------------------------------------


class LensEngine:
    """Google Lens：先上传图片拿 image_id，再用 image_id 检索。

    SerpAPI 的 visual_matches 里同时有 thumbnail 和 image 两个字段：
    thumbnail 是 encrypted-tbn*.gstatic.com（国内不可达），image 才是推文原图，
    所以验证与配图都用 image。
    """

    name = "lens"

    def __init__(self, pool: KeyPool, proxy: str = "", timeout: float = 40.0) -> None:
        self.pool = pool
        self.proxy = proxy
        self.timeout = timeout

    def search(self, raw: bytes, image_url: str = "", stream_id: str = "") -> list[Candidate]:
        key = self.pool.acquire()
        if not key:
            raise EngineError("没有可用的 SerpAPI key（未配置或全部冷却）")

        session = make_session(self.proxy)
        try:
            image_id = self._upload(session, key, raw)
            params = {"engine": "google_lens", "api_key": key, "type": "all"}
            if image_id:
                params["image_id"] = image_id
            elif image_url:
                params["url"] = image_url
            else:
                raise EngineError("图片上传失败，且没有可用的图片链接")

            resp = session.get(SERP_ENDPOINT, params=params, timeout=_timeout(self.timeout))
            if is_auth_failure(resp.status_code):
                self.pool.mark_failure(key)
                raise EngineError(f"SerpAPI 返回 HTTP {resp.status_code}")
            data = json_or_raise(resp, "SerpAPI")
            if data.get("error"):
                message = str(data["error"])[:160]
                if any(token in message.lower() for token in ("key", "quota", "plan", "limit")):
                    self.pool.mark_failure(key)
                raise EngineError(message)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.pool.mark_failure(key, cooldown_seconds=60)
            raise EngineError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        finally:
            session.close()

        self.pool.mark_success(key)
        items: list[Candidate] = []
        for group in ("visual_matches", "exact_matches"):
            for match in data.get(group) or []:
                link = clean_url(match.get("link") or "")
                if not link:
                    continue
                title = str(match.get("title") or "")
                image = str(match.get("image") or "")
                link, title = promote_sotwe(link, title)
                items.append(
                    Candidate(
                        engine=self.name,
                        url=link,
                        title=title,
                        source=str(match.get("source") or urlparse(link).netloc),
                        image_url=image,
                        note="精确匹配" if group == "exact_matches" else "",
                    ).finalize()
                )
        return items

    def _upload(self, session, key: str, raw: bytes) -> str:
        if len(raw) > SERP_UPLOAD_LIMIT:
            return ""
        try:
            resp = session.post(
                SERP_IMAGE_ENDPOINT,
                data={"api_key": key},
                files={"image": ("upload.jpg", raw, "image/jpeg")},
                timeout=_timeout(self.timeout),
            )
            if resp.status_code >= 400:
                return ""
            payload = resp.json()
            return str(payload.get("image_id") or "")
        except Exception:  # noqa: BLE001 - 上传失败就回退到按链接搜索
            return ""


# ---------------------------------------------------------------------------
# Yandex
# ---------------------------------------------------------------------------


class YandexEngine:
    """Yandex 以图搜图：POST 上传图片，结果页里内嵌 JSON。"""

    name = "yandex"

    def __init__(self, proxy: str = "", timeout: float = 30.0) -> None:
        self.proxy = proxy
        self.timeout = timeout

    def search(self, raw: bytes, image_url: str = "", stream_id: str = "") -> list[Candidate]:
        last_error = ""
        for host in YANDEX_HOSTS:
            for attempt in (1, 2):
                session = make_session(self.proxy)
                try:
                    resp = session.post(
                        f"{host}/images/search",
                        params={"rpt": "imageview", "cbir_page": "sites"},
                        data={"prg": "1"},
                        files={"upfile": ("blob", raw, "image/jpeg")},
                        timeout=_timeout(self.timeout),
                    )
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        break
                    sites = parse_yandex_sites(resp.text)
                    if sites is None:
                        last_error = "结果页结构变化，未找到 data-state"
                        break
                    return [to_yandex_candidate(site, self.name) for site in sites]
                except ReadTimeout as exc:
                    # 跨境链路偶发抖动，重试一次通常能过；超时参数不放大，避免拖长整条搜索
                    last_error = f"ReadTimeout: {str(exc)[:120]}"
                    if attempt == 1:
                        continue
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                    break
                finally:
                    session.close()
        raise EngineError(last_error or "Yandex 无响应")


def parse_yandex_sites(page: str) -> list[dict] | None:
    """从 Yandex 结果页提取 cbirSites.sites。"""
    for pattern in (
        r'<div[^>]*id="ImagesApp-[^"]*"[^>]*data-state="(.*?)"',
        r'<div[^>]*class="[^"]*Root[^"]*"[^>]*data-state="(.*?)"',
    ):
        match = re.search(pattern, page, re.S)
        if not match:
            continue
        try:
            state = json.loads(html_lib.unescape(match.group(1)))
        except Exception:  # noqa: BLE001
            continue
        sites = ((state.get("initialState") or {}).get("cbirSites") or {}).get("sites")
        if isinstance(sites, list):
            return sites
    return None


def to_yandex_candidate(site: dict, engine: str) -> Candidate:
    url = clean_url(str(site.get("url") or ""))
    title = str(site.get("title") or "")
    thumb = str((site.get("thumb") or {}).get("url") or "")
    if thumb.startswith("//"):
        thumb = "https:" + thumb
    url, title = promote_sotwe(url, title)
    return Candidate(
        engine=engine,
        url=url,
        title=title,
        source=str(site.get("domain") or ""),
        image_url=thumb,
        note="缩略图比对" if thumb else "",
    ).finalize()


# ---------------------------------------------------------------------------
# 百度
# ---------------------------------------------------------------------------


class BaiduEngine:
    """百度以图搜图：上传拿结果页地址，再解析页面里的 window.cardData。"""

    name = "baidu"

    def __init__(self, proxy: str = "", timeout: float = 30.0) -> None:
        self.proxy = proxy
        self.timeout = timeout

    def search(self, raw: bytes, image_url: str = "", stream_id: str = "") -> list[Candidate]:
        session = make_session(self.proxy, headers={"Acs-Token": ""})
        try:
            upload = session.post(
                BAIDU_UPLOAD,
                data={"from": "pc"},
                files={"image": ("image.jpg", raw, "image/jpeg")},
                timeout=_timeout(self.timeout),
            )
            payload = json_or_raise(upload, "百度")
            data_url = ((payload.get("data") or {}).get("url") or "").strip()
            if not data_url:
                raise EngineError("百度未返回结果页地址")

            page = session.get(data_url, timeout=_timeout(self.timeout))
            if page.status_code != 200:
                raise EngineError(f"百度结果页返回 HTTP {page.status_code}")

            candidates: list[Candidate] = []
            detail_url = ""
            for card in extract_card_data(page.text):
                card_name = str(card.get("cardName") or "")
                template = card.get("tplData") or {}
                if card_name == "noresult":
                    return []
                if card_name == "same":
                    for item in template.get("list") or []:
                        candidate = to_baidu_candidate(item, self.name)
                        if candidate:
                            candidate.note = "精确匹配"
                            candidates.append(candidate)
                elif card_name == "simipic":
                    detail_url = str(template.get("firstUrl") or "")

            if detail_url:
                detail = session.get(detail_url, timeout=_timeout(self.timeout))
                if detail.status_code == 200:
                    try:
                        detail_data = detail.json()
                    except Exception:  # noqa: BLE001
                        detail_data = {}
                    for item in ((detail_data.get("data") or {}).get("list") or []):
                        candidate = to_baidu_candidate(item, self.name)
                        if candidate:
                            candidates.append(candidate)
            return candidates
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        finally:
            session.close()


def extract_card_data(page: str) -> list[dict]:
    """从百度结果页里取出 window.cardData 数组。"""
    index = page.find("window.cardData")
    if index < 0:
        return []
    start = page.find("[", index)
    if start < 0:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(page[start:])
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def to_baidu_candidate(item: dict, engine: str) -> Candidate | None:
    url = clean_url(str(item.get("url") or item.get("fromUrl") or ""))
    title = item.get("title")
    if isinstance(title, list):
        title = title[0] if title else ""
    title = str(title or "")
    image = str(item.get("image_src") or item.get("thumbUrl") or "")
    if not url:
        return None
    return Candidate(
        engine=engine,
        url=url,
        title=title,
        image_url=image,
    ).finalize()


# ---------------------------------------------------------------------------
# SauceNAO
# ---------------------------------------------------------------------------


def group_listed(groups: list[str], stream_id: str, group_id: str) -> bool:
    """名单里可以写群号，也可以写会话 ID，两种都认（`g` 前缀自动忽略）。"""

    def norm(value: str) -> str:
        return str(value).strip().lstrip("g")

    wanted = {norm(item) for item in groups if str(item).strip()}
    if not wanted:
        return False
    candidates = [group_id, stream_id, stream_id.lstrip("g")]
    return any(value and norm(value) in wanted for value in candidates)


def resolve_saucenao_db(
    *,
    db_full: str,
    db_strict: str,
    policy_enabled: bool,
    invert: bool,
    groups: list[str],
    stream_id: str,
    group_id: str,
) -> str:
    """按群决定 SauceNAO 用哪个库。

    关闭名单策略时一律走全量；打开后默认「名单上的群全量、其他群收紧」，
    invert 反转为「名单上的群收紧、其他群全量」。名单为空时视为所有人
    都在名单外，即全部收紧（invert 时全部全量）。
    """
    if not policy_enabled:
        return db_full
    listed = group_listed(groups, stream_id, group_id)
    if invert:
        return db_strict if listed else db_full
    return db_full if listed else db_strict


class SauceNaoEngine:
    """SauceNAO 自带相似度，直接采用，不再做指纹比对。"""

    name = "saucenao"

    def __init__(
        self,
        pool: KeyPool,
        proxy: str = "",
        timeout: float = 30.0,
        min_similarity: float = 60.0,
        db_full: str = "999",
        db_strict: str = "5,6,21,24",
        policy_enabled: bool = False,
        invert: bool = False,
        groups: list[str] | None = None,
        group_of: Callable[[str], str] | None = None,
    ) -> None:
        self.pool = pool
        self.proxy = proxy
        self.timeout = timeout
        self.min_similarity = min_similarity
        self.db_full = db_full
        self.db_strict = db_strict
        self.policy_enabled = policy_enabled
        self.invert = invert
        self.groups = list(groups or [])
        self.group_of = group_of

    def _db_for(self, stream_id: str) -> str:
        group_id = ""
        if self.group_of is not None and stream_id:
            try:
                group_id = str(self.group_of(stream_id) or "")
            except Exception:  # noqa: BLE001 - 查不到就按名单外处理
                group_id = ""
        return resolve_saucenao_db(
            db_full=self.db_full,
            db_strict=self.db_strict,
            policy_enabled=self.policy_enabled,
            invert=self.invert,
            groups=self.groups,
            stream_id=stream_id,
            group_id=group_id,
        )

    def search(self, raw: bytes, image_url: str = "", stream_id: str = "") -> list[Candidate]:
        key = self.pool.acquire()
        if key is None:
            if self.pool.keys:
                raise EngineError("SauceNAO key 全部冷却中")
            raise EngineError("未配置 SauceNAO API key（匿名接口已不开放）")

        data = {"output_type": "2", "db": self._db_for(stream_id), "numres": "8", "minsim": "30"}
        if key:
            data["api_key"] = key

        session = make_session(self.proxy)
        try:
            resp = session.post(
                SAUCENAO_ENDPOINT,
                data=data,
                files={"file": ("image.jpg", raw, "image/jpeg")},
                timeout=_timeout(self.timeout),
            )
            if is_auth_failure(resp.status_code):
                if key:
                    self.pool.mark_failure(key)
                raise EngineError(f"SauceNAO 返回 HTTP {resp.status_code}")
            payload = json_or_raise(resp, "SauceNAO")
            header = payload.get("header") or {}
            if header.get("status") not in (None, 0, "0"):
                message = str(header.get("message") or "未知错误")[:160]
                if key:
                    self.pool.mark_failure(key)
                raise EngineError(message)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        finally:
            session.close()

        if key:
            self.pool.mark_success(key)

        candidates: list[Candidate] = []
        for item in payload.get("results") or []:
            header_item = item.get("header") or {}
            detail = item.get("data") or {}
            try:
                similarity = round(float(header_item.get("similarity") or 0), 1)
            except (TypeError, ValueError):
                similarity = 0.0
            urls = detail.get("ext_urls") or []
            url = clean_url(str(urls[0])) if urls else ""
            if not url:
                continue
            title = str(detail.get("title") or detail.get("source") or "")
            thumb = str(header_item.get("thumbnail") or "")
            handle = str(detail.get("twitter_user_handle") or "")
            name = str(detail.get("author_name") or detail.get("member_name") or detail.get("creator") or "")
            candidate = Candidate(
                engine=self.name,
                url=url,
                title=title,
                image_url=thumb,
                similarity=similarity,
                status="match" if similarity >= self.min_similarity else "suspect",
                name=name,
                handle=("@" + handle) if handle and not handle.startswith("@") else handle,
            ).finalize()
            if candidate.status == "match":
                candidate.distance = None
                candidate.note = f"相似度 {similarity:g}%"
            else:
                candidate.note = f"相似度偏低 {similarity:g}%"
            candidates.append(candidate)
        return candidates


# ---------------------------------------------------------------------------
# AI 生成检测（Sightengine）
# ---------------------------------------------------------------------------


def detect_ai(
    raw: bytes,
    pool: KeyPool,
    proxy: str = "",
    timeout: float = 25.0,
) -> dict:
    """返回 {"ok": bool, "score": float|None, "error": str}。

    免费档单次 genai 调用计 5 个 op，因此只在低置信分支调用。
    """
    key = pool.acquire()
    if not key:
        return {"ok": False, "score": None, "error": "未配置 Sightengine 密钥或全部冷却中"}
    api_user, _, api_secret = key.partition(":")

    session = make_session(proxy)
    try:
        resp = session.post(
            SIGHTENGINE_ENDPOINT,
            data={"models": "genai", "api_user": api_user, "api_secret": api_secret},
            files={"media": ("upload.jpg", raw, "image/jpeg")},
            timeout=_timeout(timeout),
        )
        if is_auth_failure(resp.status_code):
            pool.mark_failure(key)
            return {"ok": False, "score": None, "error": f"HTTP {resp.status_code}"}
        payload = json_or_raise(resp, "Sightengine")
        if payload.get("status") != "success":
            error = payload.get("error") or {}
            pool.mark_failure(key)
            return {
                "ok": False,
                "score": None,
                "error": f"{error.get('message', '未知错误')} (code {error.get('code')})",
            }
    except Exception as exc:  # noqa: BLE001
        pool.mark_failure(key, cooldown_seconds=120)
        return {"ok": False, "score": None, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    finally:
        session.close()

    pool.mark_success(key)
    score = (payload.get("type") or {}).get("ai_generated")
    return {"ok": True, "score": score, "error": ""}


# ---------------------------------------------------------------------------
# 候选图下载与指纹比对
# ---------------------------------------------------------------------------


def verify_candidate(
    candidate: Candidate,
    target_hash: int | None,
    threshold: int,
    proxy: str = "",
    timeout: float = 12.0,
) -> Candidate:
    """下载候选图并比对指纹，就地更新 status / distance。

    - 距离 ≤ threshold：match
    - threshold < 距离 ≤ threshold + 6：suspect（可能是裁剪或拼图）
    - 其余：mismatch
    - 下载失败或没有图片地址：保持 unverified
    """
    if candidate.status in {"match", "suspect"} and candidate.similarity is not None:
        return candidate
    if target_hash is None or not candidate.image_url:
        candidate.status = "unverified"
        return candidate

    from .hashing import dhash, hamming

    try:
        raw = download_image(candidate.image_url, proxy, timeout=timeout)
    except HttpError as exc:
        candidate.status = "unverified"
        candidate.note = f"候选图下载失败：{str(exc)[:60]}"
        return candidate

    candidate_hash = dhash(raw)
    if candidate_hash is None:
        candidate.status = "unverified"
        candidate.note = "候选图无法解码"
        return candidate

    distance = hamming(target_hash, candidate_hash)
    candidate.distance = distance
    if distance <= threshold:
        candidate.status = "match"
        candidate.note = f"指纹一致（距离 {distance}）"
    elif distance <= threshold + 6:
        candidate.status = "suspect"
        candidate.note = f"疑似同图（距离 {distance}，可能被裁剪）"
    else:
        candidate.status = "mismatch"
        candidate.note = f"指纹不符（距离 {distance}）"
    return candidate
