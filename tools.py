"""
工具层：Agent 能调用的「外部能力」

设计要点 —— 工具粒度决定了 Agent 行为：
    如果把所有查询合成一个万能的 search(query)，模型会自己去猜关键词，
    很容易「凭印象」回答。

    拆成三个职责明确的工具后，模型必须先想清楚：
        「我要先知道有哪几期」  → list_activities()
        「我要某一期的详情」    → get_activity(id)
        「我要通用规则」        → get_policies()
    这恰好把「先确认期次再回答」这条业务规则，落进了工具设计里。
"""

import json
import os
import re
from typing import Any, Dict, List

import config
import renderer
import store


# ---------------------------------------------------------------- 知识库加载
def load_kb() -> Dict[str, Any]:
    with open(config.DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


_KB = load_kb()


def _strip_internal(obj: Any) -> Any:
    """递归去掉以 _ 开头的内部字段（_missing / _inferred 是我们自己的注释，
    不需要占模型的上下文）。"""
    if isinstance(obj, dict):
        return {k: _strip_internal(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_internal(v) for v in obj]
    return obj


# ---------------------------------------------------------------- 三个工具
def list_activities(status: str = "全部") -> str:
    """列出所有期次的概要，可按状态筛选。

    Args:
        status: "全部" | "报名中" | "已结束"
    """
    items: List[Dict[str, Any]] = []
    for a in _KB["activities"]:
        if status != "全部" and a.get("status") != status:
            continue
        items.append({
            "id": a["id"],
            "session_label": a.get("session_label"),
            "title": a.get("title"),
            "status": a.get("status"),
            "date": a.get("date"),
            "weekday": a.get("weekday"),
            "time_range": a.get("time_range"),
            "location": a.get("location"),
        })
    return json.dumps(
        {"count": len(items), "activities": items,
         "known_gaps": _KB.get("missing_sessions", [])},
        ensure_ascii=False,
    )


def _normalize_activity_id(raw: str) -> str:
    """把模型给的各种写法统一成 Sxx。

    为什么需要这一步：不能指望模型每次都吐出标准格式。它可能给
    "S16"、"16"、"第16期"、"第十六期"。这里把能解析出的数字统一成 S{两位}。

    工具层做输入归一化，比在提示词里反复叮嘱模型「请用 Sxx 格式」可靠得多。
    """
    s = (raw or "").strip().upper()
    m = re.search(r"(\d+)", s)
    if m:
        return f"S{int(m.group(1)):02d}"
    return s


def get_activity(activity_id: str) -> str:
    """查询某一期的完整详情（价格、名额、招募对象、环节、报名与退费相关字段）。

    Args:
        activity_id: 期次编号，形如 "S16"、"S06"，也接受 "16"、"第16期"。
    """
    key = _normalize_activity_id(activity_id)
    for a in _KB["activities"]:
        if a["id"].upper() == key:
            return json.dumps(_strip_internal(a), ensure_ascii=False)
    return json.dumps(
        {
            "error": f"知识库中没有第 {activity_id} 期的记录",
            "hint": "请如实告诉家长没有这一期的记录，建议查看当期推文或群公告；不要用其他期次的信息代替",
            "available_ids": [a["id"] for a in _KB["activities"]],
        },
        ensure_ascii=False,
    )


def get_policies() -> str:
    """查询跨期通用的规则：集合地点、出行方式、退费政策、准时要求、自带物品等。"""
    return json.dumps(_strip_internal(_KB["policies"]), ensure_ascii=False)


def get_brand_info() -> str:
    """查询品牌信息：山海科普研学是什么、主办承办单位、合作机构、活动性质。"""
    return json.dumps(_strip_internal(_KB["brand"]), ensure_ascii=False)


def check_missing_info(activity_id: str = "") -> str:
    """查某一期还缺哪些信息。

    客服问「这次活动还缺什么」「还差什么没填」时用。不给期次就查正在报名的那期。
    返回值分两块：
      missing_required —— 对外发文案前必须补齐的字段（时间/地点/名额/价格等）
      missing_manual   —— 系统给不了、只能人工处理的（导航照片、真实亮点、回顾链接）
    """
    if activity_id:
        aid = _normalize_activity_id(activity_id)
    else:
        aid = store.active_session_id() or ""

    activity = renderer.load_activity(aid) if aid else None
    if not activity:
        return json.dumps(
            {"error": f"没有找到 {activity_id or '正在报名的活动'} 的数据",
             "hint": "可用 list_activities 查看存在哪些期次"},
            ensure_ascii=False,
        )

    missing = store.missing_fields(activity)
    return json.dumps(
        {
            "activity_id": activity["id"],
            "activity_title": activity.get("title"),
            "status": activity.get("status"),
            "ready_to_send": len(missing) == 0,
            "missing_required": missing,
            "missing_manual": [
                {"item": "集合点导航照片", "who": "向拍摄的同学收取", "when": "活动前一天白天之前"},
                {"item": "活动真实亮点", "who": "客服在活动结束后撰写", "when": "活动结束当晚"},
                {"item": "回顾推文链接", "who": "推文发布后补", "when": "活动后 1-2 天"},
            ],
            "rule": (
                "missing_required 为空说明文案可以完整发出；不为空则必须告诉客服"
                "具体缺哪几项、需要向活动负责人确认。禁止用推测值填补。"
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- 流程类（SOP）
def _load_processes() -> Dict[str, Any]:
    path = config.DATA_FILE.parent / "processes.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_campus_booking_flow() -> str:
    """查询进校园预约提醒的【定稿文案】与发送要求。

    设计要点：
      1. 返回的是 output_mode="verbatim" 的成品文案，调用方必须原文输出，不得改写。
      2. 被访人姓名/电话属个人信息，不写进仓库，改从 .env 注入；
         .env 缺失时**不报错**，而是留下占位符并给出配置告警，保证公开仓库克隆后仍能运行。
      3. {{反馈时点}} 故意不自动填：它取决于建群时间，必须由客服人工判断后替换，
         所以保留占位符并在 handler_tips 里说明改法。
    """
    data = _load_processes()["campus_booking"]
    payload = _strip_internal(data)

    name = os.getenv("VISIT_TARGET_NAME", "").strip()
    phone = os.getenv("VISIT_TARGET_PHONE", "").strip()

    if not name or not phone:
        payload["_config_warning"] = (
            "未配置被访人姓名或电话（.env 中的 VISIT_TARGET_NAME / VISIT_TARGET_PHONE）。"
            "发给家长前必须手工补全，否则家长无法完成预约；这不影响程序运行。"
        )

    def _fill(text: str) -> str:
        if name:
            text = text.replace("{{被访人姓名}}", name)
        if phone:
            text = text.replace("{{被访人电话}}", phone)
        return text

    for item in payload.get("send_package", {}).get("items", []):
        if item.get("text"):
            item["text"] = _fill(item["text"])

    payload["_usage_constraint"] = (
        "这是定稿文案，必须【原文输出】send_package.items 里的 text，禁止改写、增删、润色、调整顺序。"
        "其中 type=manual_send_required 的条目系统无法自动提供，必须醒目提醒客服自己去发"
        "（尤其是「海南大学」公众号名片——系统没有该链接）。"
        "同时必须把 handler_tips 里 must_do=true 和 must_modify=true 的条目提醒客服，"
        "文案中的 {{反馈时点}} 要按建群时间人工替换，不能照抄。"
    )
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- 工具清单（给模型看）
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_activities",
            "description": "列出所有研学活动的期次概要。当家长问「有哪些活动」「最近有什么」时先调用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["全部", "报名中", "已结束"],
                        "description": "按状态筛选，默认全部",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity",
            "description": "查询某一期活动的完整详情：时间、地点、招募对象、名额、价格档位、费用包含、活动环节、退费与注意事项。调用前必须先确定期次编号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": {
                        "type": "string",
                        "description": "期次编号，如 S16、S06（由 list_activities 返回）",
                    }
                },
                "required": ["activity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_policies",
            "description": "查询跨期通用规则：集合地点、出行方式、退费政策、迟到与缺席规定、需要自带什么。家长问「怎么去」「下雨怎么办」「能退费吗」「要带什么」时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_brand_info",
            "description": "查询山海科普研学这个品牌是什么、主办与承办单位、合作机构、是否公益性质。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_campus_booking_flow",
            "description": "查询进校园预约提醒的定稿文案与发送要求：预约步骤、被访人信息、必发截图、注意事项、以及必须人工修改的时点。家长问怎么进校、怎么预约、预约不上、进不去校门时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_group_messages",
            "description": "按某一期活动，把活动群文案模板里的占位符自动填上真实数据（期次、活动名、日期、时间、参与对象、名额等），输出可直接复制的成品文案，并列出仍需人工补充的项。客服要写活动群文案、群公告、活动提醒时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string", "description": "期次，如 S16 或 第十六期"},
                    "stage": {"type": "string", "description": "可选，只渲染某一阶段，如「前一天」；不传则渲染全部"},
                },
                "required": ["activity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_activity",
            "description": "把客服提供的活动信息写入知识库（如时间、集合地点、价格、名额、活动环节等）。客服在对话里补充了任何活动信息、或粘贴了推文让你录入时，必须调用它，不要只在回复里说「记住了」。会自动备份，并返回这一期还缺哪些字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string", "description": "期次，如 S16 或 第十六期"},
                    "updates": {
                        "type": "object",
                        "description": "要写入的字段字典。可用字段：session_label, title, subtitle, status, date(YYYY-MM-DD), weekday, time_range, duration_minutes, location, transport, meeting_point, meeting_time, target_audience, participant_format, capacity(数字), pricing(dict 含 tiers), fee_includes(列表), insurance, dress_code, segments(列表含 name/detail), highlights(列表), awards(列表), learning_outcomes(列表)",
                    },
                },
                "required": ["activity_id", "updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_missing_info",
            "description": "查某一期活动还缺哪些信息（时间、集合地点、集合时间、名额、价格、招募对象等）。客服问「还缺什么」「还差什么没填」「信息齐了吗」时调用。不给期次默认查正在报名那期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string", "description": "期次，如 S16；留空则查正在报名的那一期"},
                },
                "required": [],
            },
        },
    },
]

_DISPATCH = {
    "list_activities": list_activities,
    "get_activity": get_activity,
    "get_policies": get_policies,
    "get_brand_info": get_brand_info,
    "get_campus_booking_flow": get_campus_booking_flow,
    "check_missing_info": check_missing_info,
    "render_group_messages": lambda activity_id, stage=None: renderer.render_group_messages(activity_id, stage),
    "update_activity": store.update_activity,
}


def call_tool(name: str, arguments_json: str) -> str:
    """按名字执行工具。模型给的是 JSON 字符串，这里负责解析与容错。"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"参数不是合法 JSON: {arguments_json}"}, ensure_ascii=False)

    if not isinstance(args, dict):
        args = {}

    try:
        return fn(**args)
    except TypeError as e:
        return json.dumps({"error": f"参数不匹配: {e}"}, ensure_ascii=False)
    except Exception as e:  # 兜底，避免一个工具异常导致整个对话中断
        return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)
