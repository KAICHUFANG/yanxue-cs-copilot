"""
文案渲染层：把模板里的 {{占位符}} 用活动数据填上

为什么必须有这一层：
    group_messages.json 里存的是带 {{}} 的模板。如果没有这一层，
    要么客服手工一个个替换（容易漏），要么让模型去填——而模型会当场编
    日期、编名额、编集合地点，然后被发到几百人的家长群。

    所以这里的规则是：
        能从 activities.json 查到的  → 代码填，填得准
        查不到的                   → 保留 {{}} 并列入未填清单，绝不用推测顶替

    模型在这条链路上只负责「把结果念出来」，不负责「想内容」。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import config

_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")

# 占位符 → (activities.json 里的字段名, 格式化函数)
# 值为 None 表示该字段在当前数据里不存在，会被归入未填清单
_FIELD_SOURCES: Dict[str, Tuple[str, Optional[str]]] = {
    "{{期次}}": ("session_label", None),
    "{{活动名}}": ("title", None),
    "{{活动日期}}": ("date", "date"),
    "{{活动时间}}": ("time_range", None),
    "{{参与对象}}": ("target_audience", None),
    "{{名额}}": ("capacity", "capacity"),
    "{{集合地点}}": ("meeting_point", None),
    "{{集合时间}}": ("meeting_time", None),
}

# 这些占位符天然无法自动填：要么活动结束后才产生，要么由人判断
_ALWAYS_MANUAL = {
    "{{一句真实亮点": "活动结束后才能写，必须人工",
    "{{具体细节": "依赖现场实际发生的内容，必须人工",
    "{{回顾推文链接}}": "推文发布后才有链接，必须人工",
}


def _fmt_date(activity: Dict[str, Any]) -> Optional[str]:
    """2026-10-24 + 周六 → 10月24日（周六）"""
    raw = activity.get("date")
    if not raw:
        return None
    try:
        _, m, d = raw.split("-")
        wd = activity.get("weekday")
        return f"{int(m)}月{int(d)}日（{wd}）" if wd else f"{int(m)}月{int(d)}日"
    except Exception:
        return raw


def _fmt_capacity(activity: Dict[str, Any]) -> Optional[str]:
    cap = activity.get("capacity")
    if not cap:
        return None
    return f"{cap}人"


def load_activity(activity_id: str) -> Optional[Dict[str, Any]]:
    """按 S16 / 16 / 第16期 取活动数据。"""
    m = re.search(r"(\d+)", activity_id or "")
    if not m:
        return None
    key = f"S{int(m.group(1)):02d}"
    with open(config.DATA_FILE, encoding="utf-8") as f:
        kb = json.load(f)
    for a in kb["activities"]:
        if a["id"] == key:
            return a
    return None


def render_text(
    template: str,
    activity: Dict[str, Any],
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """渲染单条文案。返回渲染结果 + 未填清单。"""
    overrides = overrides or {}
    unfilled: List[str] = []

    def _replace(match: re.Match) -> str:
        token = match.group(0)

        # 1. 人工指定的值优先
        if token in overrides and str(overrides[token]).strip():
            return str(overrides[token]).strip()

        # 2. 天然只能人工填的
        for prefix, _reason in _ALWAYS_MANUAL.items():
            if token.startswith(prefix):
                unfilled.append(token)
                return token

        # 3. 从活动数据里查
        src = _FIELD_SOURCES.get(token)
        if not src:
            unfilled.append(token)
            return token

        field_name, fmt = src
        if fmt == "date":
            value = _fmt_date(activity)
        elif fmt == "capacity":
            value = _fmt_capacity(activity)
        else:
            value = activity.get(field_name)

        if value in (None, "", [], {}):
            unfilled.append(token)
            return token

        return str(value)

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    return {
        "text": rendered,
        "unfilled": unfilled,
        "ready": len(unfilled) == 0,
    }


def render_group_messages(
    activity_id: str,
    stage: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
) -> str:
    """渲染活动群文案。activity_id 形如 S16；stage 可只渲染某一阶段（如 "③"）。"""
    activity = load_activity(activity_id)
    if not activity:
        return json.dumps(
            {"error": f"没有找到 {activity_id} 对应的活动数据",
             "hint": "可先调用 list_activities 查看有哪些期次"},
            ensure_ascii=False,
        )

    path = config.DATA_FILE.parent / "group_messages.json"
    with open(path, encoding="utf-8") as f:
        book = json.load(f)

    out: List[Dict[str, Any]] = []
    all_unfilled: List[str] = []

    for st in book["timeline"]:
        if stage and stage not in st["stage"]:
            continue
        if not st.get("messages"):
            # 引用型阶段（如预约进校），提示去查对应流程
            if st.get("uses_process"):
                out.append({
                    "stage": st["stage"],
                    "label": st.get("stage", ""),
                    "note": f"该阶段引用流程 {st['uses_process']}，请调用 get_campus_booking_flow 获取定稿",
                    "ready": False,
                    "unfilled": [],
                })
            continue

        for msg in st["messages"]:
            if msg.get("type") == "manual_send_required":
                out.append({
                    "stage": st["stage"],
                    "label": msg["label"],
                    "manual": True,
                    "action": msg.get("content_note") or msg.get("text"),
                    "source": msg.get("source"),
                })
                continue

            result = render_text(msg["text"], activity, overrides)
            for u in result["unfilled"]:
                if u not in all_unfilled:
                    all_unfilled.append(u)
            out.append({
                "stage": st["stage"],
                "label": msg["label"],
                "source": msg.get("source"),
                "text": result["text"],
                "ready": result["ready"],
                "unfilled": result["unfilled"],
            })

    return json.dumps(
        {
            "activity_id": activity["id"],
            "activity_title": activity.get("title"),
            "messages": out,
            "unfilled_summary": all_unfilled,
            "rule": (
                "text 是填好数据的成品，必须原样输出给客服，禁止改写、润色、调整数字。"
                "unfilled_summary 里的占位符是代码判断不出来的（集合地点/集合时间等每期不同，"
                "或活动后才产生），必须明确告诉客服需要人工补充，**绝对不要自行编造**。"
            ),
        },
        ensure_ascii=False,
    )
