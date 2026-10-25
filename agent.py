"""
Agent：一句话进来 → 判断是「补充信息 / 要文案 / 提问」→ 执行 → 一句人话出去

存储模型是**单活动**：永远只有「当前正在办的那一期」，办完覆盖成新的一期。
所以这里没有任何期次判断——不存在「该查第几期」这个问题。

三层分工（这个项目的核心结论）：

    模型负责理解      这句话是补充信息、要文案，还是在提问？
    代码负责纪律      字段白名单、类型校验、写前备份（都在 store.py）
    代码负责定稿      文案模板填充必须原文输出，不让模型润色（renderer.py）

为什么不用关键词匹配：试过，被反复证伪。
  · 「室内有空调吗」被当成补字段，回成「这期是室内还是室外？」
  · 「这次活动不是室内的」被判成「室内」，正好反了
  · 「如果下雨就转室内」被当成赋值
  · 「不分组」被否定词黑名单误杀
每修一次都要加一条规则，说明「这句话在说什么」本来就是语义问题。
规则只能覆盖字面，覆盖不了意义。判断归模型，纪律归代码。
"""

import json
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI

import config
import knowledge
import renderer
import store

_client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)

_EMPTY = (None, "", [], {})


def _usage(u) -> Dict[str, int]:
    return {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0}


def _merge(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    return {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0) for k in
            ("prompt_tokens", "completion_tokens", "total_tokens")}


# ---------------------------------------------------------------- 问答
_CTX_SYSTEM = """你是「山海科普研学」的家长咨询助手，同时也是客服的工作搭档。

你没有活动数据在脑子里。下面会给你当前这一期的全部资料，事实一律以资料为准，
不准编造数字、日期、名额、老师姓名。

查不到就说「这一期我这边没有记录，建议看当期推文或群公告」，
绝不用别的期次顶替，也不要编一个听起来合理的原因。

**分清楚对面是谁**：
· 家长问问题 → 正常回答
· 客服只说了一个字段名、没给值（例如就打「集合地点」「室内室外」）→ 她多半是想填这一项，
  主动问一句「您是要填这一项吗？告诉我具体内容就行」，不要当成家长在提问

**回复要短**：一到三句，先给结论。缺口由界面右侧实时显示，不要在回复里罗列缺项。

## 你手上有什么（被问到就照实说，别含糊）

· **这一期的推文原文（全文）** —— 活动定位、活动内容、环节机制、研学收获、专业支持、
  费用与退费、自备物品、报名方式，全在里面
· **一组固定字段** —— 价格档位、名额、日期时段、地点、集合地点与时间、招募对象、
  参与形式、室内外、是否分组、每组人数、费用包含

你**没有**：
· 推文里的图片 —— 报名二维码和联系电话在原文里是图片，所以你确实没有报名链接
· 家长名单

**两处说法冲突时，按这个优先级**：
· 价格、名额、日期时段、集合点、室内外、分组、每组人数 → 以「固定字段」为准（客服可能更新过）
· 环节、机制、奖项、收获、活动定位、报名方式、退费规则、自备物品 → 以推文原文为准

被问「能不能看到推文」时，正确答法是：
「推文全文我这儿有，图片看不到——报名二维码和电话在原文里是图。」
不要答成「看不到推文」，也不要只说「我只有资料」。

被问「你这些信息哪来的」时，答：活动内容来自这一期的推文原文（公众号「和美山海」），
价格名额这些固定信息是团队核对过的字段。

中文口语化，像熟悉的团队伙伴。"""


