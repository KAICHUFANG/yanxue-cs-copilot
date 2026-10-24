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


# 纯枚举值的补充信息：这些是**确定性判断**，不该交给模型去猜。
#
# 为什么需要这一段（真实故障）：
#   客服在对话框里打「室外」两个字，模型把它当成提问，回答「这一期我这边没有记录…」，
#   数据一个没写进去；而打「室内」两个字它就正常写了。
#   同一类输入两种结果 —— 说明这不是能力问题，是这一层根本不该让模型判断。
#
#   判断规则很硬：整句去掉修饰后，剩下的就是一个确定的枚举值，且不含疑问词。
#   满足就直接写库，模型只负责说一句确认。
_ENUM_MAP = {
    "室内": ("indoor_outdoor", "室内"),
    "室外": ("indoor_outdoor", "室外"),
    "不分组": ("grouping", "不分组"),
    "分组": ("grouping", "分组"),
    "已结束": ("status", "已结束"),
    "报名中": ("status", "报名中"),
    "待确认": ("status", "待确认"),
}

_QUESTION_HINTS = ["还是", "吗", "？", "?", "哪", "多少", "怎么", "几", "是不是", "有没有"]
_PREFIXES = ["这期是", "本期是", "这一期是", "第十六期是", "活动是", "是的", "是",
             "这期", "本期", "第", "期"]
_SUFFIXES = ["活动", "的", "啊", "吧", "哦", "呢", "呀", "了"]


def _pure_enum(text: str):
    """整段就是一个枚举值时返回 (字段, 值)，否则 None。"""
    if any(h in text for h in _QUESTION_HINTS):
        return None
    t = re.sub(r"[^\u4e00-\u9fa5]", "", text)
    for p in _PREFIXES:
        if t.startswith(p):
            t = t[len(p):]
            break
    for s in _SUFFIXES:
        while t.endswith(s) and len(t) > len(s):
            t = t[:-len(s)]
    return _ENUM_MAP.get(t)


# 「值词出现在句尾」时，什么情况下算赋值、什么情况下算问句：
#   这期是室外活动   → 赋值（只有一个「室外」）
#   室内还是室外     → 问句（「还是」+ 只出现一次）
#   室内还是室外 室外 → 赋值（出现了两次，最后那个是答案）
# 这条规则是为了兼容客服直接照着面板字段名抄「室内还是室外」的习惯。
_ENUM_WORDS = ["室外", "室内", "不分组", "分组", "已结束", "报名中", "待确认"]


def _enum_probe(text: str):
    """返回 (字段, 值)。值是 None 表示「只说了字段名，没给值，应该反问」。"""
    raw = (text or "").strip()

    # ① 「字段：值」写法
    for sep in ("：", ":", "=", "＝"):
        if sep in raw:
            hit = _pure_enum(raw.rsplit(sep, 1)[-1])
            if hit:
                return hit

    # ② 整段就是一个枚举值
    hit = _pure_enum(raw)
    if hit:
        return hit

    # ③ 值词在句尾
    for word in _ENUM_WORDS:
        idx = raw.rfind(word)
        if idx < 0:
            continue
        tail = raw[idx + len(word):].strip(" 的了啊吧哦呢呀，。！")
        if tail:
            continue
        if raw.count(word) >= 2 or "还是" not in raw:
            return _ENUM_MAP[word]

    # ④ 只提到字段名，没给值
    if "室内" in raw or "室外" in raw:
        return ("indoor_outdoor", None)
    if "分组" in raw:
        return ("grouping", None)
    return None


