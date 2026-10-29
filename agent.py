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
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

import config
import knowledge
import renderer
import store

def _new_client() -> OpenAI:
    """建模型客户端。

    为什么要给 api_key 一个占位串：openai 3.x 在**构造时**就校验凭证，
    `OpenAI(api_key="")` 会直接抛 OpenAIError: Missing credentials。
    而本模块是在 import 时就建客户端的 —— 结果就是「新机器上还没填 Key 时，
    Streamlit 界面还没画出来就崩了」，我们那个引导填 Key 的面板根本轮不到显示。
    给个占位串让它先建成功；真正没 Key 时 config.check() 会拦住并给出引导。
    """
    return OpenAI(api_key=config.API_KEY or "not-set-yet", base_url=config.BASE_URL)


_client = _new_client()


def reset_client() -> None:
    """重建模型客户端。

    为什么需要：_client 是模块级对象，创建时就把 api_key / base_url 定死了。
    在界面上换了 Key 之后如果不重建，请求还是打到旧 Key 上（表现为「明明改了还是 401」）。
    """
    global _client
    _client = _new_client()


def ping_model() -> Tuple[bool, str]:
    """用一次最小请求验证 Key / 接口地址 / 模型名是否配套。

    为什么要真调一次：Key 填错、base_url 少 /v1、模型名和接口不匹配，
    这三种错误都只在真发请求时才暴露。存完不测，等于把错误留给客服。
    """
    try:
        resp = _client.chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "user", "content": "只回复两个字母：ok"}],
            max_tokens=8,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        return True, text or "（返回空内容，但连接正常）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


_EMPTY = (None, "", [], {})


def _usage(u) -> Dict[str, int]:
    return {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0}


def _merge(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    return {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0) for k in
            ("prompt_tokens", "completion_tokens", "total_tokens")}


