"""
Agent 主体：路由 + Function Calling

架构（这是本项目最该讲清的设计）：
    确定性的判断交给代码  →  router.py
    需要理解语言的判断     →  交给模型
    组织自然语言回答       →  交给模型

    具体分三条路：
      A. 代码能确定唯一的工具（如「第十六期多少钱」→ get_activity(S16)）
         → 用 tool_choice **强制**模型调这个工具（仍是标准 Function Calling）
      B. 代码确定了多个调用（如「第六期和第十六期分别多少钱」）
         → 代码直接跑完所有工具，把数据作为上下文交给模型，让它只负责组织回答
      C. 代码判断不出来（如「多少钱？」）
         → tool_choice="auto"，完全交给模型自己决定要调什么

    这样既保留了 Function Calling 的能力，又把「价格查错期次」这类
    确定性错误的概率压到最低。
"""

import json
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI

import config
import router
import tools

_client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)


def build_system_prompt() -> str:
    kb = tools.load_kb()
    rules = kb.get("agent_behavior_rules", {}).get("rules", [])
    rules_text = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1))
    return f"""你是「山海科普研学」的家长咨询助手，在微信/私域群为家长答疑。

你没有活动数据在脑子里。事实一律以工具返回的数据为准，不准编造数字、日期、名额、奖项。
查不到就说「这一期我这边没有记录，建议看当期推文或群公告」，绝不用别的期次顶替。

## 数据规则
{rules_text}

## 客服补充信息时（重要）
推文里经常没有时间、价格、集合地点，客服会陆续问到并告诉你。这时必须调 `update_activity` 真正写入，不能只在回复里说「记住了」——那样等于没记。
写完后，把工具返回的 `still_missing` 告诉客服：还缺哪几项、需要向负责人确认什么。
**不要替客服编造任何没提供的字段**：客服没说集合点，就不要猜「校门口」。
客服粘贴整篇推文时，先摘出能确定的字段（活动名、主题、环节、亮点、招募对象）写入，再列出还需补充的项。
写不进去时（格式错、期次不存在），如实转告，不要换个说法糊过去。

## 输出文案时（重要）
工具返回里带 `text` 字段时，那是**发给家长的原话**，必须**逐字原样输出**：
保留 emoji、换行、【】符号和 {{占位符}}，不得摘要、不得改写、不得把 {{集合时间}} 换成「待定」。

输出完整原文之后，再用一两句话说明哪几处占位符需要人工补、为什么。
摘要化的文案对客服没用——他要的是能直接复制粘贴的东西。

## 风格
中文口语化，像熟悉的团队伙伴，先给结论，尽量 150 字内。
涉及价格、名额、退费时，末尾加一句「以当期推文为准」。
"""


SYSTEM_PROMPT = build_system_prompt()


def _execute_plan(planned: List[Dict], verbose: bool) -> List[Dict[str, Any]]:
    """在代码里直接执行工具计划，返回结果列表。"""
    out = []
    for call in planned:
        args_json = json.dumps(call["args"], ensure_ascii=False)
        result = tools.call_tool(call["tool"], args_json)
        if verbose:
            preview = result if len(result) <= 200 else result[:200] + "…"
            print(f"  🔧 调用 {call['tool']}({call['args']})")
            print(f"     ↳ {preview}")
        out.append({"tool": call["tool"], "args": call["args"], "result": result})
    return out


def _build_context_message(executed: List[Dict[str, Any]]) -> str:
    """把多个工具结果拼成一条上下文，附在对话末尾。"""
    parts = ["【系统检索结果】以下是权威数据，请严格依据它回答，不要自行补充："]
    for item in executed:
        parts.append(f"\n--- {item['tool']}({json.dumps(item['args'], ensure_ascii=False)}) ---")
        parts.append(item["result"])
    parts.append("\n【回答要求】只答家长问的部分，简洁，不要复述整份数据。")
    return "\n".join(parts)