def answer_chat(
    question: str,
    activity_id: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """v6：全部输入走对话框。读=直接答，写=调 update_activity。

    返回里除了 answer，还有：
        wrote        这次写了哪几个字段（空列表 = 没写）
        write_errors 写入失败的原因（空列表 = 都成功了）
        trace        实际调用的工具
        missing      写完之后这一期还缺什么
    """
    import knowledge

    ctx = knowledge.build_context(activity_id)
    sys_msg = _CTX_SYSTEM + "\n\n" + ctx + "\n\n" + _WRITE_HINT

    trace: List[str] = []
    wrote: List[str] = []
    write_errors: List[str] = []

    # ---- 第一步：纯枚举值由代码直接写，不问模型 ----
    quick = _enum_probe(question)
    if quick and activity_id:
        field, value = quick

        if value is None:
            # 只说了字段名，没给值。不要猜，直接反问一句要个词。
            sys_msg += (f"\n\n【系统判断】\n客服只提到了字段 `{field}`，没有说具体值。\n"
                        f"请**只问一句**要她补一个值（比如「这期是室内还是室外？说一个词就行」），"
                        f"不要调用任何工具，也不要回答家长视角的问题。")
            if verbose:
                print(f"  ❓ 只提到字段 {field}，等客服给值")
            quick = None
            tool_msgs_override = []
        else:
            tool_msgs_override = None
            raw = tools.call_tool("update_activity", json.dumps(
                {"activity_id": activity_id, "updates": {field: value}}, ensure_ascii=False))
            trace.append("update_activity")
            wrote.append(field)
            try:
                res = json.loads(raw)
            except Exception:
                res = {"ok": False, "error": raw}
            if res.get("ok"):
                sys_msg += (f"\n\n【系统已经做完的事】\n"
                            f"这条输入被识别为补充信息，已直接写入：{field} = {value}。\n"
                            f"你只需要用一句话确认，**不要调用任何工具**，"
                            f"也不要说「还缺几项」——右侧面板会显示。")
            else:
                write_errors.append(str(res.get("error") or res))
                sys_msg += (f"\n\n【系统写入失败】\n原因：{res.get('error') or res}\n"
                            f"请如实告诉客服没写进去，并说明原因。")
            if verbose:
                print(f"  ⚡ 代码直接写：{field} = {value} → ok={res.get('ok')}")
    else:
        tool_msgs_override = None

    msgs: List[Dict[str, Any]] = [{"role": "system", "content": sys_msg}]
    for m in (history or [])[-6:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})

    # 代码已经写完（或只是问了字段名）的情况就不给工具了，避免它自作主张再写一遍
    if quick and quick[1] is not None:
        tool_msgs = []
    elif tool_msgs_override == []:
        tool_msgs = []
    else:
        tool_msgs = _write_tool_schema()
    pt = ct = 0

    for round_no in range(3):
        kwargs: Dict[str, Any] = {
            "model": config.MODEL,
            "messages": msgs,
            "temperature": config.TEMPERATURE,
        }
        if tool_msgs:
            kwargs["tools"] = tool_msgs
            kwargs["tool_choice"] = "auto"

        resp = _client.chat.completions.create(**kwargs)
        if resp.usage:
            pt += resp.usage.prompt_tokens or 0
            ct += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message

        if not msg.tool_calls:
            answer = msg.content or ""
            msgs.append({"role": "assistant", "content": answer})
            break

        msgs.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            result = tools.call_tool(tc.function.name, tc.function.arguments)
            trace.append(tc.function.name)
            # 从参数里捞出这次写了哪些字段，供界面刷新状态；
            # 同时看返回值判断到底写成功没有——不能光凭模型说「已写入」
            try:
                args = json.loads(tc.function.arguments or "{}")
                wrote.extend((args.get("updates") or {}).keys())
                res = json.loads(result)
                if isinstance(res, dict) and res.get("ok") is False:
                    write_errors.append(str(res.get("error") or res))
            except Exception:
                pass
            if verbose:
                print(f"  🔧 {tc.function.name} → {str(result)[:120]}")
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    else:
        answer = "（抱歉，我没能在限定轮次内给出结果，请换个说法再试一次）"

    # 写没写成功，以磁盘上的实际情况为准，不看模型怎么说
    missing: List[Dict[str, str]] = []
    if wrote:
        try:
            import store
            act = knowledge.get_activity(activity_id)
            if act:
                missing = store.missing_fields(act)
        except Exception:
            pass

    return {
        "answer": answer,
        "trace": trace or ["<context>"],
        "wrote": sorted(set(wrote)),
        "write_errors": write_errors,
        "missing": missing,
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }
