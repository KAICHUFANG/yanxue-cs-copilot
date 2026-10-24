"""
v4 管线：把「一次调用」拆成「拆题 → 带引用草稿 → 代码校验引用 → 裁判复审 → 修订」

为什么这么干
------------
v3 是单次调用：把 1.4 万字资料整个给模型，让它一次写好。
23/23 之后没有提升空间了，因为**它错在哪里我看不见**——
回答里哪句话有依据、哪句话是编的，都混在一段自然语言里。

v4 的思路是把这个黑盒拆开，每一步都能被检查：

    ① 拆题      家长一句话问了几件事？先列出来，避免漏答
    ② 草稿      回答必须附 quotes（逐字摘录的依据句）
    ③ 校验      **代码**检查每个 quote 是否真的出现在资料里（不靠模型自觉）
    ④ 复审      裁判对照子问题清单，查漏答、查无依据的话
    ⑤ 修订      有问题就带着具体问题重写，最多 N 轮

关键点：第 ③ 步是硬校验，不是提示词。模型说「我引用了原文」不算数，
必须逐字在资料里找得到。这是整条管线里唯一不依赖模型自觉的环节。

成本说明：一条问题 4~6 次调用，是 v3 的 5 倍左右。DeepSeek 便宜，可以接受；
但延迟会从 1.4 秒涨到 8~12 秒。要不要用得看场景。
"""

import json
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI

import config
import knowledge

_client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """归一化，用于「这句话是否真的出现在资料里」的比对。

    去掉所有空白和常见标点，避免因为换行、全半角差异误判成假引用。
    """
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[，。、；：！？,.;:!?\"'“”‘’（）()【】\[\]—…]", "", s)
    return s


