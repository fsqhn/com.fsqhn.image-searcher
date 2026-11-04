"""结果文案拼装：用户侧走合并转发，模型侧只给纯文字。"""

from __future__ import annotations

from .engines import Candidate
from .tasks import SearchOutcome, SearchTask

AI_FLAG_THRESHOLD = 0.75


def _ai_line(ai: dict | None) -> str:
    if not ai:
        return "🤖 AI 检测：未执行"
    if not ai.get("ok"):
        return f"🤖 AI 检测失败：{ai.get('error') or '未知原因'}"
    score = ai.get("score")
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "🤖 AI 检测：未返回有效分值"
    percent = value * 100
    if value >= AI_FLAG_THRESHOLD:
        return f"🤖 AI 生成概率：{percent:.0f}%（疑似 AI 生成）"
    return f"🤖 AI 生成概率：{percent:.1f}%（大概率真人拍摄）"


def _route_line(outcome: SearchOutcome) -> str:
    if outcome.route == "drawing":
        reason = f"，{outcome.route_reason}" if outcome.route_reason else ""
        return f"🖼 匹配路线：插画{reason}"
    return "🖼 匹配路线：照片"


def _identity_lines(candidate: Candidate) -> list[str]:
    lines = [f"🌐 平台：{candidate.platform}"]
    if candidate.note:
        lines.append(f"🔍 匹配：{candidate.note}")
    if candidate.name:
        lines.append(f"👤 名字：{candidate.name}")
    if candidate.handle:
        lines.append(f"🆔 账号：{candidate.handle}")
    if candidate.url:
        lines.append(f"🔗 {candidate.url}")
    return lines


def _grok_lines(grok: dict) -> list[str]:
    """Grok 兜底的两档文案：post 是找到，account 只是线索。"""
    tier = grok.get("tier")
    reason = str(grok.get("reason") or "").strip()
    if tier == "post":
        lines = ["✅ 找到 X 上的原帖"]
        if grok.get("post_url"):
            lines.append(f"🔗 {grok['post_url']}")
        if reason:
            lines.append(f"🔍 依据：{reason}")
        return lines
    if tier == "account":
        lines = ["🔎 X 搜索指向这个账号，但没核到原帖"]
        if grok.get("handle"):
            lines.append(f"🔗 https://x.com/{grok['handle']}")
        if grok.get("post_url"):
            # 帖子配图指纹对不上：账号多半是对的，帖子可能是同一套的另一张。
            # 只给链接，不说成原帖——同景同角色换个人拍，指纹也会这样。
            lines.append(f"🔗 存疑参考（配图相似但非同图，可能是同一套的另一张，请自行核对）：{grok['post_url']}")
        if reason:
            lines.append(f"🔍 依据：{reason}")
        return lines
    return []


