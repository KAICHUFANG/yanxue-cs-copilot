"""
路由层：用代码决定「该查什么」

为什么要这一层？
    前四版我们把"该调哪个工具""问的是第几期"全交给模型判断，
    结果在三个错误行为之间来回摇摆：
        ① 全部反问期次  ② 全部查 S16  ③ 全部只调 list_activities

    根因：这些判断本来就是**确定性的**——
        "第十六期" 里的期次是能解析出来的
        "退费" 这个词对应哪个工具也是确定的
    把它交给概率模型，就是在用不确定的东西做确定的事。

    所以拆开：
        router.py  用规则做确定性判断（抽期次、判意图）
        agent.py   带着结论去调模型，模型只负责组织语言

    判断不出来时（比如家长只问"多少钱？"），才把选择权交回模型。
"""

import re
from typing import Dict, List, Optional

# ---- 中文数字 → 整数（够用到 99）----
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> Optional[int]:
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


# 匹配：第16期 / 第十六期 / S16 / 16期 / 第 十六 期
_SESSION_PATTERNS = [
    re.compile(r"[sS]\s*(\d{1,2})"),
    re.compile(r"第\s*([0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*期"),
    re.compile(r"([0-9]{1,2})\s*期"),
]


def extract_sessions(question: str) -> List[str]:
    """抽出问题里出现的所有期次（可能一期、可能多期），去重后按出现顺序返回。"""
    found: List[str] = []
    for pat in _SESSION_PATTERNS:
        for m in pat.finditer(question):
            n = _cn_to_int(m.group(1))
            if n and 1 <= n <= 99:
                sid = f"S{n:02d}"
                if sid not in found:
                    found.append(sid)
    return found


def extract_session(question: str) -> Optional[str]:
    """抽第一个期次（兼容旧调用）。"""
    sessions = extract_sessions(question)
    return sessions[0] if sessions else None


# ---- 意图关键词 ----
_POLICY_WORDS = [
    "退费", "退款", "退钱", "取消", "天气", "下雨", "雨天", "台风", "大风",
    "怎么去", "集合", "集合点", "地点", "在哪", "自驾", "带什么", "自带",
    "水杯", "迟到", "早退", "缺席", "遇到恶劣",
    "陪同", "陪伴", "要不要陪", "大人", "家长进校", "能进校吗",
    "怎么报名", "报名方式", "哪里报名", "报名链接", "接龙", "在哪报名",
    "分组", "分好组", "一组", "几组", "怎么分组", "分几组",
    "晚点", "迟到一点点", "自己带", "带水", "需要带",
]
# 「快到点了、路上耽误了」这类问的是【到场规则】，不是【进校流程】。
# 必须在 campus 之前判断：否则「路上堵车」里的「车」会被当成进校问题。
_LATE_WORDS = [
    "堵车", "堵在", "晚到", "会晚", "迟到", "晚点", "赶不上", "来不了那么早",
    "还没到", "晚一点到", "稍微晚",
]
_LIST_WORDS = [
    "有什么活动", "有哪些活动", "有什么研学", "最近有什么", "活动列表",
    "都在报名", "有活动吗", "办过哪些", "哪些期",
]
_BRAND_WORDS = [
    "主办", "承办", "什么机构", "hi-悦野", "悦野", "公益", "谁办的",
    "什么组织", "哪个学院", "背景",
]
# 进校预约流程（含家长最常见的「进不去校门」类问题）
_CAMPUS_WORDS = [
    "进校", "进校园", "入校", "预约", "访客", "被访人", "校门", "门禁",
    "进不去", "怎么进去", "停车", "车速", "公众号", "私信",
    "身份证", "审核", "待审核", "北门", "东门", "南门", "西门", "东南门",
    "车", "停车", "哪个门", "从哪进", "能进校",
]
# 要写/生成活动群文案
_COPY_WORDS = [
    "文案", "群公告", "发群", "群里发", "写一段", "写个", "帮我写",
    "生成文案", "活动提醒", "欢迎语", "结束感谢", "报名文案",
]
# 客服补充 / 录入活动信息
_UPDATE_WORDS = [
    "补充", "录入", "更新一下", "更新信息", "记一下", "记录一下", "帮我记",
    "改成", "改为", "改成是", "添加", "存一下", "填上", "补上",
    "价格是", "名额是", "时间是", "地点是", "集合点是", "集合时间是",
]
# 字段类问法：家长不提期次，只问某个具体字段。
# 这类问题默认回答「正在报名的那一期」，因为客服服务的就是当前活动。
_FIELD_WORDS = [
    "多少钱", "收费", "费用", "几年级", "多大", "年龄", "名额", "多少人", "招多少",
    "在哪", "几点", "什么时候", "哪天", "周几", "室内", "室外", "每组", "一个组",
]
# 问缺口（还差什么信息）
_MISSING_WORDS = [
    "缺什么", "还缺", "还差", "缺少", "不完整", "齐了吗", "完整吗",
    "要补什么", "还少什么", "信息够吗",
]


def _active_session() -> Optional[str]:
    """取当前正在报名的那一期（用于「帮我写文案」没指定期次的场景）。"""
    try:
        import json

        import config

        with open(config.DATA_FILE, encoding="utf-8") as f:
            kb = json.load(f)
        for a in kb.get("activities", []):
            if a.get("status") == "报名中":
                return a["id"]
    except Exception:
        pass
    return None