# ---------------------------------------------------------------- 问答
_CTX_SYSTEM = """你是「山海科普研学」的客服助手。

**客服**会把家长的问题打给你，你帮她写一段**能直接复制发给家长**的话。
家长不会用这个系统，读到这段字的永远是客服，然后她转给家长。

## 最重要的一条：你写的是「客服发给家长的话」
· 第一人称「我」= 客服本人；「您」= 家长。写成客服正在跟家长说话的样子
· **绝不能出现系统视角的词**：资料、知识库、数据库、界面、面板、右侧、系统、
  「我这边看不到」「我让客服」「助手」——这些话家长看不懂，客服也没法发
· 需要客服亲手做的事（发报名二维码、发公众号名片、发导航照片）**不要写进正文**：
  正文里客服本来就会发。要提醒客服别忘了，交给「客服提示」那一栏，不归你管
· 一到三句，先给结论，不要罗列
· **时间一律用 24 小时制照抄**（15:00-18:00），不要写成「下午3点到6点」——家长分不清是早上还是晚上
· **不要整段贴群公告**。公告体（「各位家长请知悉」「请各位家长放心」那种）是发群用的，
  不是一对一回答家长的语气；要引公告内容就用客服的口语转述。群公告留在「生成文案」那条路上
· 但**家长问「怎么做」时，步骤必须讲清楚**（怎么进校预约、怎么报名、怎么去、要带什么），
  不能只回一句「到时候我在群里发」——她问的就是现在要怎么做
· 就算现在还不能操作（例如进校预约只能在活动前一天和当天开窗），**也要先把步骤讲清**，
  再补一句什么时候能操作。别用「等群里通知」把步骤糊过去

## 已经确定的事实，绝不能说成「还没定」
【当前活动】里 **有值的字段就是已经确定的事实**，直接答具体内容：
· meeting_point 有值 → 直接说那个地点，**不许**再说「集合点还没定」「定了再发」「每期不一样」
· meeting_time / time_range 有值 → 直接说时间，不许说「时间待定」
· 有值和没值是两种完全不同的状态，不要混着说

一句话里同时出现「地点在 X」和「集合点还没定」是**自相矛盾**，家长会直接蒙。

（真实踩过：库里集合点已经填好，模型却照抄了一条「集合点每期不同」的旧规则，
答出「地点在海洋生物标本馆。集合点每期不一样，定了我发您」。根因是那条规则
没做条件判断就进了上下文——现在数据层已经改成「集合点为空时才生效」，
但你在写话时也要守住这条。）

## 具体人名：资料没明说，就不许猜
凡涉「谁带队 / 谁主讲 / 谁负责 / 请的哪位老师」这类具体人名，
只有原文**明确写了「由谁担任」**才能答；否则一律用客服的口语推掉：
「具体是哪位老师带队，我确认一下再回您」／「这个我帮您问一下」。

**不要说「我这边没有……的记录」**——那是系统腔（本项目已明令禁止）。

**署名不是证据**：推文末尾的「文丨某某」「图丨某某」「审核丨某某」「编辑丨某某」
是公众号排版信息，不能当成带队老师、主讲人、负责人。
（真实踩过：推文末尾有「审核丨付广」，模型就答「付广老师带队」，
而付广其实是进校预约的被访人，跟带队毫无关系。）

## 查不到 / 还没定的时候，用客服的话说
· 「这个我确认一下再回您」／「集合点还没定，定了我在群里发定位和导航照片」／「以当期推文为准」
· **不要说「我这边没有记录」**——那是系统腔，家长会觉得在跟机器人讲话
· 更不许编，不许拿别的期次顶替，不许给一个听起来合理的理由

## 例外：如果这句是客服在问工具本身
（例如「你能看到推文吗」「你还缺什么」「推文在哪」）
那就直接对客服说话，不用装成家长对话。你知道这些：
· 推文的**文字版全文**在手上，活动定位、活动内容、环节机制、研学收获、专业支持、
  费用退费、自备物品、报名方式都在里面
· 推文里的**图片**看不到（报名二维码、联系电话在原文里是图），但家长打开推文就能扫，够用
· 价格、名额、日期时段、集合点、室内外、分组、每组人数是团队核对过的固定字段
· 没有家长名单

## 事实优先级（两处说法冲突时）
· 价格、名额、日期时段、集合点、室内外、分组、每组人数 → 以固定字段为准（客服可能更新过）
· 环节、机制、奖项、收获、活动定位、报名方式、退费规则、自备物品 → 以推文原文为准

## 只答被问的，别主动辟谣
家长没提某个错误说法，就不要主动去否认它 —— 那等于替她把错的说法先想出来。
· 家长问「怎么报名」→ 只说怎么报名
· 家长问「是不是在群里接龙」→ 才说「不是在群里接龙，是扫推文里的二维码」
· 家长问「是不是北门集合」→ 才纠正北门是进校门，不是集合点

## 报名怎么答
报名是扫**当期推文里**的报名二维码 / 链接填表。
家长说找不到推文或扫不出来 → 「我发您一份」。
不要说「二维码只有客服有」——二维码就在推文里，家长自己就能扫。

## 语气：客气，但不油腻
客服是在**服务**家长，不是求人，也不是发通知。

· **称呼是硬要求**：每条回复里至少出现一次「您」指代家长，哪怕只是一句告知也要带；
  一律不用「你」（「你们」也改成「您和孩子」）
  ✗ 集合点每期不一样，这一期还没定
  ✓ 集合点每期不一样，这一期还没定，定了我第一时间发您
· 收口不是每句都要。**只有这三种情况加一句**：
  要家长做决定（「您看要不要先定下来」）、要拒绝（「还请您理解」）、有不确定（「我确认好回您」）
  纯粹告知事实的（集合点在哪、带什么东西），说清楚就行，不用硬加客套话
· 要加就换着说，别每条都用同一句
· 陈述也要有温度：
  ✗ 这一期还没定，定了群里发
  ✓ 这一期还没定，定了我第一时间在群里发您
· 要拒绝时，先说原因再给替代，不要只丢一个「不行」：
  ✗ 不代订酒店
  ✓ 酒店这块我们暂时没有这项服务，得您自己安排；活动是自驾到海大，您订学校附近会方便些
· 语气词（哈／呢／啦）一条最多两处，堆多了显得不专业
· **不许为了客气把话说糊**：还没定就说还没定，不要用「我再帮您确认一下哦」把不确定藏起来

中文口语化，像熟悉的团队伙伴，客气但不客套。"""


def answer_ctx(question: str, activity_id: Optional[str] = None,
               history: Optional[List[Dict[str, Any]]] = None,
               verbose: bool = False,
               stream_cb: Optional[Any] = None) -> Dict[str, Any]:
    """纯问答：把当前活动资料给模型，直接回答。不写库、不调用工具。

    stream_cb（流式回调）：传了就用 stream=True 逐字推给界面，边生成边显示。
    没传就是一次性拿完整结果（命令行、评测走这条）。
    """
    ctx = knowledge.build_context(activity_id, question)
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