def _best_block(outcome: SearchOutcome) -> str:
    best = outcome.best
    grok = outcome.grok or {}
    grok_lines = _grok_lines(grok)
    # 帖子档高于视觉档：帖子能点开对照，视觉只是模型判断，所以先给帖子。
    if grok.get("tier") == "post":
        if best is not None and best.status == "vision":
            grok_lines.append("视觉复核也认为是同一个人（模型判断，仅供参考），那张候选见参考区。")
        return "\n".join(grok_lines)
    if best is None and grok_lines:
        # 只拿到账号：比「完全没找到」有用，但没有帖子可对照，仍然算线索。
        vision = outcome.vision or {}
        if vision.get("ok") and vision.get("checked"):
            if vision.get("downgraded"):
                grok_lines.append(
                    f"视觉复核挑出 {vision['downgraded']} 条，但都没有可核对的账号（电商商品页或只有平台页面），只当参考。"
                )
            else:
                grok_lines.append(f"视觉复核了 {vision['checked']} 张候选，没有发现同一个人。")
        elif vision.get("error"):
            grok_lines.append(f"视觉复核没做完：{vision['error']}")
        return "\n".join(grok_lines)
    if best is None:
        compared = sum(1 for item in outcome.candidates if item.distance is not None)
        highest = max((item.distance or 0 for item in outcome.candidates), default=0)
        suspects = [item for item in outcome.candidates if item.status == "suspect"]
        lines = ["⚠️ 未找到可靠出处"]
        if compared:
            lines.append(f"已比对 {compared} 条候选，最高指纹距离 {highest}，均未通过阈值。")
        else:
            lines.append("候选图都没能取到或解码，无法验证。")
        vision = outcome.vision or {}
        if vision.get("ok") and vision.get("checked"):
            if vision.get("downgraded"):
                lines.append(
                    f"视觉复核挑出 {vision['downgraded']} 条，但都没有可核对的账号（电商商品页或只有平台页面），只当参考。"
                )
            else:
                lines.append(f"视觉复核了 {vision['checked']} 张候选，没有发现同一个人。")
        elif vision.get("error"):
            lines.append(f"视觉复核没做完：{vision['error']}")
        if grok:
            if grok.get("error") and grok.get("tier") != "none":
                lines.append(f"Grok 找人没做完：{grok['error']}")
            elif grok.get("error"):
                lines.append(f"Grok 找人没做成：{grok['error']}")
            else:
                lines.append("Grok 在 X 上搜过，没有找到对应的账号或帖子。")
        if suspects:
            lines.append(f"另有 {len(suspects)} 条疑似同图，仅供参考，见参考区。")
        if outcome.total_candidates:
            lines.append(f"共收到 {outcome.total_candidates} 条结果，见参考区。")
        return "\n".join(lines)
    lines = _identity_lines(best)
    if best.status == "vision":
        # 视觉判的是「同一个人」，不是同一张图，别让用户以为这就是那张原图。
        lines.insert(0, "🧠 没找到同一张图，但视觉复核认为是同一个人")
        lines.append("⚠️ 这条是视觉模型挑的，不是指纹比对，可能认错，请自行核对。")
    if grok_lines:
        lines.append("—")
        lines.extend(grok_lines)
    return "\n".join(lines)


ENGINE_LABELS = {
    "lens": "Google Lens",
    "yandex": "Yandex",
    "baidu": "百度识图",
    "saucenao": "SauceNAO",
}


def _engine_name(engine: str) -> str:
    return ENGINE_LABELS.get(engine, engine or "未知引擎")


def _candidate_identity(item: Candidate) -> str:
    """没下载比对过、或指纹明确不符的候选，账号并不是原作者，不能当结论发出去。"""
    if item.status in {"unverified", "mismatch"}:
        return "（账号未确认）"
    return item.handle or item.name or "（未识别）"


def _candidate_text(item: Candidate, index: int) -> str:
    tag = f"[{item.platform}]"
    identity = _candidate_identity(item)
    status = item.note or "未验证"
    lines = [f"{index}. {tag} {identity} · {status}"]
    if item.url:
        lines.append(f"   {item.url}")
    return "\n".join(lines)


def engine_groups(
    outcome: SearchOutcome,
    best: Candidate | None,
    limit: int,
) -> list[tuple[str, list[Candidate]]]:
    """按引擎把候选分组，组内按指纹距离从近到远排；limit 是每个引擎最多列几条。

    组内排序按「距离优先、其次相似度」，这样每组第一条就是该引擎最接近的那张，
    配图时取它，用户看到的也就是最接近的那条。
    """
    groups: dict[str, list[Candidate]] = {}
    for item in outcome.candidates:
        # 视觉挑中的那条也要留在参考区，结论里「那张候选见参考区」才对得上号；
        # 指纹命中时 best 已经在结论里给过，不必重复列。
        if item is best and (best is None or best.status != "vision"):
            continue
        groups.setdefault(item.engine, []).append(item)
    result: list[tuple[str, list[Candidate]]] = []
    for engine, items in groups.items():
        ordered = sorted(
            items,
            key=lambda item: (
                item.distance if item.distance is not None else 999,
                -(item.similarity or 0),
            ),
        )
        result.append((engine, ordered[: max(1, limit)]))
    return result