# 疑问句标记：用来区分「第十六期是什么时候」（问）和「第十六期是室外活动」（答）
_QUESTION_MARKERS = [
    "吗", "呢", "？", "?", "多少", "几", "什么", "怎么", "哪", "是否",
    "能不能", "要不要", "有没有", "多久", "几点", "如何", "可不可以",
]


def _session_exists(sid: str) -> bool:
    """这个期次在知识库里有没有。"""
    try:
        import json

        import config

        with open(config.DATA_FILE, encoding="utf-8") as f:
            kb = json.load(f)
        return any(a["id"] == sid for a in kb.get("activities", []))
    except Exception:
        return True   # 读不到就按"存在"处理，宁可多查一次也不要误判


def _is_question(text: str) -> bool:
    return any(m in text for m in _QUESTION_MARKERS)


def detect_intent(question: str) -> str:
    """返回 list / policies / brand / activity / campus / copy / update / missing / auto"""
    sessions = extract_sessions(question)
    # 提到了知识库里没有的期次 → 优先去查，触发「没有记录」的如实回答，
    # 不要被「怎么报名」这类通用规则关键词截胡。
    # 例：「第十期怎么报名」应回答「没有第十期的记录」，而不是讲通用报名方式。
    unknown_session = bool(sessions) and not all(_session_exists(s) for s in sessions)

    if any(w in question for w in _MISSING_WORDS):
        return "missing"
    # 「补充信息」类优先判断：客服说「第十六期价格是…」时不能当成查询
    if any(w in question for w in _UPDATE_WORDS):
        return "update"
    if unknown_session and _is_question(question):
        return "activity"
    # 「堵车/晚到」优先当到场规则，不要被「车」这个词拖进进校流程
    if any(w in question for w in _LATE_WORDS):
        return "policies"
    if any(w in question for w in _CAMPUS_WORDS):
        return "campus"
    if any(w in question for w in _COPY_WORDS):
        return "copy"
    if any(w in question for w in _POLICY_WORDS):
        return "policies"
    if any(w in question for w in _LIST_WORDS):
        return "list"
    if any(w in question for w in _BRAND_WORDS):
        return "brand"
    if sessions:
        # 提到了期次，但整句不像在提问 → 当成客服在补充信息，而非查数据
        # 例：「第十六期是室外活动」= 补充；「第十六期是什么时候」= 提问
        return "activity" if _is_question(question) else "update"

    # 没提期次，但问的是某个具体字段 → 默认答「正在报名的那一期」
    # 例：家长在第十六期群里直接问「多少钱」
    if any(w in question for w in _FIELD_WORDS) and _active_session():
        return "field"

    return "auto"


def plan(question: str) -> Optional[List[Dict]]:
    """生成工具调用计划（可能包含多个调用）。

    返回 None 表示「判断不出来，把选择权交给模型」。
    """
    intent = detect_intent(question)
    sessions = extract_sessions(question)

    if intent == "campus":
        return [{"tool": "get_campus_booking_flow", "args": {}}]

    if intent == "missing":
        target = sessions[0] if sessions else ""
        return [{"tool": "check_missing_info", "args": {"activity_id": target}}]

    if intent == "field":
        active = _active_session()
        return [{"tool": "get_activity", "args": {"activity_id": active}}] if active else None

    if intent == "copy":
        # 文案需求：有期次就直接渲染该期；没提期次就取正在报名的那期
        target = sessions[0] if sessions else _active_session()
        if target:
            return [{"tool": "render_group_messages", "args": {"activity_id": target}}]
        return None

    if intent == "policies":
        return [{"tool": "get_policies", "args": {}}]

    if intent == "list":
        return [{"tool": "list_activities", "args": {"status": "全部"}}]

    if intent == "brand":
        return [{"tool": "get_brand_info", "args": {}}]

    if intent == "activity" and sessions:
        # 一次问多期 → 每期各查一次（最多 3 期，防止模型被塞爆）
        return [
            {"tool": "get_activity", "args": {"activity_id": sid}}
            for sid in sessions[:3]
        ]

    return None


def forced_tool_choice(planned: Optional[Dict]) -> Optional[Dict]:
    """把计划翻译成 OpenAI 兼容的 tool_choice 参数。

    这一步是关键：只要代码能确定，就**强制**模型调那一个工具，
    而不是「建议」它调。不确定性被压缩到最小。
    """
    if not planned:
        return None
    return {"type": "function", "function": {"name": planned["tool"]}}


def hint_text(planned: Optional[Dict], question: str) -> str:
    """给模型的提示：告诉它我已经确定了什么，避免它再反问。"""
    if not planned:
        return ""
    session = extract_session(question)
    if planned["tool"] == "get_activity" and session:
        n = int(session[1:])
        return (
            f"【系统已识别】家长问的是第{n}期（{session}），"
            f"请直接使用即将返回的活动数据回答，**不要反问是哪一期**。"
        )
    if planned["tool"] == "get_policies":
        return "【系统已识别】家长问的是通用规则，请结合退费/集合/携带要求等条款回答。"
    if planned["tool"] == "list_activities":
        return "【系统已识别】家长想了解有哪些活动，请概括列出，不要编造不存在的期次。"
    if planned["tool"] == "get_brand_info":
        return "【系统已识别】家长问的是品牌/主办方信息。"
    return ""