def answer_ctx(question: str, activity_id: Optional[str] = None,
               history: Optional[List[Dict[str, Any]]] = None,
               verbose: bool = False,
               stream_cb: Optional[Any] = None) -> Dict[str, Any]:
    """纯问答：把当前活动资料给模型，直接回答。不写库、不调用工具。

    stream_cb（流式回调）：传了就用 stream=True 逐字推给界面，边生成边显示。
    没传就是一次性拿完整结果（命令行、评测走这条）。
    """
    ctx = knowledge.build_context(activity_id)
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": _CTX_SYSTEM + "\n\n" + ctx}]
    for m in (history or [])[-6:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})

    if stream_cb is None:
        resp = _client.chat.completions.create(
            model=config.MODEL, messages=msgs, temperature=config.TEMPERATURE)
        if verbose:
            print(f"  📎 上下文 {len(ctx)} 字")
        return {"answer": resp.choices[0].message.content or "",
                "trace": ["<context>"], "usage": _usage(resp.usage)}

    # 流式：边生成边回调。注意流式拿不到 usage，token 统计按 0 记。
    stream = _client.chat.completions.create(
        model=config.MODEL, messages=msgs, temperature=config.TEMPERATURE, stream=True)
    parts: List[str] = []
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            parts.append(piece)
            stream_cb(piece)
    return {"answer": "".join(parts), "trace": ["<context>"],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


# ---------------------------------------------------------------- 输入分类
# 三类：update（补充信息）/ copy（要一段文案）/ question（提问）
#
# 只有这三类是因为**执行方式不同**，不是因为语义分类本身好玩：
#   update   → 走 store 落库（要有校验和备份）
#   copy     → 走 renderer 填模板（必须原文输出，不能让模型润色）
#   question → 模型直接读资料回答
# 判断交给模型，执行交给最合适的那个模块。

_CLASSIFY_SYSTEM = """你是客服助手的输入判断器。判断客服这句话属于哪一类。

A. update —— 在补充或修改当前活动的信息
   例：「室外」「不分组」「这期是室外活动」「集合点改到五教104」「每组4到5人」
       「不是室内，是室外」「室内还是室外：室外」「名额改成30人」
B. copy —— 要一段可以直接发出去的文案。要再分两类：
   B1. copy_type = "template"：活动群通知类文案（欢迎语、进校预约提醒、活动提醒、出发提醒、结束感谢）
       —— 这些有定稿模板，stage 填是哪一种
   B2. copy_type = "promo"：推广/招募类文案（发到别的群拉人报名、朋友圈、朋友推荐）
       —— 这些没有定稿，需要现写
C. question —— 在问问题
   例：「室内有空调吗」「几点集合」「这次活动是不是室内的」「报名怎么弄」

判断要点：
· 只给了一个词或一个值、没有疑问语气 → update
· 有否定或条件时，先正确理解含义再定值：
  「不是室内」→ 室外；「如果下雨就转室内」→ 这不是在改活动属性，按 question 处理
· **粘贴了一大段推文/公告原文**（里面有活动名、时间、地点、名额、费用等一条条具体信息）
  → 一律 update，把这些字段抽出来。**不要因为里面有「招募」两个字就当成 copy 去写文案**
· 「推广」「招募」「拉人」「朋友圈」「引流」「私域」→ 一律 copy + promo
  （前提是她在**要一段文案**，不是在**贴一份资料**）
· 「欢迎语」「群公告」「活动提醒」「结束感谢」→ copy + template，stage 填对应的那一段
· 只说「活动群文案」「群文案」「整个群的通知」但没说哪一段 → copy + template，
  **stage 填「全部」**（客服要的就是全流程，不要反问）
· 只说「写个文案」「来段文案」而没说清是活动群通知还是推广 → copy，但 **copy_type 留空**，
  让程序去反问客服，不要替她猜
· 拿不准的时候选 question（宁可多问一句，也不要乱写库）
· 只抽客服**明确说过**的字段，没说的一律不要填，不要编「校门口」「上午」这种默认值
· 客服只报了字段名、没给内容（「集合地点」「名额」）→ 不要当成 update，
  按 question 处理，并反问她要填什么

可选字段（只能用这些键名）：
__FIELDS__

值的要求：
· date 用 YYYY-MM-DD；capacity / duration_minutes 用数字；pricing 是 {"model":..,"tiers":[{"label":..,"price":数字}]}
· indoor_outdoor 只能是「室内」或「室外」这样的短语，不要写成一整段说明
· grouping 用「分组」或「不分组」；group_size 用「4-5人」这类短语
· 其余是字符串

另外单独判断一个开关 is_article：
· true = 客服**粘进来一篇文章/推文/公告原文**（几百字以上，是叙述性文章，
  有小标题、有段落、讲活动是干什么的）
· false = 其他所有情况

只输出 JSON：
{"kind": "update" | "copy" | "question", "is_article": true | false, "copy_type": "template" | "promo"（仅 copy 时用）, "promo_channel": "group" | "moments"（仅 promo 时用，见下）, "stage": "欢迎语/活动提醒/出发提醒/结束感谢；没说哪一段就填「全部」（仅 template 时用）", "updates": {字段: 值}, "reason": "一句话说明依据"}

promo_channel 怎么填：
· 提到「朋友圈」「盆友圈」「pyq」「圈里」「动态」→ moments
· 提到「群」「私域」「家长群」「发到群里」或没说清 → group
· 「推广文案」「招募文案」但没指定渠道 → group
"""

# 文案阶段 → group_messages.json 里 timeline 的圈号。
# 渲染器的 stage 参数是按「圈号是不是子串」筛选的，所以这里只给圈号。
_STAGE_HINT = [
    ("欢迎", "①"), ("拉群", "①"), ("建群", "①"),
    ("预约", "②"), ("进校", "②"),
    ("提醒", "③"), ("前一天", "③"),
    ("出发", "④"), ("当天", "④"),
    ("结束", "⑤"), ("感谢", "⑤"), ("回顾", "⑤"),
]


def _field_menu() -> str:
    act = knowledge.get_activity() or {}
    lines = []
    for k, desc in store.ALLOWED_FIELDS.items():
        cur = act.get(k)
        cur_txt = "（空）" if cur in _EMPTY else "（现值：" + str(cur)[:24] + "）"
        lines.append(f"- {k}：{desc}{cur_txt}")
    return "\n".join(lines)


def classify_input(question: str, activity_id: Optional[str] = None) -> Dict[str, Any]:
    """让模型判断这句话属于 update / copy / question。"""
    resp = _client.chat.completions.create(
        model=config.MODEL,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYSTEM.replace("__FIELDS__", _field_menu())},
            {"role": "user", "content": question},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or ""
    try:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}

    kind = data.get("kind")
    if kind not in ("update", "copy", "question"):
        kind = "question"          # 判不出来就当提问，绝不瞎写
    copy_type = data.get("copy_type")
    if copy_type not in ("template", "promo"):
        copy_type = ""
    channel = data.get("promo_channel")
    if channel not in ("group", "moments"):
        channel = "group"          # 没说清就是发群
    return {
        "kind": kind,
        "is_article": bool(data.get("is_article")),
        "copy_type": copy_type,
        "promo_channel": channel,
        "updates": data.get("updates") if isinstance(data.get("updates"), dict) else {},
        "stage": data.get("stage") or "",
        "reason": data.get("reason", ""),
        "usage": _usage(resp.usage),
    }