**粘的是文章时，再判断一次 article_kind（推广推文，还是活动回顾推文）：**
· "review" = 这是**活动办完之后写的回顾推文**。特征是：讲已经办完的那一场，
  常出现「回顾」「精彩瞬间」「圆满结束」「现场照片」「孩子们在活动中……」（过去时），
  目的是总结已经发生的事
· "promo"  = 这是**活动之前写的招募/推广推文**。特征是：讲还没办、正在报名的那一场，
  有报名方式、名额、时间地点、招募对象、「扫码报名」
· ""       = 不是文章，或有文章但它既不像回顾也不像招募（比如普通群公告）
· **拿不准就看有没有报名信息**：正在拉人报名 → promo；已经办完在总结 → review

**粘的是整篇推文时，再判断一次 is_new_activity（这条很关键，判错会串期）：**
· true = 这是**另一期新活动**的推文（有期次、有日期、讲的是新活动）
· false = 这是**当前这一期**的推文重发/更正，或者只是同一期的补充说明

为什么重要：另一期的推文要**整条换掉**旧记录。如果按「只覆盖推文里提到的字段」处理，
推文没写的集合点、名额、室内外就会留着上一期的值，客服照着答家长就答错了期。

所以第一次见到新一期的推文时：
· 只抽推文里**明确写了**的字段；推文没写的字段**留空，不要填**
· **不要拿「现值」来补**：现值是上一期的，填进去就是串期
· 尤其当推文只写了「名额有限，报满即止」这种说法、没有具体数字时，
  capacity 就不要填，让它空着（空着界面会提示客服去确认，填错了没人发现）

只输出 JSON：
{"kind": "update" | "copy" | "question", "is_article": true | false, "is_new_activity": true | false, "article_kind": "promo" | "review" | "", "copy_type": "template" | "promo"（仅 copy 时用）, "promo_channel": "group" | "moments"（仅 promo 时用，见下）, "stage": "欢迎语/活动提醒/出发提醒/结束感谢；没说哪一段就填「全部」（仅 template 时用）", "updates": {字段: 值}, "reason": "一句话说明依据"}

promo_channel 怎么填：
· 提到「朋友圈」「盆友圈」「pyq」「圈里」「动态」→ moments
· 提到「群」「私域」「家长群」「发到群里」或没说清 → group
· 「推广文案」「招募文案」但没指定渠道 → group
"""

# 文案阶段 → group_messages.json 里 timeline 的圈号。
# 渲染器的 stage 参数是按「圈号是不是子串」筛选的，所以这里只给圈号。
_STAGE_HINT = [
    ("欢迎", "①", None), ("拉群", "①", None), ("建群", "①", None),
    ("预约", "②", None), ("进校", "②", None),
    ("提醒", "③", None), ("前一天", "③", None),
    ("出发", "④", None), ("当天", "④", None),
    # ⑤ 里有两种不同用途的文案，靠 tag 分开：
    #   结束感谢（thanks）当天晚上发；回顾分享（review）等推文发完 1-2 天才发。
    # 「回顾」放在前面先匹配，否则「回顾推文」会被当成「结束」把两段都吐出来。
    ("回顾", "⑤", ["review"]),
    ("结束", "⑤", ["thanks"]), ("感谢", "⑤", ["thanks"]),
]


def _field_menu() -> str:
    act = knowledge.get_activity() or {}
    lines = []
    for k, desc in store.ALLOWED_FIELDS.items():
        cur = act.get(k)
        cur_txt = "（空）" if cur in _EMPTY else "（现值：" + str(cur)[:24] + "）"
        lines.append(f"- {k}：{desc}{cur_txt}")
    # 团队级字段（被访人）也允许在对话里改，但它们不属于某一期，
    # 写在 activities.json 顶层 team_info，不进 activities[0]。
    _team = knowledge.load_team_info()
    for k, desc in store.TEAM_FIELDS.items():
        cur = _team.get(k) or ""
        cur_txt = "（空）" if not cur else "（现值：" + str(cur)[:24] + "）"
        lines.append(f"- {k}：{desc}（团队级，跨期通用）{cur_txt}")
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
    article_kind = data.get("article_kind")
    if article_kind not in ("promo", "review"):
        article_kind = ""           # 认不出就按「不处理文章」走，宁可不存也不乱存
    return {
        "kind": kind,
        "is_article": bool(data.get("is_article")),
        "is_new_activity": bool(data.get("is_new_activity")),
        "article_kind": article_kind,
        "copy_type": copy_type,
        "promo_channel": channel,
        "updates": data.get("updates") if isinstance(data.get("updates"), dict) else {},
        "stage": data.get("stage") or "",
        "reason": data.get("reason", ""),
        "usage": _usage(resp.usage),
    }


_POLISH_SYSTEM = """把下面这段话改成「客服对家长说话」的客气语气。

