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
import re
import shutil
import time
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


def load_article(activity: Optional[Dict[str, Any]] = None) -> str:
    """读这一期的推文原文。

    为什么存原文而不是拆字段：
        推文是高自由度的叙述文字，每次的活动内容、排版、说法都不一样。
        把它硬拆成「环节1/环节2/亮点/收获」这种固定字段，是在用不存在的结构去框它，
        每次新推文都要重新拆一遍，而且拆的过程一定会丢东西（措辞、语气、逻辑）。
        推文本身才两千多字，直接整篇给模型看，比拆成字段更准也更省事。

    只有**固定字段**（价格、名额、日期、集合点、室内外、分组、每组人数）
    需要结构化存储 —— 因为代码要拿它们做校验和计算，不能有歧义。
    """
    act = activity if activity is not None else get_activity()
    rel = (act or {}).get("article")
    if not rel:
        return ""
    path = config.DATA_DIR / rel
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


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
    """当前活动的 id。

    存储模型是**单活动**：文件里永远只有一条，就是正在办的那一期。
    办完了直接覆盖成新的一期，往期放在 data/archive/ 只留档、不再查询。
    之所以还留着 id，只是为了备份文件名和日志好看。
    """
    acts = load_activities().get("activities", [])
    return acts[0]["id"] if acts else None


def get_activity(activity_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """当前活动。activity_id 参数保留只为兼容旧调用，实际一律返回当前那一条。"""
    acts = load_activities().get("activities", [])
    return acts[0] if acts else None


def save_article(text: str, activity: Optional[Dict[str, Any]] = None) -> str:
    """把客服粘进来的推文整篇存下来（覆盖同一期旧的，旧版先备份）。

    返回存到哪个相对路径。
    """
    act = activity if activity is not None else get_activity()
    rel = (act or {}).get("article") or f"articles/current.md"
    path = config.DATA_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = config.DATA_DIR / "backups" / f"article.{time.strftime('%Y%m%d-%H%M%S')}.md"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bak)
    path.write_text(text, encoding="utf-8")
    return rel


def archive_article(label: str = "", activity: Optional[Dict[str, Any]] = None) -> str:
    """把**当前这一期**的推文原文按「期次」名字存进 archive/。

    为什么不能只靠 save_article 的自动备份：
        current.md 这个文件名不带期次，备份文件名又是时间戳。
        换了一期之后想找回上一期的推文，得一个个打开备份猜。
        这里直接存成 archive/article_第十六期.md，一眼就能找到。

    返回相对路径（没内容就返回空串）。
    """
    act = activity if activity is not None else get_activity()
    text = load_article(act)
    if not text.strip():
        return ""
    name = str(label or (act or {}).get("session_label") or (act or {}).get("id") or "past")
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", name).strip("_") or "past"
    rel = f"archive/article_{safe[:40]}.md"
    path = config.DATA_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def build_context(activity_id: Optional[str] = None) -> str:
    """拼出「当前活动 + 通用规则 + 流程 + 文案模板」的完整上下文。

    单活动模型下不需要区分期次：资料里永远只有一条活动，就是正在办的那一期。
    """
    kb = load_activities()
    act = get_activity()

    parts: List[str] = []

    if act is None:
        parts.append("【当前活动】知识库里还没有活动记录。"
                     "家长问到具体数据时，如实说「这一期我这边没有记录」，不要编。")
    else:
        parts.append(
            f"【当前活动】{act.get('session_label') or act.get('id')}"
            f"「{act.get('title', '')}」（{act.get('status', '')}）\n"
            "这是当前唯一在办的活动，家长的问题默认都是问它。**不需要向家长确认是哪一期。**\n"
            + json.dumps({k: v for k, v in _strip(act).items() if k != "article"},
                         ensure_ascii=False, indent=1)
        )

    # 推文原文：活动内容（环节、机制、奖项、收获、定位、报名方式、退费、自备物品）全在这里
    article = load_article(act)
    if article:
        parts.append(
            "【这一期的推文原文】—— 活动内容以这份原文为准，不要另外编。\n"
            "注意：价格、名额、日期时段、集合点、室内外、分组、每组人数这几项"
            "以【当前活动】里的固定字段为准（那是客服确认过、可能已更新的）；"
            "其余内容（环节、机制、奖项、收获、活动定位、报名方式、退费规则、自备物品）以原文为准。\n\n"
            + article
        )
    else:
        parts.append(
            "【这一期的推文原文】还没有录入。家长问到活动环节、内容、亮点这类问题时，"
            "如实说「这一期的详细内容我这边还没有整理，建议看当期推文」，不要编。"
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