def candidate_image(outcome: SearchOutcome, item: Candidate) -> str:
    """取候选的展示图；已经作为参考图单独贴过的不要重复。"""
    if not item.image_url:
        return ""
    if outcome.best is None and outcome.image_b64 and item.image_url == outcome.image_source:
        return ""
    return (outcome.images or {}).get(item.image_url, "")


def _header_text(outcome: SearchOutcome, command: str, replay: bool) -> str:
    header = f"🔍 {command}结果"
    if outcome.status == "error":
        header = f"🔍 {command} · 搜索失败"
    if replay:
        header = f"🔍 {command}结果 · 全部候选"
    if outcome.ai and outcome.ai.get("ok"):
        try:
            if float(outcome.ai.get("score") or 0) >= AI_FLAG_THRESHOLD:
                header = f"🔍 {command} · 疑似 AI 生成"
        except (TypeError, ValueError):
            pass
    if outcome.cached:
        header += " · 缓存"
    return header


def ttl_text(minutes: int) -> str:
    """把保留时长写成人话：1440 分钟读起来太费劲。"""
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes >= 60:
        hours = minutes / 60
        return f"{hours:.0f} 小时" if hours == int(hours) else f"{hours:.1f} 小时"
    return f"{minutes} 分钟"


def _reference_distance(outcome: SearchOutcome, source: str) -> int | None:
    """参考图跟原图的指纹距离。

    先按图片地址在候选里找；Grok 参考图不在候选里时，用它核对时记下的最近
    距离。取不到就返回 None，文案里就不写距离。
    """
    if not source:
        return None
    for item in outcome.candidates:
        if item.image_url and item.image_url == source and item.distance is not None:
            return item.distance
    grok = outcome.grok or {}
    if str(grok.get("reference_url") or "") == source:
        distance = grok.get("distance")
        if isinstance(distance, int) and distance >= 0:
            return distance
    return None


def _footer_lines(outcome: SearchOutcome, command: str, ttl_minutes: int, replay: bool, task_id: str = "") -> list[str]:
    lines = [_route_line(outcome), _ai_line(outcome.ai)]
    if outcome.cached:
        # 说清这是复用，不是刚跑出来的：同一张图结果一样，但引擎这次一条都没调。
        lines.append("💾 这张图之前搜过，直接复用了当时的结果（引擎没有重新跑）")
    if outcome.image_b64 and outcome.image_source == "upload":
        lines.append("（候选图都没取到，转发里的图是你发的原图）")
    elif outcome.image_b64 and outcome.best is None:
        grok_ref = str((outcome.grok or {}).get("reference_url") or "")
        distance = _reference_distance(outcome, outcome.image_source)
        suffix = f"，指纹距离 {distance}，不是同一张" if distance is not None else "，未经指纹确认"
        if grok_ref and outcome.image_source == grok_ref:
            lines.append(f"（参考图是 Grok 给的帖子里跟原图最接近的一张{suffix}）")
        else:
            lines.append(f"（参考区那张是候选里跟原图最接近的一张{suffix}）")
    if outcome.errors:
        failed = "、".join(f"{name}（{message[:40]}）" for name, message in outcome.errors.items())
        lines.append(f"⚠️ 未返回结果的引擎：{failed}")
    lines.append("")
    if replay:
        lines.append("以上为全部参考结果")
    else:
        if task_id:
            # 编号是「更多」的凭据：群里同时有好几次搜索时，光说「更多」会取错。
            lines.append(f"任务编号：{task_id}（保留 {ttl_text(ttl_minutes)}）")
            lines.append(f"回复「{command} 更多 {task_id}」查看全部结果")
        else:
            lines.append(f"{ttl_text(ttl_minutes)}内回复「{command} 更多」可查看全部结果")
        # 缓存命中时结果会秒回，给个明确的重新搜入口，免得以为只能等缓存过期。
        lines.append(f"重新搜一遍（忽略缓存）：发图时带上「{command} 重搜」")
    return lines


