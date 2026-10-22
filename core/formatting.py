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


def _reference_block(
    outcome: SearchOutcome,
    limit: int,
) -> str:
    best = outcome.best
    others = [item for item in outcome.candidates if item is not best]
    if not others:
        return ""
    others = others[:limit]
    lines = ["📋 其他结果"]
    for index, item in enumerate(others, start=1):
        tag = f"[{item.platform}]"
        identity = item.handle or item.name or "（未识别）"
        status = item.note or "未验证"
        lines.append(f"{index}. {tag} {identity} · {status}")
        if item.url:
            lines.append(f"   {item.url}")
    return "\n".join(lines)


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
    """生成合并转发的四个节点；只有最佳匹配节点带图。

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

    header = "🔍 麦麦寻人结果"
    if outcome.status == "error":
        header = "🔍 麦麦寻人 · 搜索失败"
    if replay:
        header = "🔍 麦麦寻人结果 · 全部候选"
    if outcome.ai and outcome.ai.get("ok"):
        try:
            if float(outcome.ai.get("score") or 0) >= AI_FLAG_THRESHOLD:
                header = "🔍 麦麦寻人 · 疑似 AI 生成"
        except (TypeError, ValueError):
            pass

    nodes.append(node([{"type": "text", "content": header}]))

    best_content: list[dict] = [{"type": "text", "content": _best_block(outcome)}]
    if outcome.image_b64 and outcome.best is not None:
        best_content.append({"type": "image", "content": outcome.image_b64})
    nodes.append(node(best_content))

    # 没找到可靠出处时，把参考图挂到参考区，方便用户自己核对是不是同一张。
    ref_image = outcome.image_b64 if outcome.best is None else ""
    if show_reference or ref_image:
        reference = _reference_block(outcome, max(1, limit))
        if reference or ref_image:
            ref_content: list[dict] = []
            if reference:
                ref_content.append({"type": "text", "content": reference})
            if ref_image:
                if not reference:
                    ref_content.append({"type": "text", "content": "📋 参考图（未经指纹确认）"})
                ref_content.append({"type": "image", "content": ref_image})
            nodes.append(node(ref_content))

    footer_lines = [_route_line(outcome), _ai_line(outcome.ai)]
    if outcome.image_b64 and outcome.image_source == "upload":
        footer_lines.append("（候选图都没取到，转发里的图是你发的原图）")
    elif outcome.image_b64 and outcome.best is None:
        footer_lines.append("（参考区那张是相似度最高的候选，未经指纹确认）")
    if outcome.errors:
        failed = "、".join(f"{name}（{message[:40]}）" for name, message in outcome.errors.items())
        footer_lines.append(f"⚠️ 未返回结果的引擎：{failed}")
    footer_lines.append("")
    if replay:
        footer_lines.append("以上为全部参考结果")
    else:
        footer_lines.append(f"{ttl_minutes} 分钟内回复「{command} 更多」可查看全部结果")
    nodes.append(node([{"type": "text", "content": "\n".join(footer_lines)}]))

    return nodes


def build_text_summary(
    task: SearchTask,
    *,
    command: str,
    show_reference: bool = True,
    limit: int = 5,
) -> str:
    """给模型看的纯文字摘要，不带图片。"""
    outcome = task.outcome
    if outcome is None:
        if task.status == "running":
            return f"搜索还在进行中（task_id={task.task_id}），大约 40 秒后可用 search_status 取结果。"
        return f"任务失败：{task.error or '未知错误'}"

    lines = [f"任务 {task.task_id}：{_best_block(outcome)}"]
    if outcome.status != "ok":
        lines.append(f"结论：{outcome.status}")
    if show_reference and outcome.candidates:
        best = outcome.best
        others = [item for item in outcome.candidates if item is not best][:limit]
        if others:
            lines.append("其他候选：")
            for index, item in enumerate(others, start=1):
                identity = item.handle or item.name or "（未识别）"
                detail = f"{index}. [{item.platform}] {identity} · {item.note or '未验证'}"
                if item.url:
                    detail += f" · {item.url}"
                lines.append(detail)
    lines.append(_route_line(outcome).replace("🖼 ", ""))
    lines.append(_ai_line(outcome.ai).replace("🤖 ", ""))
    lines.append(f"耗时 {outcome.elapsed_ms / 1000:.1f} 秒，共 {outcome.total_candidates} 条候选。")
    return "\n".join(lines)
