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

import hashlib
import json
import re
import shutil
import time
from typing import Any, Dict, List, Optional

import config
import store


def _load_json(path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_activities() -> Dict[str, Any]:
    """读库。派生字段（星期/时长/期次编号）在这里补齐，上层直接用。"""
    kb = _load_json(config.DATA_FILE)
    kb["activities"] = [store.with_derived(a) for a in (kb.get("activities") or [])]
    return kb


def load_team_info() -> Dict[str, Any]:
    """团队级信息（被访人姓名/电话）。放在 activities.json 顶层，换期不丢。

    为什么不做成 config 常量：它不是部署参数，是业务信息。
    放在 .env 里的直接后果就是「界面里被塞进了 API 设置那一栏」——
    按存放位置分组，就会把不相关的东西凑到一起。
    """
    kb = _load_json(config.DATA_FILE)
    info = kb.get("team_info") or {}
    # 唯一来源就是这里。以前还从 .env 做兜底，结果出现了双源：
    # 有人改了 .env 却发现没生效（renderer 优先用 team_info）。
    # 单一来源比「多一层保险」重要 —— 改不到的地方等于坏的地方。
    return {
        "visit_target_name": str(info.get("visit_target_name") or "").strip(),
        "visit_target_phone": str(info.get("visit_target_phone") or "").strip(),
    }


def save_team_info(name: str, phone: str) -> None:
    """写回 activities.json 顶层（只动 team_info，其余原样）。"""
    kb = _load_json(config.DATA_FILE)
    info = dict(kb.get("team_info") or {})
    if name.strip():
        info["visit_target_name"] = name.strip()
    if phone.strip():
        info["visit_target_phone"] = phone.strip()
    kb["team_info"] = info
    with open(config.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)


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
_HIDDEN_KEYS = {"source", "why", "common_mistake", "schema_version", "applies_to",
                "output_mode_note", "applies_when_empty"}


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


# ---------------------------------------------------------------- 活动回顾推文
# 为什么单独存一份，而不是塞进 activities.json：
#   活动记录是**单活动模型**——办完一期就被新一期整条替换掉。
#   但回顾推文是那一期的历史资料，客服过两个月还会被问「第十五期回顾里写了什么」。
#   跟着活动记录走的话，换一期就没了。
#   所以回顾推文按期次存成单独的文件，只往后加，不跟着换期被覆盖。
REVIEW_DIR_REL = "articles/reviews"


def _safe_name(label: str) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(label or "")).strip("_")
    return s or "unknown"


def period_label(text: str) -> str:
    """从一段文字里认出「第X期」，统一成阿拉伯数字写法。认不出返回空串。

    为什么统一写法：客服可能写「第9期」也可能写「第九期」，
    文件名不一致就会存成两份，查的时候总漏一份。
    """
    m = re.search(r"第\s*([0-9]{1,3}|[零一二两三四五六七八九十]{1,4})\s*期", text or "")
    if not m:
        return ""
    n = store.session_number(m.group(0))
    return f"第{n}期" if n else ""


def _review_index() -> Any:
    return config.DATA_DIR / REVIEW_DIR_REL / "index.jsonl"


