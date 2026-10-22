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