def build_forward_nodes(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 8,
    nickname: str = "麦麦寻人",
    ttl_minutes: int = 15,
    replay: bool = False,
    user_id: str = "",
) -> list[dict]:
    """生成合并转发节点：标题、最佳匹配、按引擎分组的参考区、页脚。

    limit 是每个引擎最多列几条。图片由调用方预先塞进 outcome.images
    （键为候选图地址），首次回复只放每个引擎最接近的那张，回「更多」时放全部。
    user_id 会写进转发节点的 uin。QQ 端要求 uin 非空，留空会出现
    「消息异常」或只剩一个 [聊天记录] 占位，所以调用方要把机器人账号传进来。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    nodes: list[dict] = []

    def node(content: list[dict]) -> dict:
        item = {"nickname": nickname, "content": content}
        if user_id:
            item["user_id"] = user_id
        return item

    nodes.append(node([{"type": "text", "content": _header_text(outcome, command, replay)}]))

    best_content: list[dict] = [{"type": "text", "content": _best_block(outcome)}]
    if outcome.image_b64 and outcome.best is not None:
        best_content.append({"type": "image", "content": outcome.image_b64})
    nodes.append(node(best_content))

    # 视觉和 Grok 都挑出了东西时，两张图并排给：结论里一句「Grok 那边也有个号」
    # 不如图直接摆出来好判断。
    if outcome.grok_image_b64:
        grok_gap = (outcome.grok or {}).get("distance")
        gap_text = f"，指纹距离 {grok_gap}" if isinstance(grok_gap, int) and grok_gap >= 0 else ""
        nodes.append(
            node(
                [
                    {"type": "text", "content": f"📋 Grok 帖子配图里跟原图最接近的一张{gap_text}（配图非同图，仅供参考）"},
                    {"type": "image", "content": outcome.grok_image_b64},
                ]
            )
        )

    # 没找到可靠出处时，先单独给一张参考图，方便用户自己核对是不是同一张。
    if outcome.best is None and outcome.image_b64:
        grok_ref = str((outcome.grok or {}).get("reference_url") or "")
        distance = _reference_distance(outcome, outcome.image_source)
        gap = f"，指纹距离 {distance}" if distance is not None else ""
        if grok_ref and outcome.image_source == grok_ref:
            hint = f"📋 参考图（Grok 帖子配图里跟原图最接近的一张{gap}，不是同一张）"
        else:
            hint = f"📋 参考图（候选里跟原图最接近的一张{gap}，不是同一张）"
        nodes.append(
            node(
                [
                    {"type": "text", "content": hint},
                    {"type": "image", "content": outcome.image_b64},
                ]
            )
        )

    # 参考区按引擎分组，每个引擎一个节点，组内候选顺序沿用排序结果。
    # 命中时（指纹、视觉、Grok 任一）首次回复只给结论，参考区留给「更多」：
    # 一次搜到结果还要刷四五条转发，群里看着比没找到更烦。
    if show_reference and outcome.status != "ok":
        for engine, items in engine_groups(outcome, outcome.best, limit):
            content: list[dict] = [
                {"type": "text", "content": f"▍{_engine_name(engine)}（{len(items)} 条）"}
            ]
            for index, item in enumerate(items, start=1):
                content.append({"type": "text", "content": _candidate_text(item, index)})
                image = candidate_image(outcome, item)
                if image:
                    content.append({"type": "image", "content": image})
            nodes.append(node(content))

    nodes.append(
        node(
            [
                {
                    "type": "text",
                    "content": "\n".join(_footer_lines(outcome, command, ttl_minutes, replay, task.task_id)),
                }
            ]
        )
    )

    return nodes


def build_engine_forward_nodes(
    task: SearchTask,
    engine: str,
    items: list[Candidate],
    *,
    command: str,
    nickname: str = "麦麦寻人",
    ttl_minutes: int = 15,
    user_id: str = "",
    part: tuple[int, int] | None = None,
    with_best: bool = False,
    include_footer: bool = False,
) -> list[dict]:
    """把一个引擎的一批候选单独打成一条合并转发。

    QQ 对单条合并转发的图片总量有限制，图一多（十几张）组装就会失败，
    所以「更多」按引擎拆成几条发，每条只有该引擎的候选、图片随候选走。
    """
    outcome = task.outcome or SearchOutcome(status="error")
    nodes: list[dict] = []

    def node(content: list[dict]) -> dict:
        entry = {"nickname": nickname, "content": content}
        if user_id:
            entry["user_id"] = user_id
        return entry

    label = f"▍{_engine_name(engine)}（{len(items)} 条）"
    if part:
        label += f" · {part[0]}/{part[1]}"
    header = f"{_header_text(outcome, command, True)} · {label}"
    nodes.append(node([{"type": "text", "content": header}]))

    if with_best:
        best_content: list[dict] = [{"type": "text", "content": _best_block(outcome)}]
        if outcome.image_b64 and outcome.best is not None:
            best_content.append({"type": "image", "content": outcome.image_b64})
        nodes.append(node(best_content))

    for index, item in enumerate(items, start=1):
        content: list[dict] = [{"type": "text", "content": _candidate_text(item, index)}]
        image = candidate_image(outcome, item)
        if image:
            content.append({"type": "image", "content": image})
        nodes.append(node(content))

    if include_footer:
        nodes.append(
            node([{"type": "text", "content": "\n".join(_footer_lines(outcome, command, ttl_minutes, True))}])
        )
    return nodes


def stale_image_hint(stale_seconds: int) -> str:
    """图找到了、但早于取图窗口时给模型的提示：引导引用，而不是说没有图。"""
    minutes = max(1, int(stale_seconds) // 60)
    return (
        f"当前会话最近一张图是约 {minutes} 分钟前发的，超出 3 分钟的自动取图窗口，所以没取到。"
        "这不是图片服务故障，不要反复重试。那条消息在上下文里有 msg_id，"
        "把它作为 image_search 的 msg_id 参数再调一次即可；也可以请用户引用那张图。"
    )


def build_text_summary(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 5,
    eta_text: str = "约 60 秒",
) -> str:
    """给模型看的纯文字摘要，不带图片。"""
    outcome = task.outcome
    if outcome is None:
        if task.status == "running":
            return f"搜索还在进行中（task_id={task.task_id}），{eta_text}后可用 search_status 取结果。"
        return f"任务失败：{task.error or '未知错误'}"

    lines = [f"任务 {task.task_id}：{_best_block(outcome)}"]
    if outcome.status != "ok":
        lines.append(f"结论：{outcome.status}")
    if show_reference and outcome.candidates and outcome.status != "ok":
        # 命中时不列参考候选：模型只需要知道结论，列多了它会忍不住在群里复述一遍。
        groups = engine_groups(outcome, outcome.best, limit)
        if groups:
            lines.append("其他候选（按引擎分组）：")
            for engine, items in groups:
                lines.append(f"[{_engine_name(engine)}]")
                for index, item in enumerate(items, start=1):
                    identity = _candidate_identity(item)
                    detail = f"{index}. [{item.platform}] {identity} · {item.note or '未验证'}"
                    if item.url:
                        detail += f" · {item.url}"
                    lines.append(detail)
    lines.append(_route_line(outcome).replace("🖼 ", ""))
    lines.append(_ai_line(outcome.ai).replace("🤖 ", ""))
    lines.append(f"耗时 {outcome.elapsed_ms / 1000:.1f} 秒，共 {outcome.total_candidates} 条候选。")
    return "\n".join(lines)