def save_review(text: str, label: str = "") -> Dict[str, Any]:
    """把活动回顾推文存起来（按期次一个文件，同一期重存先备份）。

    期次怎么定：
      ① 客服明确给了 label → 用它
      ② 推文正文里写了「第X期」→ 用正文里的（最可靠，粘的是往期也对得上）
      ③ 都没写 → 当当前这一期
    不做模型判断：期次弄错会把第十五期的回顾存成第十六期，以后查出来就是错的。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "回顾推文是空的"}
    act = get_activity() or {}
    lab = (label or period_label(text)
           or str(act.get("session_label") or act.get("id") or "unknown"))
    safe = _safe_name(lab)[:40]
    rel = f"{REVIEW_DIR_REL}/review_{safe}.md"
    path = config.DATA_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    replaced = path.exists()
    if replaced:
        # 文件名要带内容哈希：只靠到「秒」的时间戳，同一秒内保存两次会互相覆盖，
        # 备份就静默丢了一份（这个坑是测试跑太快撞出来才发现的）。
        digest = hashlib.md5(path.read_bytes()).hexdigest()[:8]
        bak = (config.DATA_DIR / "backups"
               / f"review.{safe}.{time.strftime('%Y%m%d-%H%M%S')}.{digest}.md")
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bak)
    path.write_text(text + "\n", encoding="utf-8")

    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    idx = _review_index()
    with open(idx, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session_label": lab,
            "path": rel,
            "chars": len(text),
            "first_line": first[:60],
        }, ensure_ascii=False) + "\n")
    return {"ok": True, "label": lab, "rel": rel, "chars": len(text),
            "replaced": replaced}


def load_review_by_label(label: str) -> str:
    """按期次读回顾推文原文。先试同名文件，再按期次号匹配（容错「第9期/第九期」）。"""
    path = config.DATA_DIR / REVIEW_DIR_REL / f"review_{_safe_name(label)[:40]}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        pass
    want = store.session_number(label)
    if want is None:
        return ""
    for it in list_reviews():
        if store.session_number(it.get("session_label")) == want:
            try:
                return (config.DATA_DIR / str(it.get("path"))).read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def load_review(activity: Optional[Dict[str, Any]] = None) -> str:
    """当前这一期的回顾推文原文（没有就返回空串）。"""
    act = activity if activity is not None else get_activity()
    lab = str((act or {}).get("session_label") or (act or {}).get("id") or "")
    return load_review_by_label(lab) if lab else ""


def list_reviews() -> List[Dict[str, Any]]:
    """已存档的回顾推文（同一期只留最新一条），按期次从新到旧。"""
    idx = _review_index()
    if not idx.exists():
        return []
    latest: Dict[str, Dict[str, Any]] = {}
    for ln in idx.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            it = json.loads(ln)
        except Exception:
            continue
        lab = str(it.get("session_label") or "")
        if lab:
            latest[lab] = it
    return sorted(latest.values(),
                  key=lambda x: store.session_number(x.get("session_label")) or 0,
                  reverse=True)


_REVIEW_MARKS = ("回顾", "精彩瞬间", "圆满结束", "现场照片", "活动花絮", "精彩回顾")
_RECRUIT_MARKS = ("报名", "招募", "扫码", "名额", "抢位", "填写报名")


def decide_article_kind(text: str, model_kind: str) -> str:
    """模型判的 article_kind，再用代码旁证纠一次。

    为什么要有这一道：
        判成「推广推文」会走 save_article，**直接把当期推文覆盖掉**；
        判成「回顾推文」只是多存一个文件，没有破坏性。两条路的代价差很大，
        所以拿两个反向硬标记做否决：
          · 像回顾（≥2 个回顾标记）且完全没报名信息 → 一定是回顾
          · 像招募（≥2 个报名标记）且完全没回顾标记 → 一定是推广
        证据不明显时仍尊重模型的判断，不要拿关键词表去替代它。
    """
    t = text or ""
    r = sum(1 for m in _REVIEW_MARKS if m in t)
    p = sum(1 for m in _RECRUIT_MARKS if m in t)
    if r >= 2 and p == 0:
        return "review"
    if p >= 2 and r == 0:
        return "promo"
    return model_kind


def review_url(text: str) -> str:
    """从回顾推文原文里找出公众号链接（能找到就自动填进「回顾推文链接」）。"""
    urls = re.findall(r"https?://[^\s\u3001\uff0c\uff09\)\]】<>》\"']+", text or "")
    for u in urls:
        if "mp.weixin.qq.com" in u:
            return u
    return urls[0] if urls else ""


def _drop_inapplicable(policies: dict, act: dict) -> dict:
    """丢掉「只在某个字段为空时才成立」的规则。

    真实踩过的坑（用户报的）：
        库里集合点已经填成「海南大学海洋生物标本馆」，
        但 meeting_point_policy 那句「集合点每期不同，确定了再发」照样进了上下文，
        模型照抄，于是答出「地点在海洋生物标本馆。集合点每期不一样，定了我发您」——
        一句话里自相矛盾，家长直接蒙。

    根因不是模型笨，是**我们给它看了一条当下已经不成立的规定**。
    所以规则本身要带条件：在数据里写 applies_when_empty: meeting_point，
    该字段一旦有值，这条就不进上下文。
    """
    out = {}
    for k, v in (policies or {}).items():
        guard = v.get("applies_when_empty") if isinstance(v, dict) else None
        if guard and act.get(guard) not in (None, "", [], {}):
            continue
        out[k] = v
    return out


def build_context(activity_id: Optional[str] = None, question: str = "") -> str:
    """拼出「当前活动 + 通用规则 + 流程 + 文案模板」的完整上下文。

    单活动模型下不需要区分期次：资料里永远只有一条活动，就是正在办的那一期。

    question：客服/家长这次问的原话。只用于**多带一份往期回顾原文**——
        客服明确点到期次（「第十五期回顾里写了什么」）时，就把那一期回顾也放进来。
        这不是意图分类，只是字符串匹配，失败也只是多给一份资料，不会答错。
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
            "其余内容（环节、机制、奖项、收获、活动定位、报名方式、退费规则、自备物品）以原文为准。\n"
            "⚠️ 推文末尾常出现「文丨」「图丨」「审核丨」「编辑丨」「排版丨」加一个人名，"
            "那是公众号的排版署名，**不是活动信息**。绝不能据此判断谁是带队老师、"
            "谁是主讲人、谁负责现场——原文没明说的地方，一律回答「我确认一下再回您」。\n\n"
            + article
        )
    else:
        parts.append(
            "【这一期的推文原文】还没有录入。家长问到活动环节、内容、亮点这类问题时，"
            "如实说「这一期的详细内容我这边还没有整理，建议看当期推文」，不要编。"
        )

    # 活动回顾推文：活动办完之后才有，讲的是**已经发生**的事。
    # 它单独存一个文件（articles/reviews/review_<期次>.md），不跟着换期被覆盖。
    review = load_review(act)
    if review:
        parts.append(
            "【这一期的活动回顾推文原文】（活动已办完，讲的是现场实际发生的事）\n"
            "家长问「那天孩子们做了什么」「现场怎么样」这类问题时，以这份为准。\n\n"
            + review
        )

    # 往期回顾：活动记录换期会被整条替换，但回顾推文按期次长期留着，
    # 这就是它的用处——客服问「上一期回顾里怎么写的」，还得能查回来。
    revs = list_reviews()
    if revs:
        labels = "、".join(str(r.get("session_label") or "") for r in revs[:12])
        parts.append("【已存档的活动回顾推文】" + labels +
                     "\n（已经存下的期次。客服明确点到期次时，那一期的原文会自动附在下面。）")
        cur_label = str((act or {}).get("session_label") or "")
        for r in revs:
            lab = str(r.get("session_label") or "")
            if not lab or lab == cur_label or lab not in (question or ""):
                continue
            text = load_review_by_label(lab)
            if text:
                parts.append(f"【往期（{lab}）的活动回顾推文原文】\n{text}")
                break            # 一次只带一期，别把窗口撑爆

    # 顺序很重要：先按条件过滤（要用 applies_when_empty 这个开关），再 strip 掉内部字段。
    # 反过来写的话，_strip 会先把开关删了，过滤器就永远判不出该不该丢（这个顺序错过一次）。
    _policies = _strip(_drop_inapplicable(kb.get("policies", {}), act or {}))
    parts.append("【跨期通用规则】\n" + json.dumps(_policies, ensure_ascii=False, indent=1))
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


def context_size(activity_id: Optional[str] = None, question: str = "") -> int:
    """上下文有多少字，用来盯着别把窗口撑爆。"""
    return len(build_context(activity_id, question))