def _fc_loop(
    conversation: List[Dict[str, Any]],
    verbose: bool,
    forced_tool: Optional[str] = None,
) -> Dict[str, Any]:
    """标准 Function Calling 循环。"""
    tool_rounds = 0
    pt = ct = 0
    trace: List[str] = []

    while tool_rounds < config.MAX_TOOL_ROUNDS:
        kwargs: Dict[str, Any] = {
            "model": config.MODEL,
            "messages": conversation,
            "tools": tools.SCHEMAS,
            "temperature": config.TEMPERATURE,
        }
        # 只在第一轮强制工具；拿到结果后放开，允许模型补充查询
        if forced_tool and tool_rounds == 0:
            kwargs["tool_choice"] = {"type": "function", "function": {"name": forced_tool}}
        else:
            kwargs["tool_choice"] = "auto"

        resp = _client.chat.completions.create(**kwargs)
        if resp.usage:
            pt += resp.usage.prompt_tokens or 0
            ct += resp.usage.completion_tokens or 0

        msg = resp.choices[0].message

        if not msg.tool_calls:
            conversation.append({"role": "assistant", "content": msg.content or ""})
            break

        conversation.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            result = tools.call_tool(tc.function.name, tc.function.arguments)
            if verbose:
                preview = result if len(result) <= 200 else result[:200] + "…"
                print(f"  🔧 调用 {tc.function.name}({tc.function.arguments})")
                print(f"     ↳ {preview}")
            trace.append(tc.function.name)
            conversation.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
        tool_rounds += 1

    return {
        "answer": conversation[-1].get("content", ""),
        "tool_rounds": tool_rounds,
        "trace": trace,
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def ask(conversation: List[Dict[str, Any]], verbose: bool = True) -> Dict[str, Any]:
    """执行一次完整问答（按需自动选路 A / B / C）。"""
    if not conversation or conversation[0].get("role") != "system":
        conversation.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

    question = ""
    for m in reversed(conversation):
        if m.get("role") == "user":
            question = m.get("content", "")
            break

    planned = router.plan(question)

    # ---- 路线 A/B：代码能确定要查什么 → 代码直接查完，模型只负责组织语言 ----
    #
    # 为什么不用 tool_choice 强制模型去调：
    #   实测 DeepSeek 会忽略 tool_choice={"type":"function",...} 这种强制指定，
    #   仍然按自己的想法选工具（问「第十六期多少钱」它会去调 list_activities）。
    #   既然强制不可靠，那就不强求模型去"做决定"，直接由代码执行。
    if planned:
        executed = _execute_plan(planned, verbose)
        work = list(conversation) + [
            {"role": "system", "content": _build_context_message(executed)}
        ]
        resp = _client.chat.completions.create(
            model=config.MODEL,
            messages=work,
            temperature=config.TEMPERATURE,
        )
        answer = resp.choices[0].message.content or ""
        conversation.append({"role": "assistant", "content": answer})
        u = resp.usage
        return {
            "answer": answer,
            "tool_rounds": 1,
            "trace": [p["tool"] for p in planned],
            "usage": {
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            },
        }

    # ---- 路线 C：代码判断不出来 → 交给模型自己决定调什么（标准 Function Calling）----
    return _fc_loop(conversation, verbose, forced_tool=None)


# ============================================================================
# v3：无路由问答
# ============================================================================
#
# 上面的 ask() 是 v2 架构（关键词路由 + Function Calling）。
# 保留它是为了跑 A/B 对比，生产路径请用 answer_ctx()。
#
# 为什么 v3 不需要路由：
#   真实家长提问里 0% 会写「第十六期」——群本身就是那一期的群。
#   期次是**给定**的（knowledge.current_activity_id），不是从句子里猜的。
#   既然背景已定，就不存在「该查哪份资料」的判断，整份资料直接给模型看即可。
#
#   写入、生成文案、缺口体检不属于「回答家长问题」，走显式入口（store / renderer），
#   不经过模型，也就没有「猜意图」这一步。

_CTX_SYSTEM = """你是「山海科普研学」的家长咨询助手，在微信/私域群里为家长答疑。

你没有活动数据在脑子里。下面会给你这一期的全部资料，事实一律以资料为准，
不准编造数字、日期、名额、老师姓名。

查不到就说「这一期我这边没有记录，建议看当期推文或群公告」，
绝不用别的期次顶替，也不要编一个听起来合理的原因。

中文口语化，像熟悉的团队伙伴，先给结论，尽量 150 字内。"""


def answer_ctx(
    question: str,
    activity_id: Optional[str] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """v3 主路径：不做路由，把当前活动的全部资料给模型，直接回答。

    Args:
        question: 家长的原话
        activity_id: 当前活动；不传就取「报名中」那一期
    """
    import knowledge   # 局部导入，避免和 v2 的循环依赖

    ctx = knowledge.build_context(activity_id)
    act = knowledge.get_activity(activity_id)
    label = (act or {}).get("session_label") or "（无当前活动）"

    resp = _client.chat.completions.create(
        model=config.MODEL,
        messages=[
            {"role": "system", "content": _CTX_SYSTEM},
            {"role": "user", "content": question},
            {"role": "system", "content": ctx},
        ],
        temperature=config.TEMPERATURE,
    )
    answer = resp.choices[0].message.content or ""
    u = resp.usage
    if verbose:
        print(f"  📎 当前活动：{label}   上下文 {len(ctx)} 字")
    return {
        "answer": answer,
        # v3 没有工具，trace 记一下「数据从哪来」，方便评测时对照
        "trace": ["<context>"],
        "context_chars": len(ctx),
        "usage": {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
        },
    }


# ============================================================================
# v6：对话输入版（所有输入都走对话框，但带一个"写入"工具）
# ============================================================================
#
# 为什么和 v3 不同：
#   v3 假设「写入走界面按钮」，所以它没有工具，纯粹读资料答问题。
#   但产品决定是「全部输入都走对话框」——客服不学表单，想到什么就说什么。
#
#   于是「这是录入还是提问」这个判断又回来了。区别是这次的兜底不是靠猜准，
#   而是靠**结果可见**：
#       · 写库仍然过 store.update_activity（白名单 + 类型校验 + 写前备份）
#       · 写完立刻返回「改了哪几项、还缺什么」
#       · 界面上有一块只读状态面板，写完当场刷新
#   写错了立刻看得见，也能从 data/backups/ 回滚。不要求模型 100% 判断对。
#
#   工具只有一个（update_activity）。读不需要工具——整份资料已经在上下文里了。

_WRITE_HINT = """
## 客服在对话里补充活动信息时（重要）
客服会陆续告诉你活动信息（时间、集合地点、价格、名额、是否室外、分组等），
或者粘贴整篇推文让你录入。

这种情况**必须调用 update_activity 真正写入**，不能只在回复里说「记住了」——那样等于没记。

判断是不是补充信息，按这几条：
· 输入很短、只有一个词或一个值（「室外」「不分组」「25人」）→ 这是补充信息，直接写
· 带着具体值说出来的（「这期是室外」「每组4到5个」）→ 这是补充信息，直接写
· 带问号、带「吗/呢/多少/几/哪/怎么」的 → 这是在问问题，正常回答，不要写
· 说不准的时候，**宁可先追问一句「您是说要改成室外吗？」也不要猜着写**

· 只写客服明确说过的字段，没说的不要猜，尤其不要填「校门口」「上午」这种像模像样的默认值
· 写不进去（格式错、字段不允许）就如实转告，不要换个说法糊过去
· **不要在回复里报「还缺几项」**——右侧状态面板由程序实时算，比你说得准。你只管确认写了什么
"""


def _write_tool_schema() -> list:
    """只要 update_activity 这一个工具。"""
    for s in tools.SCHEMAS:
        if s["function"]["name"] == "update_activity":
            return [s]
    return []




# 输入分类：这句话是「补充活动信息」还是「在问问题」？
#
# 这一层**全部交给模型判断，代码里没有任何关键词表**。
# 之前试过用规则判定枚举值，代价是被反复证伪：
#   · 「室内还是室外」被当成问句，写不进去
#   · 「这次活动不是室内的」被写成「室内」，正好反了
#   · 「如果下雨就转室内」被当成赋值
#   · 「不分组」被否定词黑名单误杀
# 每修一次都要加一条规则，说明「判断这句话在说什么」本来就是语义问题，
# 规则只能覆盖字面，覆盖不了意义。所以这一层回到模型。
#
# 代码只留下**它该管的事**：字段白名单、类型校验、写前备份（在 store 里）。
# 判断归模型，纪律归代码。

_CLASSIFY_SYSTEM = """你是客服助手的输入判断器。判断客服这句话属于哪一类。

A. update —— 在补充或修改这一期活动信息
   例：「室外」「不分组」「这期是室外活动」「集合点改到五教104」「每组4到5人」
       「不是室内，是室外」「室内还是室外：室外」
B. question —— 在问问题，或者要一段文案
   例：「室内有空调吗」「几点集合」「这次活动是不是室内的」「帮我写群文案」

判断要点：
· 只给了一个词或一个值、没有任何疑问语气 → update
· 有否定或条件时，先正确理解含义再定值：
  「不是室内」→ 室外；「如果下雨就转室内」→ 这不是在改活动属性，按 question 处理
· 拿不准的时候选 question（宁可多问一句，也不要乱写库）
· 只抽客服**明确说过**的字段，没说的一律不要填，不要编「校门口」「上午」这种默认值

可选字段（只能用这些键名）：
__FIELDS__

值的要求：
· date 用 YYYY-MM-DD；capacity / duration_minutes 用数字；pricing 是 {"model":..,"tiers":[{"label":..,"price":数字}]}
· indoor_outdoor 只能是「室内」或「室外」这样的短语，不要写成一整段说明
· grouping 用「分组」或「不分组」；group_size 用「4-5人」这类短语
· 其余是字符串

只输出 JSON：
{"kind": "update" | "question", "updates": {字段: 值}, "reason": "一句话说明依据"}
"""


def _field_menu() -> str:
    import store
    import knowledge
    act = knowledge.get_activity() or {}
    lines = []
    for k, desc in store.ALLOWED_FIELDS.items():
        cur = act.get(k)
        cur_txt = "（空）" if cur in (None, "", [], {}) else "（现值：" + str(cur)[:24] + "）"
        lines.append(f"- {k}：{desc}{cur_txt}")
    return "\n".join(lines)


def classify_input(question: str, activity_id: Optional[str] = None) -> Dict[str, Any]:
    """让模型判断「补充信息」还是「提问」。返回 {kind, updates, reason}。"""
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
    u = resp.usage
    try:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}

    kind = data.get("kind")
    if kind not in ("update", "question"):
        kind = "question"          # 判不出来就当提问，绝不瞎写
    updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    return {
        "kind": kind,
        "updates": updates,
        "reason": data.get("reason", ""),
        "usage": {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                  "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                  "total_tokens": getattr(u, "total_tokens", 0) or 0},
    }


def _answer_context(question: str, activity_id: Optional[str],
                    history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """纯问答：把这一期资料给模型，直接回答。不提供任何工具。"""
    import knowledge
    ctx = knowledge.build_context(activity_id)
    msgs: List[Dict[str, Any]] = [
        {"role": "system", "content": _CTX_SYSTEM + "\n\n" + ctx},
    ]
    for m in (history or [])[-6:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})

    resp = _client.chat.completions.create(
        model=config.MODEL, messages=msgs, temperature=config.TEMPERATURE)
    u = resp.usage
    return {
        "answer": resp.choices[0].message.content or "",
        "trace": ["<context>"],
        "usage": {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                  "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                  "total_tokens": getattr(u, "total_tokens", 0) or 0},
    }


def _confirm_written(question: str, updates: Dict[str, Any], activity_id: str,
                     ok: bool, err: str) -> Dict[str, Any]:
    """写完（或写失败）之后，让模型用一句人话回复客服。"""
    import knowledge
    import store
    act = knowledge.get_activity(activity_id) or {}
    left = [m["label"] for m in store.missing_fields(act)]

    if ok:
        done = "、".join(f"{k}={v}" for k, v in updates.items())
        note = (f"已经写入的字段：{done}\n"
                f"这一期还缺：{'、'.join(left) if left else '无'}")
    else:
        note = f"写入失败，原因是：{err}"

    resp = _client.chat.completions.create(
        model=config.MODEL,
        messages=[
            {"role": "system", "content":
             "你在回复客服（不是家长）。用一到两句话说明这次录入的结果："
             "写了什么 / 为什么没写进去。已经写成功就简短确认，不要重复罗列资料。"
             "缺项由界面右侧显示，你只要在确实有必要时提一句。"},
            {"role": "user", "content": f"客服说的话：{question}\n\n系统实际情况：\n{note}"},
        ],
        temperature=0,
    )
    u = resp.usage
    return {
        "answer": resp.choices[0].message.content or "",
        "trace": ["classify", "update_activity"],
        "usage": {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                  "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                  "total_tokens": getattr(u, "total_tokens", 0) or 0},
    }


def _valid_updates(updates: Dict[str, Any]):
    """代码只做校验：字段必须在白名单里。判断归模型，纪律归代码。"""
    import store
    good, bad = {}, []
    for k, v in (updates or {}).items():
        if k in store.ALLOWED_FIELDS and v not in (None, "", [], {}):
            good[k] = v
        elif k not in store.ALLOWED_FIELDS:
            bad.append(f"未知字段 {k}")
        else:
            bad.append(f"{k} 是空值")
    return good, bad


def answer_chat(
    question: str,
    activity_id: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """所有输入走对话框。

    流程：
        ① 模型判断这是「补充信息」还是「提问」（classify_input）
        ② 补充信息 → 代码校验字段白名单 → store 写入（带备份）→ 模型说一句确认
        ③ 提问     → 把这一期资料给模型，直接回答

    全流程没有关键词表。判断是模型的事，写入纪律是代码的事。
    """
    import knowledge
    aid = activity_id or knowledge.current_activity_id()

    decided = classify_input(question, aid)
    if verbose:
        print(f"  🧭 模型判断：{decided['kind']} （{decided['reason'][:40]}）")
    total = dict(decided["usage"])

    if decided["kind"] == "update" and decided["updates"]:
        good, bad = _valid_updates(decided["updates"])
        if verbose:
            print(f"  📝 抽到的字段：{good}   被拒：{bad}")
        if not good:
            out = _answer_context(question, aid, history)
            out["wrote"], out["write_errors"], out["missing"] = [], bad, []
            return out

        raw = tools.call_tool("update_activity", json.dumps(
            {"activity_id": aid, "updates": good}, ensure_ascii=False))
        try:
            res = json.loads(raw)
        except Exception:
            res = {"ok": False, "error": raw}

        ok = bool(res.get("ok"))
        err = "" if ok else str(res.get("error") or res)
        out = _confirm_written(question, good, aid, ok, err)
        out["wrote"] = sorted(good) if ok else []
        out["write_errors"] = [] if ok else [err]

        act = knowledge.get_activity(aid) or {}
        try:
            import store
            out["missing"] = store.missing_fields(act)
        except Exception:
            out["missing"] = []
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            out["usage"][k] = total.get(k, 0) + out["usage"].get(k, 0)
        return out

    out = _answer_context(question, aid, history)
    out["wrote"], out["write_errors"], out["missing"] = [], [], []
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        out["usage"][k] = total.get(k, 0) + out["usage"].get(k, 0)
    return out
