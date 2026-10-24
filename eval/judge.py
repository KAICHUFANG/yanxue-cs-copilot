"""
LLM 裁判（LLM-as-judge，用模型当评审）

为什么需要它
------------
正则断言只能查「某个词出现没出现」，查不了这四件事：
    · 有没有把家长问的每一句都答到（漏答）
    · 语气对不对（能不能直接复制发给家长）
    · 有没有「根据资料显示」这种不该发给家长的元话术
    · 事实是不是真被上下文支持

所以再加一层：让一个模型拿着【知识库原文 + 家长问题 + 待评答案】打分，
输出结构化 JSON，并**必须给出理由和证据句**。只给分数不给理由的裁判不可信。

用法（一般不单独跑，由 eval/ab_v4.py 调用）：
    from judge import judge
    v = judge(question, answer, context)
"""

import json
import re
from typing import Any, Dict, Optional

from openai import OpenAI

import config

_client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)

JUDGE_SYSTEM = """你是一个严格的质检员，评审「研学活动客服」给家长的回复。

你会看到三样东西：
  1. 【知识库】这一期的全部真实资料
  2. 【家长问题】家长原话
  3. 【候选回复】待评审的回复

请按四个维度打分，每个维度 1-5 分（3 分及格，5 分优秀）：

A. 准确性：回复里的每个事实，都能在知识库里找到依据吗？
   编造、推测、把别期数据套用，都算重大错误，直接 1-2 分。

B. 完整性：家长问的每一件事，都回答了吗？
   一句话问四件事只答了三件，扣分。

C. 可直接发送：这段文字客服能直接复制粘到群里吗？
   出现「根据资料」「上面提到的知识库」「作为 AI」这类元话术，扣分。

D. 分寸：该说「没有记录」的时候说了吗？该纠正家长错误信息的时候纠正了吗？
   含糊其辞、顺着家长的错误前提走，扣分。

判定规则：
  · 只要有一个维度 ≤ 2 分，verdict 判为 "fail"
  · A 维度出现编造，无论其他多好，verdict 必须是 "fail"

只输出 JSON，不要任何解释文字：
{
  "accuracy": 1-5,
  "completeness": 1-5,
  "sendable": 1-5,
  "tact": 1-5,
  "verdict": "pass" | "fail",
  "problems": ["具体问题1", "具体问题2"],
  "evidence": "指出哪句话没有知识库依据，或哪一问没答"
}
"""


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    """模型有时会包一层 ```json，兜一下。"""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def judge(question: str, answer: str, context: str, temperature: float = 0.0) -> Dict[str, Any]:
    """给一条回复打分。失败时返回 verdict="error"，不要静默当通过。"""
    user_msg = (
        f"【知识库】\n{context}\n\n"
        f"【家长问题】\n{question}\n\n"
        f"【候选回复】\n{answer}"
    )
    try:
        resp = _client.chat.completions.create(
            model=config.MODEL,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        data = _parse_json(resp.choices[0].message.content or "")
    except Exception as e:
        return {"verdict": "error", "problems": [f"裁判调用失败：{e}"], "scores": {}}

    if not data:
        return {"verdict": "error", "problems": ["裁判返回无法解析"], "scores": {}}

    scores = {k: data.get(k) for k in ("accuracy", "completeness", "sendable", "tact")}
    # 兜底：模型说 pass 但有维度 ≤2，按失败算。裁判也会心软，代码不能心软。
    low = [k for k, v in scores.items() if isinstance(v, int) and v <= 2]
    verdict = data.get("verdict", "fail")
    if low:
        verdict = "fail"

    return {
        "verdict": verdict,
        "scores": scores,
        "avg": round(sum(v for v in scores.values() if isinstance(v, int)) / max(
            1, len([v for v in scores.values() if isinstance(v, int)])), 2),
        "problems": data.get("problems", []),
        "evidence": data.get("evidence", ""),
    }