只做两件事：
1. 用「您」称呼家长，至少出现一次；不出现「你」（「你们」改成「您和孩子」）
2. 语气稍微软一点

**严禁改动任何内容**：
· 所有数字、时间、价格、日期必须**逐字照抄**（「18:00」不能写成「6点」，「88元」不能写成「八十八元」）
· 结论、条款、允许/禁止的口径一个字都不能变
· 不许加内容、不许减内容、不许加新的承诺

直接输出改好的整段，不要解释。
"""


def _num_tokens(s: str):
    """抽出所有数字，用来校验重写有没有动事实。"""
    return sorted(re.findall(r"\d+(?:[:：.]\d+)?", s or ""))


def _ensure_polite(answer: str) -> str:
    """家长话术必须有「您」。不达标就补，但绝不允许改动事实。

    为什么要有这一层：提示词写了「每条至少一次您」，实测仍会漏六分之一
    （一句话就能答完的问题最容易省掉称呼）。语气是客服的脸面，不能靠概率。

    但「整段重写」很危险——实测它把「18:00」改成了「6点」。
    所以重写后要拿数字指纹对一遍：数字变了就判定不可信，
    退回最保守的做法——正文一个字不动，只在末尾追加一句客气话。
    """
    if not answer:
        return answer
    # 群公告体是「各位家长」开头，本来就不该逐个人称，不能硬塞「您」
    if "各位家长" in answer:
        return answer
    if "您" in answer and not re.search(r"你(?!们)", answer):
        return answer

    before = _num_tokens(answer)
    try:
        resp = _client.chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "system", "content": _POLISH_SYSTEM},
                      {"role": "user", "content": answer}],
            temperature=0,
        )
        fixed = (resp.choices[0].message.content or "").strip()
    except Exception:
        fixed = ""

    if fixed and "您" in fixed and _num_tokens(fixed) == before:
        return fixed
    # 重写动了数字 / 还是没加「您」→ 放弃重写，只追一句称呼，正文原样保留
    return answer.rstrip() + "\n\n您还有想了解的吗？随时问我。"


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
        # 派生字段（星期/时长/期次编号）由 date / time_range / session_label 自动算出，
        # 手填只会制造不一致（「10月25日（周六）」这种）。模型偶尔还是会填，静默丢掉。
        if k in store.DERIVED_FIELDS:
            continue
        if k not in store.ALLOWED_FIELDS and k not in store.TEAM_FIELDS:
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


def write_updates(updates: Dict[str, Any], replace: bool = False,
                  keep_article: bool = True) -> Dict[str, Any]:
    """把字段写进当前活动。薄薄一层，真正干活的是 store（校验+备份+归档）。"""
    return store.update_activity(None, updates, replace=replace, keep_article=keep_article)


def _is_other_session(decided: Dict[str, Any]) -> bool:
    """这次粘进来的推文，是不是「另一期」的？

    两条独立信号，任一成立就算另一期：
      ① 代码比期次 —— 推文里写明了期次，而且和库里当前那一期不一样。
         这一条最可靠，因为期次是固定字段，不靠模型自由发挥。
      ② 模型判 is_new_activity —— 兜住推文没写期次的情况。
    为什么不只用模型：模型偶尔会把「同一期推文重发」判成新一期，
    那会白白清空客服手工补过的集合点。代码这一路能推翻它。
    """
    if not decided.get("is_article"):
        return False
    new_label = str((decided.get("updates") or {}).get("session_label") or "").strip()
    old_label = str((knowledge.get_activity() or {}).get("session_label") or "").strip()
    if new_label and old_label and new_label != old_label:
        return True
    return bool(decided.get("is_new_activity"))


# ---------------------------------------------------------------- 文案
# ---------------------------------------------------------------- 按原文拟稿
# 「结束感谢」里的现场亮点、「回顾分享」里的具体细节，过去都是留给客服自己想的空。
# 用户 2026-10-26 的要求：这两处让系统直接按推文拟出来，客服只核对。
#
# 风险在哪：这一步是**模型写内容**，而收尾文案是要发到几百人家长群的。
# 所以加了三道闸：
#   ① 只给它原文（推广推文 / 回顾推文），不给它别的资料
#   ② 要求它同时交出「依据的原句」，并且这句必须在原文里找得到（找不到整条作废）
#   ③ 拟出来的稿子在输出里单独标出来，写明依据，客服看得见才核对得上
_DERIVE_SYSTEM = """你在帮「山海科普研学」的客服填活动群文案里的一个空。

