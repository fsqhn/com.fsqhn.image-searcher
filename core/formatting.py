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


def _best_block(outcome: SearchOutcome) -> str:
    best = outcome.best
    if best is None:
        compared = sum(1 for item in outcome.candidates if item.distance is not None)
        highest = max((item.distance or 0 for item in outcome.candidates), default=0)
        suspects = [item for item in outcome.candidates if item.status == "suspect"]
        lines = ["⚠️ 未找到可靠出处"]
        if compared:
            lines.append(f"已比对 {compared} 条候选，最高指纹距离 {highest}，均未通过阈值。")
        else:
            lines.append("候选图都没能取到或解码，无法验证。")
        if suspects:
            lines.append(f"另有 {len(suspects)} 条疑似同图，仅供参考，见参考区。")
        if outcome.total_candidates:
            lines.append(f"共收到 {outcome.total_candidates} 条结果，见参考区。")
        return "\n".join(lines)
    return "\n".join(_identity_lines(best))


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
        if item is best:
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
    return header


def _footer_lines(outcome: SearchOutcome, command: str, ttl_minutes: int, replay: bool) -> list[str]:
    lines = [_route_line(outcome), _ai_line(outcome.ai)]
    if outcome.image_b64 and outcome.image_source == "upload":
        lines.append("（候选图都没取到，转发里的图是你发的原图）")
    elif outcome.image_b64 and outcome.best is None:
        lines.append("（参考区那张是相似度最高的候选，未经指纹确认）")
    if outcome.errors:
        failed = "、".join(f"{name}（{message[:40]}）" for name, message in outcome.errors.items())
        lines.append(f"⚠️ 未返回结果的引擎：{failed}")
    lines.append("")
    if replay:
        lines.append("以上为全部参考结果")
    else:
        lines.append(f"{ttl_minutes} 分钟内回复「{command} 更多」可查看全部结果")
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

    # 没找到可靠出处时，先单独给一张参考图，方便用户自己核对是不是同一张。
    if outcome.best is None and outcome.image_b64:
        nodes.append(
            node(
                [
                    {"type": "text", "content": "📋 参考图（未经指纹确认）"},
                    {"type": "image", "content": outcome.image_b64},
                ]
            )
        )

    # 参考区按引擎分组，每个引擎一个节点，组内候选顺序沿用排序结果。
    if show_reference:
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

    nodes.append(node([{"type": "text", "content": "\n".join(_footer_lines(outcome, command, ttl_minutes, replay))}]))

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
        f"当前会话最近一张图是约 {minutes} 分钟前发的，已经超出 3 分钟的取图窗口，所以取不到。"
        "这不是图片服务故障，不要反复重试。请让用户引用那张图再发一次，或者重新发图。"
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
    if show_reference and outcome.candidates:
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
