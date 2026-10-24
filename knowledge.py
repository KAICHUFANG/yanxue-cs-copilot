"""
知识装配层：把「当前活动」需要知道的全部资料拼成一段上下文

为什么要有这一层（这是 v3 架构的核心）
------------------------------------
v2 的做法是「问一句 → 猜该查什么 → 调工具」。它依赖两件事：
  ① 从这句话里解析出「第十六期」这样的期次
  ② 用关键词表判断这是价格问题还是政策问题

两件都是假的：
  · 真实家长提问里**没有一条**会写「第十六期」。群就是第十六期的群，
    家长只会问「多少钱」「几点集合」。翻遍真实聊天记录，期次出现率是 0%。
  · 判断「该查哪份资料」这件事，本该由界面上的「当前活动」决定，
    而不是从一句自然语言里猜。

所以 v3 把期次从「猜」改成「给定」：

    当前活动 = S16（界面上选中的那一期，或群绑定）
        ↓
    knowledge.build_context()  把 S16 的数据 + 通用规则 + 流程 + 文案模板拼成一段
        ↓
    模型直接读这段上下文回答（没有工具、没有意图分类、没有期次正则）

写入、文案生成、缺口体检这些**确定性功能**不经过模型：
    客服点按钮 / 敲命令 → renderer / store 直接执行。
    模型只干一件事：用给定资料回答家长的自然语言问题。
"""

import json
from typing import Any, Dict, List, Optional

import config


def _load_json(path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_activities() -> Dict[str, Any]:
    return _load_json(config.DATA_FILE)


def load_processes() -> Dict[str, Any]:
    return _load_json(config.DATA_DIR / "processes.json")


def load_group_messages() -> Dict[str, Any]:
    return _load_json(config.DATA_DIR / "group_messages.json")


# 这些键是给程序/审计看的，不该塞进模型上下文：
#   _ 开头的是内部溯源字段（_status_basis、_meeting_point_note…）
#   下面几个是写给我自己看的判断依据，模型读了反而会照着「讲道理」
_HIDDEN_KEYS = {"source", "why", "common_mistake", "schema_version", "applies_to", "output_mode_note"}


def _strip(obj: Any) -> Any:
    """去掉内部的溯源字段，只留下模型需要的事实。"""
    if isinstance(obj, dict):
        return {
            k: _strip(v)
            for k, v in obj.items()
            if k not in _HIDDEN_KEYS and not k.startswith("_")
        }
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def current_activity_id() -> Optional[str]:
    """当前活动是哪一期。

    优先级：
      1. 显式指定（界面选中 / 命令行参数）
      2. 「报名中」的那一期 —— 客服服务的就是当期
    注意：这里**不从问题里猜**。猜期次是 v2 的病根。
    """
    acts = load_activities().get("activities", [])
    for a in acts:
        if a.get("status") == "报名中":
            return a["id"]
    return acts[0]["id"] if acts else None


def get_activity(activity_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    aid = activity_id or current_activity_id()
    for a in load_activities().get("activities", []):
        if a["id"] == aid:
            return a
    return None


def build_context(activity_id: Optional[str] = None) -> str:
    """拼出「当前活动 + 通用规则 + 流程 + 文案模板」的完整上下文。"""
    kb = load_activities()
    aid = activity_id or current_activity_id()
    act = get_activity(aid)

    parts: List[str] = []

    if act is None:
        parts.append(f"【当前活动】知识库里没有 {aid} 这一期的记录。"
                     "家长问到具体数据时，如实说「这一期我这边没有记录」，不要用别的期次顶替。")
    else:
        parts.append(
            f"【当前活动】{act.get('session_label') or act.get('id')}"
            f"「{act.get('title', '')}」（{act.get('status', '')}）\n"
            "家长就在这一期的群里，问题默认都是问这一期。**不需要向家长确认是哪一期。**\n"
            + json.dumps(_strip(act), ensure_ascii=False, indent=1)
        )

    # 全部期次的概要一直放进来。
    # 家长偶尔会问「你们办过哪些活动」，v3 没有工具可以去「查」，那就让资料一直在场。
    # 概要只有几十字一条，比为了这一个问题保留一整套路由便宜得多。
    brief = []
    for a in kb.get("activities", []):
        brief.append(
            f"· {a.get('session_label') or a.get('id')}「{a.get('title', '')}」"
            f" {a.get('date', '')} {a.get('weekday', '')} —— {a.get('status', '')}"
        )
    parts.append(
        "【全部期次概要】回答「办过哪些活动」时用这个，不要编造没列出来的期次。\n"
        + "\n".join(brief)
    )

    parts.append("【跨期通用规则】\n" + json.dumps(_strip(kb.get("policies", {})), ensure_ascii=False, indent=1))
    parts.append("【品牌信息】\n" + json.dumps(_strip(kb.get("brand", {})), ensure_ascii=False, indent=1))
    parts.append("【流程与定稿文案】\n" + json.dumps(_strip(load_processes()), ensure_ascii=False, indent=1))
    parts.append("【活动群文案模板】\n" + json.dumps(_strip(load_group_messages()), ensure_ascii=False, indent=1))

    parts.append(
        "【回答要求】\n"
        "· 只答家长问的那部分，不要复述整份资料。\n"
        "· 资料里没有的，如实说「这一期我这边没有记录，建议看当期推文或群公告」，禁止推测、禁止编造。\n"
        "· 带 text 字段的是发给家长的原话，要逐字原样输出，保留 emoji、换行和 {{占位符}}。\n"
        "· 涉及价格、名额、退费时，末尾加一句「以当期推文为准」。"
    )
    return "\n\n".join(parts)


def context_size(activity_id: Optional[str] = None) -> int:
    """上下文有多少字，用来盯着别把窗口撑爆。"""
    return len(build_context(activity_id))