【原文】给的是这一期的文章原文。请按【要求】从原文里提炼一句话。

硬规则：
· 只能用【原文】里**明确写过**的内容。原文没写的，一律不许补充
· 不许写数字（人数、名额、价格），除非原文原句里就有
· 不许写评价和情绪（「收获满满」「玩得超开心」「意猶未尽」这类空话不要）
· 不要引号、不要句号结尾（模板后面还要接别的字）
· 原文里找不到合适依据 → sentence 输出空字符串
· quote 必须**逐字照抄**【原文】里你依据的那一句（不超过 40 字）——
  这句会被拿去原文里核对，找不到就整条作废

只输出 JSON：{"sentence": "...", "quote": "..."}"""

# 两处空分别要什么（只改【要求】，不另写两套提示词）
_TARGET_HIGHLIGHT = (
    "写一句接在「今天孩子们」后面的话，20-35 字。"
    "要具体到孩子们**做了什么**（环节名、动作、做出的东西），不要写感想。"
)
_TARGET_REVIEW = (
    "写一句接在「里面记录了孩子们」后面的话，20-35 字。"
    "写活动里的环节名，或写的孩子们在现场的真实表现（做了什么、做出了什么）。"
)


def _norm(s: str) -> str:
    """比对用：去掉空白和标点，只留字。"""
    return re.sub(r"[\s，。、；：！？「」『』（）()“”\"'’,.;:!?—…\-~]", "", s or "")


def derive_from_article(target: str, source: str, source_name: str) -> Dict[str, str]:
    """从原文里提炼一句话。返回 {} 表示不可用（宁可留空给人工，不允许编）。"""
    if not (source or "").strip():
        return {}
    try:
        resp = _client.chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "system", "content": _DERIVE_SYSTEM},
                      {"role": "user", "content":
                       f"【原文】（{source_name}）\n{source}\n\n【要求】\n{target}"}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        m = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return {}

    sent = str(data.get("sentence") or "").strip().strip("「」\"'")
    quote = str(data.get("quote") or "").strip().strip("「」\"'")
    # 闸②：依据必须在原文里找得到。找不到就是它在编，整条丢掉。
    if not sent or not quote or _norm(quote) not in _norm(source):
        return {}
    if len(sent) > 60:
        return {}
    return {"text": sent, "quote": quote, "source_name": source_name}


def _derive_overrides(tokens: set) -> tuple:
    """看这次缺哪几个空，只给缺的那几个花钱调模型。"""
    ov: Dict[str, str] = {}
    drafts: List[Dict[str, str]] = []

    if "{{现场亮点}}" in tokens:
        d = derive_from_article(_TARGET_HIGHLIGHT, knowledge.load_article(),
                                "当期活动推广推文")
        if d:
            ov["{{现场亮点}}"] = d["text"]
            drafts.append({**d, "label": "现场亮点"})

    if "{{具体细节}}" in tokens:
        d = derive_from_article(_TARGET_REVIEW, knowledge.load_review(),
                                "活动回顾推文")
        if d:
            ov["{{具体细节}}"] = d["text"]
            drafts.append({**d, "label": "回顾细节"})

    if "{{回顾推文链接}}" in tokens:
        url = knowledge.review_url(knowledge.load_review())
        if url:
            ov["{{回顾推文链接}}"] = url
            drafts.append({"label": "回顾推文链接", "text": url,
                           "quote": "（从回顾推文原文里提取的链接）",
                           "source_name": "活动回顾推文"})
    return ov, drafts


def _render_group(aid: Optional[str], key: Optional[str],
                  overrides: Optional[Dict[str, str]] = None,
                  only_tags: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """调渲染层并把 JSON 解析好。渲染层出错时不让整个对话挂掉。"""
    try:
        raw = renderer.render_group_messages(aid, stage=key, overrides=overrides,
                                             only_tags=only_tags)
    except Exception as e:
        return {"error": f"渲染失败：{e}"}
    try:
        return json.loads(raw)
    except Exception:
        return {"error": raw}


def render_copy(stage_text: str, all_when_empty: bool = True) -> str:
    """按阶段渲染群文案。模板里的 {{占位符}} 由代码填，保证原文输出。

    渲染器返回的是 JSON（给程序用），这里格式化成客服能直接复制发群的文本。
    格式化这一步也必须由代码做：让模型去「排版」就等于把定稿交给它改写。

    all_when_empty：认不出阶段时，是否把 5 个阶段全部输出。
        界面的「生成全部群文案」按钮传 True（它就是要全部）；
        对话框路径必须传 False —— 否则「写推广文案」会被渲染成「欢迎语」，
        因为 stage=None 时渲染器从第一个阶段开始输出。这是真实报过的 bug。
    """
    key, tags = None, None
    for word, no, tg in _STAGE_HINT:
        if word in (stage_text or ""):
            key, tags = no, tg
            break
    if key is None and not all_when_empty:
        return ""

    aid = knowledge.current_activity_id()
    data = _render_group(aid, key, None, tags)
    if not data:
        return ""
    if data.get("error"):
        return json.dumps(data, ensure_ascii=False)

    # 两遍渲染：第一遍先看这次到底缺哪几个空，只给缺的那几个花钱调模型拟稿，
    # 拟到就第二遍填进去。不缺（或拟不出来）就完全不调模型。
    tokens = set(data.get("unfilled_summary") or [])
    overrides, drafts = _derive_overrides(tokens)
    if overrides:
        data = _render_group(aid, key, overrides, tags) or data

    lines: List[str] = []
    for m in data.get("messages", []):
        if m.get("tips"):
            # 提示本身是多行的（比如「改反馈时点」分两种建群时间），
            # 不能简单拼成一行，否则两种情形看起来像同一条。
            block: List[str] = []
            for t in m["tips"]:
                ls = [x.strip().lstrip("·").strip()
                      for x in str(t).splitlines() if x.strip()]
                if not ls:
                    continue
                block.append("· " + ls[0])
                block += ["   " + x for x in ls[1:]]
            if block:
                lines.append("⚠ 这一步必须人工做的：" + chr(10) + chr(10).join(block))
        elif m.get("text"):
            lines.append(m["text"].strip())
            if m.get("unfilled"):
                lines.append("（上面还有需要人工补的："
                             + "、".join(m["unfilled"]) + "）")
        elif m.get("manual"):
            lines.append(f"【需要你手动发】{m.get('label', '')}：{m.get('action', '')}")
        elif m.get("note"):
            lines.append(f"【{m.get('label', '')}】{m['note']}")
    if not lines:
        return ""

    out = "\n\n————\n\n".join(lines)
    left = data.get("unfilled_summary") or []
    if left:
        reasons = data.get("unfilled_reasons") or {}
        out += "\n\n⚠ 还需人工补：" + "、".join(left)
        detail = [f"· {t}：{reasons[t]}" for t in left if reasons.get(t)]
        if detail:
            out += "\n" + "\n".join(detail)

    # 拟稿单独标出来：这几句是系统写的，要让人看见依据才核对得上
    if drafts:
        out += "\n\n🤖 下面这几处是系统按原文拟的稿，发之前核对一下现场是不是真这样："
        for d in drafts:
            out += (f"\n· {d['label']}：「{d['text']}」"
                    f"\n  依据{d['source_name']}：「{d['quote']}」")
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
· **每版 2-3 个 emoji，最多 3 个**（数一下再输出），放在句首或关键词旁边
  （时间用 🗓、地点用 📍、价格用 💰、动手环节用 🎨、报名用 👉），
  **不要堆在句尾**，也不要每行都放
· 给两版：先写「A 稳重版」，再写「B 活泼版」（活泼版可以比稳重版多一个 emoji）

直接输出两版文案，不要解释，不要写「以下是」。
"""

