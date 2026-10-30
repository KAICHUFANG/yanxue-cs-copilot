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
import store

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

# 这些占位符天然无法自动填：要么活动结束后才产生，要么必须人判断
_ALWAYS_MANUAL = {
    # 2026-10-26 改：不再让客服整句自己想。
    # 系统先用活动推广推文拟一版草稿（agent.derive_*），客服只负责核对，
    # 核对不过关才由人重写。
    "{{现场亮点": "系统会先按当期推文拟一版草稿，发之前核对现场是否真照推文执行",
    "{{具体细节": "系统会先按活动回顾推文拟一版草稿，核对后再发",
    "{{回顾推文链接}}": "推文发布后才有链接（回顾推文原文里带链接时会自动填上）",
}

# 每一类占位符为什么填不了。要写给客服看，不能只说「未填」。
_MANUAL_REASON = dict(_ALWAYS_MANUAL)
_MANUAL_REASON["{{反馈时点}}"] = (
    "必须按建群时间改：距活动不足 2 天 →「今天较晚时段」；否则 →「明天下午」"
)


def _reason_for(token: str) -> str:
    for prefix, reason in _MANUAL_REASON.items():
        if token.startswith(prefix):
            return reason
    if token in _FIELD_SOURCES:
        return "活动记录里这一项还是空的：问活动负责人确认，然后直接跟我说「集合地点在…」就会写进去"
    return "代码里没有这项数据来源，需要人工补"



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
            # 派生字段（weekday 等）由 store 现算，文件里不存
            return store.with_derived(a)
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


def _load_processes() -> Dict[str, Any]:
    path = config.DATA_DIR / "processes.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 流程发送包
# 「进校预约」这类流程的定稿存在 processes.json 里，群文案阶段只写一句
# uses_process: campus_booking 引用它（定稿只存在一处，不复制）。
#
# 这里踩过一个不报错的坑（2026-10-26 用户报的）：
#   只判「这个阶段有没有 messages」，有就只渲染 messages、`uses_process` 被静默忽略。
#   结果阶段②只吐出「进校门小提醒」，真正的预约文案、公众号名片、填写示例图、
#   催办话术全部没出现——客服以为系统漏了，只能自己去翻文件。
#   所以现在：只要有 uses_process，就一定把流程的 send_package 展开进来。


def _process_sources() -> Dict[str, Optional[str]]:
    """流程文案里的占位符。（None = 代码没有来源，必须人工填）"""
    import knowledge
    team = knowledge.load_team_info()      # 业务信息从数据里读，.env 只做兜底
    return {
        "{{被访人姓名}}": team.get("visit_target_name") or None,
        "{{被访人电话}}": team.get("visit_target_phone") or None,
        # 不能照抄默认值：建群晚的活动要改成「今天较晚时段」，否则来不及催审核
        "{{反馈时点}}": None,
    }


def render_plain(template: str, sources: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """通用占位符渲染（给流程/发送包用）。

    和 render_text 的区别：这里的数据不来自活动记录，而是来自 .env 和流程文件。
    规则一样：查不到就原样保留并记进未填清单，绝不用推测顶替。
    """
    unfilled: List[str] = []

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        value = sources.get(token)
        if value in (None, ""):
            unfilled.append(token)
            return token
        return str(value)

    return {"text": _PLACEHOLDER_RE.sub(_replace, template), "unfilled": unfilled}


def _process_items(proc_key: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把流程里的 send_package 展开成「一条一条要发的东西」。"""
    proc = _load_processes().get(proc_key) or {}
    pkg = proc.get("send_package") or {}
    sources = _process_sources()
    items: List[Dict[str, Any]] = []

    for it in pkg.get("items") or []:
        base = {
            "stage_seq": it.get("seq", 99),
            "label": it.get("label", ""),
            "source": it.get("source") or ("" if it.get("type") == "manual_send_required"
                                           else "用户提供"),
            "from_process": proc_key,
        }
        if it.get("type") == "manual_send_required":
            items.append({**base, "manual": True,
                          "action": it.get("content_note") or it.get("text") or "",
                          "note": it.get("why", "")})
            continue
        r = render_plain(it.get("text", ""), sources)
        items.append({**base, "text": r["text"],
                      "unfilled": r["unfilled"], "ready": not r["unfilled"]})

    items.sort(key=lambda x: x.get("stage_seq", 99))
    return items, proc


def _merge_items(proc_items: List[Dict[str, Any]], inline: List[Dict[str, Any]],
                 insert_after_seq: Optional[int]) -> List[Dict[str, Any]]:
    """把流程的发送包和本阶段自带的文案按发送顺序合到一起。

    insert_after_seq：本阶段自带的那几条，要插在流程的第几条之后发。
      例：进校门提醒插在「填写示例图（3）」之后、「催办（4）」之前。
      不写就排在整个流程后面——宁可顺序平一点，也不要猜错顺序。
    """
    if not proc_items:
        return list(inline)
    if insert_after_seq is None:
        return list(proc_items) + list(inline)
    head = [i for i in proc_items if i.get("stage_seq", 99) <= insert_after_seq]
    tail = [i for i in proc_items if i.get("stage_seq", 99) > insert_after_seq]
    return head + list(inline) + tail


def _resolve_ref(ref: str) -> str:
    """解析形如 "campus_booking.entry_gate.ready_to_send_text" 的引用。

    为什么要引用而不是复制：门禁提醒这类定稿只应存在一处。
    复制到两个文件里，改了一处忘了另一处，就会出现「群文案说得跟流程不一样」。
    """
    try:
        node: Any = _load_processes()
        for part in ref.split("."):
            node = node[part]
        return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)
    except Exception:
        return f"【未能解析引用：{ref}】"


def render_group_messages(
    activity_id: str,
    stage: Optional[str] = None,
    overrides: Optional[Dict[str, str]] = None,
    only_tags: Optional[List[str]] = None,
) -> str:
    """渲染活动群文案。activity_id 形如 S16；stage 可只渲染某一阶段（如 "③"）。

    only_tags：同一阶段里有几种不同用途的文案时，只出指定的那几种。
      例：阶段⑤ 包含「结束感谢」(tag=thanks) 和「回顾分享」(tag=review)，
      客服要「写结束感谢」时不该把回顾分享那段也拖出来（那时它全是空）。
      不传 = 这个阶段的全部都要。
    """
    activity = load_activity(activity_id)
    if not activity:
        return json.dumps(
            {"error": f"没有找到 {activity_id} 对应的活动数据",
             "hint": "可先调用 list_activities 查看有哪些期次"},
            ensure_ascii=False,
        )

    path = config.DATA_DIR / "group_messages.json"
    with open(path, encoding="utf-8") as f:
        book = json.load(f)

    out: List[Dict[str, Any]] = []
    all_unfilled: List[str] = []
    reasons: Dict[str, str] = {}

    def note(tokens: List[str]) -> None:
        for tk in tokens:
            if tk not in all_unfilled:
                all_unfilled.append(tk)
                reasons[tk] = _reason_for(tk)

    for st in book["timeline"]:
        if stage and stage not in st["stage"]:
            continue

        # ① 本阶段引用的流程（进校预约…）：先展开成一条条要发的东西。
        #    注意：**不能只在 messages 为空时才展开** —— 旧逻辑就是这样，
        #    导致阶段②只吐出「进校门提醒」，预约文案/公众号名片/填写示例图全丢失
        #    （2026-10-26 用户实测报出来的）。
        proc_items, proc = ([], {})
        if st.get("uses_process"):
            proc_items, proc = _process_items(st["uses_process"])
            for it in proc_items:
                note(it.get("unfilled") or [])

        # ② 本阶段自带的文案
        inline: List[Dict[str, Any]] = []
        for msg in st.get("messages") or []:
            if only_tags and msg.get("tag") not in only_tags:
                continue

            # 有前置条件的条目（目前只有「回顾分享」）：条件不满足就不生成正文。
            # 真实报过的问题：一键生成群文案时把「活动回顾推文出来啦」也吐出来了，
            # 而那时活动回顾推文根本还没发；而且它把 {{具体细节}} 列进「需人工补」，
            # 让人以为要自己写 —— 实际应该按回顾推文自动拟。所以这里直接不生成。
            if msg.get("requires_review"):
                import knowledge as _kb
                if not (_kb.load_review() or "").strip():
                    out.append({
                        "stage": st["stage"],
                        "label": msg.get("label", ""),
                        "note": msg.get("requires_note")
                                or "这段要等活动回顾推文出来才能写。",
                        "ready": False,
                    })
                    continue

            if msg.get("type") == "manual_send_required":
                inline.append({
                    "stage": st["stage"],
                    "label": msg["label"],
                    "manual": True,
                    "action": msg.get("content_note") or msg.get("text"),
                    "source": msg.get("source"),
                })
                continue

            if msg.get("type") == "reference":
                ref = msg.get("ref", "")
                resolved = _resolve_ref(ref)
                r = render_plain(resolved, _process_sources())
                note(r["unfilled"])
                inline.append({
                    "stage": st["stage"],
                    "label": msg["label"],
                    "source": msg.get("source"),
                    "text": r["text"],
                    "ready": not r["unfilled"],
                    "unfilled": r["unfilled"],
                    "from_ref": ref,
                })
                continue

            result = render_text(msg["text"], activity, overrides)
            note(result["unfilled"])
            inline.append({
                "stage": st["stage"],
                "label": msg["label"],
                "source": msg.get("source"),
                "text": result["text"],
                "ready": result["ready"],
                "unfilled": result["unfilled"],
            })

        if not proc_items and not inline and st.get("uses_process"):
            # 流程里没写发送包、也没有自带文案：只能说清去哪里找，不能假装有内容
            out.append({
                "stage": st["stage"],
                "label": st.get("stage", ""),
                "note": (f"该阶段引用流程 {st['uses_process']}，但那个流程里没有可发的文案，"
                         f"请人工确认要发什么"),
                "ready": False,
            })
            continue

        out.extend(_merge_items(proc_items, inline, st.get("insert_after_seq")))

        # ③ 流程里的必做提醒（漏发名片、漏发示例图、必须改反馈时点）
        tips = [t.get("text") for t in
                ((proc.get("send_package") or {}).get("handler_tips") or [])
                if t.get("text")]
        if tips:
            out.append({"stage": st["stage"],
                        "label": "这一步必须人工做的",
                        "tips": tips})

    return json.dumps(
        {
            "activity_id": activity["id"],
            "activity_title": activity.get("title"),
            "messages": out,
            "unfilled_summary": all_unfilled,
            "unfilled_reasons": reasons,
            "rule": (
                "text 是填好数据的成品，必须原样输出给客服，禁止改写、润色、调整数字。"
                "manual=true 的条目是系统发不了的（公众号名片、示例截图、导航照片），"
                "必须一条不落地告诉客服由她亲自发。"
                "unfilled_summary 里的占位符是代码判断不出来的（集合地点/集合时间等每期不同，"
                "或活动后才产生），必须明确告诉客服需要人工补充，绝对不要自行编造；"
                "unfilled_reasons 里写了每一项为什么填不了、该怎么补。"
            ),
        },
        ensure_ascii=False,
    )


def render_publish_flow(activity_id: str) -> str:
    """生成「活动群文案发布流程」清单（只讲流程，不生成任何正文文案）。

    为什么单独一个函数、而不是复用 render_group_messages：
        这两件事的输出物完全不同 ——
          渲染文案：给客服可以直接复制发出去的原话；
          发布流程：给客服一份「什么时候发什么、哪里容易漏」的清单。
        混在一起就会变成「一键吐出五段文案 + 一堆提示」，客服反而不知道先发哪个。
    """
    activity = load_activity(activity_id)
    book = json.loads((config.DATA_DIR / "group_messages.json").read_text(encoding="utf-8"))
    flow = book.get("publish_flow") or {}
    if not flow:
        return "（流程数据缺失：group_messages.json 里没有 publish_flow）"

    label = (activity or {}).get("session_label") or "本期"
    title = (activity or {}).get("title") or ""
    lines = [f"📋 活动群文案发布流程 · {label}" + (f"「{title}」" if title else ""), ""]
    lines.append("按顺序来。每一步只在这里看「发什么」，具体文案在对话框里要（例如「写个欢迎语」）。")
    lines.append("")

    for st in flow.get("main_steps") or []:
        lines.append(f"{st['seq']}️⃣ {st['when']} · {st['title']}")
        lines.append(f"    发什么：{st['what']}")
        if st.get("caveat"):
            lines.append(f"    ⚠️ {st['caveat']}")
        lines.append("")

    sides = flow.get("side_notes") or []
    if sides:
        lines.append("————")
        lines.append("⏱ 流程之外（不受上面的时间线限制）")
        lines.append("")
        for i, sn in enumerate(sides, 1):
            lines.append(f"{i}. {sn['title']}｜{sn['when']}")
            lines.append(f"   {sn['text']}")
            lines.append("")

    lines.append("————")
    lines.append("记不住就先看这一句：欢迎 → 预约 → 前一天提醒 → 当天出发 → 结束感谢 → 回顾推广；"
                 "定位随时发，引导图活动开始前发。")
    return chr(10).join(lines).rstrip() + chr(10)