def _ask(system: str, user: str, json_mode: bool = False,
         temperature: float = 0.0) -> str:
    kwargs: Dict[str, Any] = {
        "model": config.MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


def _parse(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# ① 拆题
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = """你是客服质检助手。把家长的一条消息拆成若干个独立的小问题。

规则：
- 家长只问一件事，就只输出一条。
- 家长一句话里塞了多件事（比如「多少钱？几年级？几点结束？」），就拆成多条。
- 拆出来的每条要能单独回答，用家长的口吻，不要改写成书面语。
- 不要自己去回答，也不要补充家长没问的东西。

只输出 JSON：{"sub_questions": ["...", "..."]}"""


def decompose(question: str) -> List[str]:
    out = _parse(_ask(_DECOMPOSE_SYSTEM, question, json_mode=True))
    subs = (out or {}).get("sub_questions") or [question]
    subs = [s.strip() for s in subs if str(s).strip()]
    return subs or [question]


# ---------------------------------------------------------------------------
# ② 带引用的草稿
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM = """你是「山海科普研学」的家长咨询助手，在微信群里答疑。

给你这一期的全部资料。请回答家长的问题。

硬性要求：
1. **每个事实都必须能落到资料里的原话上**。答完在 quotes 里逐字摘录你依据的原文句子。
   摘录必须是资料里连续的一整句，不许改写、不许拼接、不许自己总结。
2. 资料里没有的，如实说「这一期我这边没有记录，建议看当期推文或群公告」，
   并把这条写进 unsure 列表。**禁止推测，禁止编一个听起来合理的原因。**
3. 家长预设了错误前提（比如「不是在北门集合吗」），要温和纠正，不能顺着承认。
4. 回答要能直接复制发给家长：口语化、先给结论、150 字内，
   不要出现「根据资料」「以上信息」这类元话术。

只输出 JSON：
{
  "answer": "给家长的回复",
  "quotes": ["逐字摘录的资料原文，一条一句"],
  "unsure": ["资料里没有、需要人工确认的点"]
}"""


def draft(question: str, context: str, subqs: List[str]) -> Dict[str, Any]:
    subs = "\n".join(f"- {s}" for s in subqs)
    user = (f"【资料】\n{context}\n\n"
            f"【家长原话】\n{question}\n\n"
            f"【需要逐一回答的小问题】\n{subs}\n\n"
            f"必须每一条都回答到。")
    out = _parse(_ask(_DRAFT_SYSTEM, user, json_mode=True)) or {}
    return {
        "answer": (out.get("answer") or "").strip(),
        "quotes": [str(q) for q in (out.get("quotes") or [])],
        "unsure": [str(u) for u in (out.get("unsure") or [])],
    }


# ---------------------------------------------------------------------------
# ③ 代码校验引用（这一层不依赖模型自觉）
# ---------------------------------------------------------------------------

def verify_quotes(quotes: List[str], context: str) -> List[str]:
    """返回伪造的引用（在资料里找不到的那些）。空列表 = 全部通过。"""
    haystack = _norm(context)
    fake = []
    for q in quotes:
        needle = _norm(q)
        # 太短的引用说明不了问题，跳过（避免「是」「可以」这类碎片误判）
        if len(needle) < 6:
            continue
        if needle not in haystack:
            fake.append(q)
    return fake


# ---------------------------------------------------------------------------
# ④ 复审
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = """你是严格的质检员。给你：资料、家长原话、需要回答的小问题、以及一份候选回复。

逐项检查：
1. 每个小问题都答到了吗？漏了哪个？
2. 回复里有没有资料**不支持**的说法？（编造、推测、把别期数据套过来）
3. 有没有顺着家长的错误前提走？
4. 有没有「根据资料」「以上」这类不能发给家长的话术？

如果全部通过，ok 为 true，revised 留空。
如果有问题，ok 为 false，把问题逐条写进 issues，并在 revised 里给出**改好的完整回复**
（要求：口语化、可直接发群、150 字内、不编造）。

只输出 JSON：
{"ok": true/false, "issues": ["..."], "revised": "改好的回复，ok 为 true 时留空"}"""


def review(question: str, context: str, subqs: List[str], answer: str) -> Dict[str, Any]:
    subs = "\n".join(f"- {s}" for s in subqs)
    user = (f"【资料】\n{context}\n\n"
            f"【家长原话】\n{question}\n\n"
            f"【需要回答的小问题】\n{subs}\n\n"
            f"【候选回复】\n{answer}")
    out = _parse(_ask(_REVIEW_SYSTEM, user, json_mode=True)) or {}
    return {
        "ok": bool(out.get("ok", False)),
        "issues": [str(i) for i in (out.get("issues") or [])],
        "revised": (out.get("revised") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def answer_v4(
    question: str,
    activity_id: Optional[str] = None,
    max_rounds: int = 2,
    verbose: bool = False,
) -> Dict[str, Any]:
    """完整管线。返回 answer + 每一步的轨迹，方便复盘它到底错在哪。"""
    ctx = knowledge.build_context(activity_id)
    steps: List[Dict[str, Any]] = []
    calls = 0

    # ① 拆题
    subqs = decompose(question)
    calls += 1
    steps.append({"step": "decompose", "sub_questions": subqs})
    if verbose:
        print(f"  ① 拆题：{len(subqs)} 个 → {subqs}")

    # ② 草稿 + ③ 校验（假引用就给一次机会重写）
    d = draft(question, ctx, subqs)
    calls += 1
    fake = verify_quotes(d["quotes"], ctx)
    steps.append({"step": "draft", "quotes": d["quotes"], "fake_quotes": fake})
    if verbose:
        print(f"  ② 草稿：{len(d['quotes'])} 条引用，伪造 {len(fake)} 条")

    retry = 0
    while fake and retry < 1:
        retry += 1
        fix = (f"你上一版的这些引用在资料里找不到，属于伪造，请重新摘录真实的原文句子：\n"
               + "\n".join(f"· {q}" for q in fake))
        d = draft(question + "\n\n" + fix, ctx, subqs)
        calls += 1
        fake = verify_quotes(d["quotes"], ctx)
        steps.append({"step": f"draft_retry{retry}", "quotes": d["quotes"], "fake_quotes": fake})
        if verbose:
            print(f"  ③ 重摘：伪造剩 {len(fake)} 条")

    answer = d["answer"]

    # ④⑤ 复审 + 修订
    for r in range(max_rounds):
        rv = review(question, ctx, subqs, answer)
        calls += 1
        steps.append({"step": f"review{r + 1}", "ok": rv["ok"], "issues": rv["issues"]})
        if verbose:
            print(f"  ④ 复审{r + 1}：{'通过' if rv['ok'] else '不通过 → ' + str(rv['issues'])[:80]}")
        if rv["ok"] or not rv["revised"]:
            break
        answer = rv["revised"]

    return {
        "answer": answer,
        "trace": ["<v4>"],
        "steps": steps,
        "calls": calls,
        "fake_quotes": fake,
    }


# ---------------------------------------------------------------------------
# v5：双通道输出（发给家长的话 + 给客服的内部提示）
# ---------------------------------------------------------------------------
#
# 为什么加这一层：
#   v4 之后评测已经满分，再加调用没用——瓶颈不在模型，在知识库有 5 个字段是 null
#   （集合地点、集合时间、室内室外、是否分组、每组人数）。
#   家长最常问的恰恰就是这几项，系统只能答「没有记录」。
#
#   与其让模型把「不知道」说得更漂亮，不如让它**多给一句客服能立刻行动的话**：
#   这一项缺什么、该去问谁、在这之前怎么回家长、要不要转人工。
#
#   这一段不发给家长，所以可以放开写，不受「150 字、口语化」的限制。

_INTERNAL_SYSTEM = """你在给客服写内部备注，这段**不会发给家长**。

基于资料和这次的回复，写 2-4 条给客服的提示，每条一行，只在确实需要时写：
- 资料里缺哪一项、建议现在去问谁（负责人 / 策划组 / 老师）
- 这条回复有没有风险（承诺过头、可能被追问、口径不确定）
- 家长情绪如何，要不要打个电话
- 下一步该做什么

要求：具体、可执行、短。不要复述已经答过的事实。不要写「建议保持专业」这种空话。
如果没有需要提醒的，直接输出空字符串。

只输出 JSON：{"notes": ["...", "..."]}"""


def internal_notes(question: str, answer: str, context: str,
                   subqs: List[str], unsure: List[str]) -> List[str]:
    user = (f"【资料】\n{context}\n\n"
            f"【家长原话】\n{question}\n\n"
            f"【资料里查不到的点】\n" + ("\n".join(f"- {u}" for u in unsure) or "（无）") + "\n\n"
            f"【已经发给家长的回复】\n{answer}")
    out = _parse(_ask(_INTERNAL_SYSTEM, user, json_mode=True)) or {}
    return [str(n) for n in (out.get("notes") or []) if str(n).strip()]


def answer_v5(
    question: str,
    activity_id: Optional[str] = None,
    max_rounds: int = 2,
    verbose: bool = False,
) -> Dict[str, Any]:
    """v4 的完整管线 + 给客服的内部提示。"""
    r = answer_v4(question, activity_id=activity_id, max_rounds=max_rounds, verbose=verbose)

    # 从草稿步骤里捞回 unsure（资料里查不到的点）
    ctx = knowledge.build_context(activity_id)
    unsure: List[str] = []
    for s in r["steps"]:
        unsure.extend(s.get("unsure") or [])

    notes = internal_notes(question, r["answer"], ctx, [question], unsure)
    r["internal_notes"] = notes
    r["calls"] += 1
    if verbose:
        print(f"  ⑤ 内部提示 {len(notes)} 条")
    return r