# 朋友圈：超过 6 行会被折叠成「全文」，所以必须短，而且不要分点排版
_PROMO_MOMENTS = "你在给「山海科普研学」写**朋友圈文案**：发在自己的朋友圈，吸引家长报名。\n\n" + _PROMO_FACTS + """
写法要求（朋友圈有硬限制，必须遵守）：
· **每版 60-100 字，最多 5 行**。超过 6 行会被折叠成「全文」，家长根本不会点开
· **不要分点、不要小标题、不要罗列**。朋友圈是一段话，不是公告
· 结构就用三句：一句钩子 + 一句具体信息（时间、对象、价格挑一两个说，不要全塞）+ 一句行动（扫码看推文 / 私信我）
· **每版 2-3 个 emoji，最多 3 个**（数一下再输出），放在句首或关键词旁边
  （时间 🗓、地点 📍、价格 💰、报名 👉），不要堆在句尾，也不要一句一个
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
    ctx = knowledge.build_context(activity_id, question)
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
    ctx = knowledge.build_context(activity_id, question)
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
def _review_saved_reply(info: Dict[str, Any]) -> str:
    """回顾推文存好之后跟客服说的话（客服看的，不是家长看的）。"""
    lab = info["label"]
    msg = (f"活动回顾推文已经存下来了：{lab}（{info['rel']}，{info['chars']} 字）。\n"
           f"活动字段一项都没动 —— 回顾推文讲的是已经办完的那一场，不是新一期，"
           f"拿它去抽日期/名额会把旧值写进新一期。\n\n"
           f"接下来可以直接做：\n"
           f"· 发群里的「回顾分享文案」→ 说「写回顾分享」\n"
           f"· 「结束感谢」里的现场亮点 → 说「写结束感谢」（按当期推广推文拟）\n"
           f"以后问「{lab}回顾里写了什么」也能查回来。")
    if info.get("replaced"):
        msg += "\n（这一期之前存过回顾推文，旧的那份已备份到 data/backups/）"
    return msg


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
        out.setdefault("review_saved", "")
        out.setdefault("write_errors", [])
        out.setdefault("replaced", False)
        out.setdefault("dropped", [])
        out.setdefault("missing", store.missing_fields(knowledge.get_activity(aid) or {}))
        # 只有 question 那条路是真流式；其余一次性结果在这里整段回调一次，
        # 界面就不需要为「有没有流式」写两个分支。
        if stream_cb is not None and (out.get("trace") or [""])[-1] != "<context>":
            stream_cb(out["answer"])
        return out

    # ---- 粘进来一篇推文：先整篇存原文，再抽固定字段 ----
    # 原文不拆字段（推文是叙述文字，拆了必丢东西），只有固定字段进库。
    article_saved = ""
    article_archived = ""
    # 文章类型：模型判，代码拿旁证再纠一次。
    # 为什么不能只信模型：判成「推广推文」会把当期推文覆盖掉，
    # 判成「回顾推文」只是多存一个文件，两条路代价差太大。
    a_kind = knowledge.decide_article_kind(question, decided.get("article_kind") or "")
    # 为什么连 is_article 也要纠：
    #   实测模型会把一篇完整的回顾推文判成「不是文章」（is_article=false），
    #   那就直接掉进 update 分支，把回顾里的旧日期/旧地点/旧分组写进**当前这一期**——
    #   这就是串期。只要旁证确定它是文章，而且她不是在要文案，就按文章算。
    is_article = (bool(decided.get("is_article"))
                  or (len(question) > 200 and a_kind != ""
                      and decided["kind"] != "copy"))    # 上一期的推文要先按「期次」名字归档，否则换期之后 current.md 一覆盖就找不回来了
    # （save_article 的自动备份是时间戳命名，翻起来只能一个个打开猜）。
    if _is_other_session(decided):
        article_archived = knowledge.archive_article()

    # ---- 粘进来一篇「活动回顾推文」：单独存成一个文件，一个活动字段都不写 ----
    # 回顾推文讲的是**已经办完**的那一场，里面全是旧值（上一期的日期、名额、集合点）。
    # 如果跟推广推文一样拿去抽字段，就会把旧值写进当前这一期 —— 串期。
    # 所以这里直接存原文就走，不进入下面的 update 分支。
    # 长度门槛只对「推广推文」设 200：存推广推文会覆盖 current.md，宁可严一点。
    # 回顾推文只是多存一个文件，不具破坏性，门槛放到 80 字（太短的话多半不是推文）。
    if (is_article and a_kind == "review"
            and decided["kind"] != "copy" and len(question) > 80):
        info = knowledge.save_review(question)
        if info.get("ok"):
            if verbose:
                print(f"  📄 回顾推文已存：{info['rel']}（{info['label']}，"
                      f"{info['chars']} 字）")
            return done({"answer": _review_saved_reply(info),
                         "trace": ["classify", "save_review"],
                         "review_saved": info["rel"]})
        return done({"answer": f"回顾推文没存下来：{info.get('error')}",
                     "trace": ["classify", "save_review"],
                     "write_errors": [str(info.get("error"))]})
    # 只有「这次是在录一期新的推文」才落盘。
    # 曾经只判 is_article、不判 kind，结果模型把一段长文案请求误标成 is_article，
    # 就把当期推文覆盖掉了（实测踩过，靠 git 才恢复）。判断是模型给的，破坏动作不能跟着它走。
    if is_article and decided["kind"] == "update" and len(question) > 200 \
            and a_kind != "review":
        rel = knowledge.save_article(question)
        store.set_article_pointer(rel)
        article_saved = rel
        if verbose:
            print(f"  📄 推文原文已存：{rel}（{len(question)} 字）"
                  + (f"；上一期原文已归档 {article_archived}" if article_archived else ""))

    # ---- update ----
    if decided["kind"] == "update" and decided["updates"]:
        good, bad = _valid_updates(decided["updates"])
        # 团队级字段（被访人）要跟当期字段分开写：它们进 activities.json 顶层，
        # 绝不能走 update_activity——换期时整条替换会把 activities[0] 清空，
        # 团队信息跟着丢（这类“换期把无关信息带走”的事故已经踩过一次）。
        team_part = {k: v for k, v in good.items() if k in store.TEAM_FIELDS}
        good = {k: v for k, v in good.items() if k not in store.TEAM_FIELDS}
        team_note = ""
        if team_part:
            knowledge.save_team_info(str(team_part.get("visit_target_name", "")),
                                     str(team_part.get("visit_target_phone", "")))
            team_note = ("团队信息已更新："
                         + "、".join(f"{store.TEAM_FIELDS[k]}={v}"
                                    for k, v in team_part.items())
                         + "（跨期通用，之后生成的进校预约文案都用这个）")
            if verbose:
                print(f"  👥 团队信息写入：{team_part}")
        if team_part and not good:
            # 只改了团队信息，没动这一期的字段 → 不必再调模型写确认语
            return done({"answer": team_note + "。",
                         "trace": ["classify", "save_team_info"],
                         "wrote": sorted(team_part)})
        # 粘的是另一期的推文 → 整条替换，没抽到的字段一律清空。
        # 只做合并的话，新一期会继承上一期的集合点/名额/室内外，客服就答错期了。
        replace = _is_other_session(decided) and bool(good)
        if verbose:
            print(f"  📝 抽到：{good}  被拒：{bad}  整条替换：{replace}")
        if good:
            res = write_updates(good, replace=replace, keep_article=bool(article_saved))
            ok = bool(res.get("ok"))
            err = "" if ok else str(res.get("error") or res)
            extras: List[str] = []
            if team_note:
                extras.append(team_note)
            if article_saved:
                extras.append(f"整篇推文原文也已存档（{article_saved}），"
                              f"以后问活动内容直接读原文，不用再拆字段")
            if ok and replace:
                n = len(res.get("dropped_fields") or [])
                extras.append(
                    f"这次是**整条替换**成「{good.get('session_label') or '新一期'}」："
                    f"上一期（{res.get('activity_id')} 之前的记录，共 {n} 项）已归档，"
                    f"推文没写到的字段已经清空，等客服向负责人确认后再补。"
                    f"跟客服说清楚：清空是故意的，不是出错。")
            elif ok:
                extras.append("这次是**同一期的信息补充**，只覆盖这次提到的字段，其余保持不动。")
            if article_archived:
                extras.append(f"上一期推文原文已归档到 {article_archived}，后面要找回还能查。")
            out = _confirm_written(question, good, ok, err,
                                   extra="\n".join(extras))
            out["wrote"] = sorted(list(good) + list(team_part)) if ok else []
            out["write_errors"] = [] if ok else [err]
            out["article_saved"] = article_saved
            out["replaced"] = bool(ok and replace)
            out["dropped"] = res.get("dropped_fields") or []
            return done(out)
        # 原文存下来了，但固定字段一个都没抽出来 → 直说，别拿长文本去走问答
        if article_saved:
            out = {"answer": ("推文原文已经存下来了，但没从里面认出新一期的固定字段。\n"
                              "价格、名额、日期、集合点这几项直接告诉我，我填进去。"),
                   "trace": ["classify", "save_article"],
                   "write_errors": bad, "article_saved": article_saved}
            return done(out)
        # 抽到的字段全不合法 → 当提问处理，同时把被拒原因带出去
        out = answer_ctx(question, aid, history, stream_cb=stream_cb)
        out["answer"] = _ensure_polite(out["answer"])
        out["write_errors"] = bad
        return done(out)

    if decided["kind"] == "update" and article_saved:
        return done({"answer": ("推文原文已经存下来了，但没从里面认出新一期的固定字段。\n"
                                "价格、名额、日期、集合点这几项直接告诉我，我填进去。"),
                     "trace": ["classify", "save_article"],
                     "article_saved": article_saved})

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
    out = answer_ctx(question, aid, history, stream_cb=stream_cb)
    out["answer"] = _ensure_polite(out["answer"])
    return done(out)