# ---------------------------------------------------------------- 写入
def _field_words() -> set:
    """字段名本身（含常见叫法）。用来识别「把字段名当值写进去」这种错误。"""
    words = set()
    for k, desc in store.ALLOWED_FIELDS.items():
        words.add(k)
        words.add(desc.split("，")[0].split("（")[0].strip())
    words |= {"室内室外", "室内/室外", "是否分组", "分组吗", "每组几个人"}
    return {w for w in words if w}


def _valid_updates(updates: Dict[str, Any]):
    """代码只做校验：字段必须在白名单里，值不能是字段名本身。判断归模型，纪律归代码。

    第二条是真踩过的坑：客服只打了「集合地点」四个字，模型抽成
    {meeting_point: "集合地点"} 并写进了库——把字段名当成了字段值。
    这类错误一眼能看出是垃圾数据，让代码挡掉，不要指望模型每次都识别对。
    """
    field_words = _field_words()
    good, bad = {}, []
    for k, v in (updates or {}).items():
        if k not in store.ALLOWED_FIELDS:
            bad.append(f"未知字段 {k}")
        elif v in _EMPTY:
            bad.append(f"{k} 是空值")
        elif str(v).strip() in field_words:
            bad.append(f"{k} 收到的是字段名「{v}」，不是具体内容")
        else:
            good[k] = v
    return good, bad


def _confirm_written(question: str, updates: Dict[str, Any],
                     ok: bool, err: str, extra: str = "") -> Dict[str, Any]:
    """写完（或写失败）之后，让模型用一句人话回复客服。

    注意：**不把缺项清单喂进去**。
    实测过：一旦告诉它「还缺哪几项」，它每条回复都要罗列一遍，客服看得烦。
    缺项由界面右侧的面板显示，那里是程序实时算的，比模型说一遍更准。
    """
    note = ("、".join(f"{k}={v}" for k, v in updates.items()) if ok
            else f"写入失败，原因是：{err}")
    prefix = "写入成功的字段：" if ok else ""
    resp = _client.chat.completions.create(
        model=config.MODEL,
        messages=[
            {"role": "system", "content":
             "你在回复客服（不是家长）。用一到两句话说明这次录入的结果。\n"
             "写成功就简短确认写了什么；写失败就说清为什么。\n"
             "**不要罗列还缺哪些字段**，界面右侧有实时面板，重复一遍只会干扰。"},
            {"role": "user", "content": ("客服说的话：" + question
                                        + "\n\n系统实际情况：\n" + prefix + note
                                        + (("\n" + extra) if extra else ""))},
        ],
        temperature=0,
    )
    return {"answer": resp.choices[0].message.content or "",
            "trace": ["classify", "update_activity"], "usage": _usage(resp.usage)}


def write_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    """把字段写进当前活动。薄薄一层，真正干活的是 store（校验+备份）。"""
    return store.update_activity(None, updates)


# ---------------------------------------------------------------- 文案
def render_copy(stage_text: str, all_when_empty: bool = True) -> str:
    """按阶段渲染群文案。模板里的 {{占位符}} 由代码填，保证原文输出。

    渲染器返回的是 JSON（给程序用），这里格式化成客服能直接复制发群的文本。
    格式化这一步也必须由代码做：让模型去「排版」就等于把定稿交给它改写。

    all_when_empty：认不出阶段时，是否把 5 个阶段全部输出。
        界面的「生成全部群文案」按钮传 True（它就是要全部）；
        对话框路径必须传 False —— 否则「写推广文案」会被渲染成「欢迎语」，
        因为 stage=None 时渲染器从第一个阶段开始输出。这是真实报过的 bug。
    """
    key = None
    for word, no in _STAGE_HINT:
        if word in (stage_text or ""):
            key = no
            break
    if key is None and not all_when_empty:
        return ""

    raw = renderer.render_group_messages(knowledge.current_activity_id(), stage=key)
    try:
        data = json.loads(raw)
    except Exception:
        return raw

    lines: List[str] = []
    for m in data.get("messages", []):
        if m.get("text"):
            lines.append(m["text"].strip())
            if m.get("unfilled"):
                lines.append("（上面还有需要人工补的："
                             + "、".join(m["unfilled"]) + "）")
        elif m.get("manual"):
            lines.append(f"【需要你手动发】{m.get('label', '')}：{m.get('action', '')}")
    if not lines:
        return ""

    out = "\n\n————\n\n".join(lines)
    left = data.get("unfilled_summary") or []
    if left:
        out += "\n\n⚠ 还需人工补：" + "、".join(left)
    return out


# 活动群那 5 个阶段，认不出来时拿它反问客服
STAGE_NAMES = "① 拉群欢迎语　② 进校预约提醒　③ 活动提醒（含集合点）　④ 出发提醒　⑤ 结束感谢"

# 推广文案：这类不是定稿，本来就是要「写」，所以交给模型。
# 但事实必须来自资料——不能让模型自己编优惠、编名额紧张。
# 推广文案的**共同约束**：事实只能用资料里有的。写法和长度按渠道分。
_PROMO_FACTS = """事实只能用【资料】里有的：活动名、主题、时间、地点、招募对象、名额、价格、费用包含、活动环节与亮点。

**严禁编造**：
· 不写资料里没有的优惠、赠品、折扣、早鸟价
· 不写「仅剩X个名额」「最后一天」这类紧迫感，除非资料明确写了名额上限（那就照实说「25人报满即止」）
· 不写往期好评、获奖、合作机构、老师头衔
· 不写进校预约、集合点这些**报名之后**才需要的信息
"""

# 微信群：屏幕能滚，可以分点讲清楚，家长会认真读完
_PROMO_GROUP = "你在给「山海科普研学」写**微信群推广文案**：发到别的家长群里，吸引报名。\n\n" + _PROMO_FACTS + """
写法要求：
· **每版 150-250 字**，口语化，像家长之间真心推荐，不要像广告
· 开头一句钩子；中间 2-3 个具体卖点，必须用活动里真实的环节，不要写「寓教于乐」这种空话
· 结尾引导：看推文扫码报名 / 私信我
· 给两版：先写「A 稳重版」，再写「B 活泼版」

直接输出两版文案，不要解释，不要写「以下是」。
"""

# 朋友圈：超过 6 行会被折叠成「全文」，所以必须短，而且不要分点排版
_PROMO_MOMENTS = "你在给「山海科普研学」写**朋友圈文案**：发在自己的朋友圈，吸引家长报名。\n\n" + _PROMO_FACTS + """
写法要求（朋友圈有硬限制，必须遵守）：
· **每版 60-100 字，最多 5 行**。超过 6 行会被折叠成「全文」，家长根本不会点开
· **不要分点、不要小标题、不要罗列**。朋友圈是一段话，不是公告
· 结构就用三句：一句钩子 + 一句具体信息（时间、对象、价格挑一两个说，不要全塞）+ 一句行动（扫码看推文 / 私信我）
· 可以用 1-2 个表情，不要堆
· 给两版：先写「A 简洁版」，再写「B 亲切版」

直接输出两版，不要解释，不要写「以下是」。
"""

_PROMO_BY_CHANNEL = {"moments": _PROMO_MOMENTS, "group": _PROMO_GROUP}


def write_promo(question: str, activity_id: Optional[str] = None,
                channel: str = "group") -> Dict[str, Any]:
    """生成推广/招募文案。事实来自资料，写法和长度按渠道定。

    channel:
        group   微信群 —— 150-250 字，可以分点
        moments 朋友圈 —— 60-100 字、最多 5 行，超过 6 行会被折叠
    """
    ctx = knowledge.build_context(activity_id)
    system = _PROMO_BY_CHANNEL.get(channel, _PROMO_GROUP)
    resp = _client.chat.completions.create(
        model=config.MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"【资料】\n{ctx}\n\n【客服要求】\n{question}"},
        ],
        temperature=config.TEMPERATURE,
    )
    return {"answer": resp.choices[0].message.content or "",
            "trace": ["classify", "write_promo"],
            "usage": _usage(resp.usage)}


# ---------------------------------------------------------------- 给客服的内部提示
# 这一层不发给家长，所以不受「150 字、口语化」的限制。
# 它的价值：把「我不知道」变成「你该怎么做」——缺的那几项该问谁、这条回复有什么风险、
# 哪些东西必须客服亲自动手发。
#
# 注：这块一度被误删（改文案提示词时整段替换掉了），而 internal_notes 里的 try/except
# 把 NameError 吞成了空列表，所以界面只是"没有提示"，看不出报错。加变量前先确认它真的在。
_INTERNAL_SYSTEM = """你在给客服写内部备注，这段**不会发给家长**。

基于资料和这次的回复，写 2-4 条给客服的提示，每条一行，只在确实需要时写：
- 资料里缺哪一项、建议现在去问谁（负责人 / 策划组 / 老师）
- 这条回复有没有风险（承诺过头、可能被追问、口径不确定）
- **有没有需要客服亲自手动发的东西** —— 报名二维码、公众号名片、集合点导航照片，
  这些系统发不了，必须客服自己发，漏了家长就卡住
- 家长情绪如何，要不要打个电话
- 下一步该做什么

要求：具体、可执行、短。不要复述已经答过的事实。不要写「建议保持专业」这种空话。
如果没有需要提醒的，直接输出空字符串。

只输出 JSON：{"notes": ["...", "..."]}"""


def internal_notes(question: str, answer: str,
                   activity_id: Optional[str] = None,
                   unsure: Optional[List[str]] = None) -> List[str]:
    ctx = knowledge.build_context(activity_id)
    user = (f"【资料】\n{ctx}\n\n"
            f"【家长/客服原话】\n{question}\n\n"
            f"【资料里查不到的点】\n" + ("\n".join(f"- {u}" for u in (unsure or [])) or "（无）") + "\n\n"
            f"【已经发出的回复】\n{answer}")
    try:
        resp = _client.chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "system", "content": _INTERNAL_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        m = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.S)
        data = json.loads(m.group(0)) if m else {}
        return [str(n) for n in (data.get("notes") or []) if str(n).strip()]
    except Exception:
        return []


# ---------------------------------------------------------------- 主入口
def answer_chat(
    question: str,
    activity_id: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    verbose: bool = False,
    stream_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """所有输入走对话框。

    ① 模型判断类别（update / copy / question）
    ② update  → 代码校验白名单 → store 写入（带备份）→ 模型说一句确认
    ③ copy    → renderer 填模板，原文返回
    ④ question→ 把当前活动资料给模型，直接回答

    返回：
        answer / trace / usage
        wrote         这次写了哪些字段（空 = 没写）
        write_errors  写入失败原因（空 = 都成功）
        missing       当前活动还缺什么（由 store 现算，不看模型怎么说）
        notes         给客服的内部提示（如果生成）
    """
    aid = activity_id or knowledge.current_activity_id()
    decided = classify_input(question, aid)
    if verbose:
        print(f"  🧭 模型判断：{decided['kind']}（{decided['reason'][:40]}）")
    total = decided["usage"]

    def done(out: Dict[str, Any]) -> Dict[str, Any]:
        """统一收尾：合并 usage、补齐字段、顺手给流式界面回填整段。"""
        out["usage"] = _merge(total, out.get("usage") or {})
        out.setdefault("wrote", [])
        out.setdefault("article_saved", "")
        out.setdefault("write_errors", [])
        out.setdefault("missing", store.missing_fields(knowledge.get_activity(aid) or {}))
        # 只有 question 那条路是真流式；其余一次性结果在这里整段回调一次，
        # 界面就不需要为「有没有流式」写两个分支。
        if stream_cb is not None and (out.get("trace") or [""])[-1] != "<context>":
            stream_cb(out["answer"])
        return out

    # ---- 粘进来一篇推文：先整篇存原文，再抽固定字段 ----
    # 原文不拆字段（推文是叙述文字，拆了必丢东西），只有固定字段进库。
    article_saved = ""
    if decided.get("is_article") and len(question) > 200:
        rel = knowledge.save_article(question)
        store.set_article_pointer(rel)
        article_saved = rel
        if verbose:
            print(f"  📄 推文原文已存：{rel}（{len(question)} 字）")

    # ---- update ----
    if decided["kind"] == "update" and decided["updates"]:
        good, bad = _valid_updates(decided["updates"])
        if verbose:
            print(f"  📝 抽到：{good}  被拒：{bad}")
        if good:
            res = write_updates(good)
            ok = bool(res.get("ok"))
            err = "" if ok else str(res.get("error") or res)
            out = _confirm_written(
                question, good, ok, err,
                extra=(f"整篇推文原文也已存档（{article_saved}），"
                       f"以后问活动内容直接读原文，不用再拆字段" if article_saved else ""))
            out["wrote"] = sorted(good) if ok else []
            out["write_errors"] = [] if ok else [err]
            out["article_saved"] = article_saved
            return done(out)
        # 抽到的字段全不合法 → 当提问处理，同时把被拒原因带出去
        out = answer_ctx(question, aid, history, stream_cb=stream_cb)
        out["write_errors"] = bad
        return done(out)

    # ---- copy ----
    if decided["kind"] == "copy":
        # 连「哪一种文案」都没说清 → 反问，不要替她猜成欢迎语
        if not decided["copy_type"]:
            out = {"answer": "要哪种文案？\n"
                             f"· 活动群通知（发在已经报名的家长群）：{STAGE_NAMES}\n"
                             "· 推广文案（发到别的群或朋友圈拉人报名）",
                   "trace": ["classify", "ask_copy_type"]}
            return done(out)

        # B2 推广/招募文案：没有定稿模板，交给模型写（事实仍取自资料）
        if decided["copy_type"] == "promo":
            return done(write_promo(question, aid, decided["promo_channel"]))

        # B1 活动群定稿：代码填模板，原文输出
        # 「全部」= 客服要整个流程的通知，5 个阶段都给；
        # 认不出的阶段才反问，避免「要推广却吐出欢迎语」那种答非所问。
        if "全部" in (decided["stage"] or ""):
            text = render_copy("", all_when_empty=True)
        else:
            text = render_copy(decided["stage"], all_when_empty=False)
        if not text:
            # 认不出是哪个阶段 —— 宁可反问，也不要把 5 个阶段全吐出来
            out = {"answer": f"活动群文案分这几个阶段，你要哪一段？\n{STAGE_NAMES}\n"
                             f"（如果要发到别的群拉人报名，那是推广文案，跟我说「写推广文案」就行）",
                   "trace": ["classify", "ask_stage"]}
            return done(out)

        return done({"answer": text, "trace": ["classify", "render_group_messages"]})

    # ---- question ----
    return done(answer_ctx(question, aid, history, stream_cb=stream_cb))
