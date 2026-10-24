"""
A/B 对比：规则路由 vs 让模型自己选工具

为什么要做这个实验
------------------
`router.py` 用关键词表判断意图。这套东西能跑，但有个天然毛病：
关键词之间会互相干扰。实测踩过两次：
  · 为了认「车能开进学校吗」加了单字「车」→ 「路上堵车」被误判成进校流程
  · 为了认「怎么报名」加了「怎么报名」→ 「第十期怎么报名」不再如实回答「没有记录」
每修一次都要重新跑一遍回归，关键词表越长越难维护。

但「太僵硬」只是感觉。要决定换不换，得有数字。所以这个脚本：
同一批用例，跑两遍——
  A. rules：现有架构（router 判意图 → 代码执行工具 → 模型组织语言）
  B. llm  ：把 router.plan 直接废掉，回到标准 Function Calling（模型自己选工具）
然后逐条对比。

注意 B 不是为了赢，是为了知道 A 到底带来了多少收益。如果 B 差不多，
那 A 的复杂度就不值这个钱，应该删掉。

用法：
    .venv\\Scripts\\python.exe eval\\ab_compare.py
    .venv\\Scripts\\python.exe eval\\ab_compare.py --file cases_blind.json
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJ = EVAL_DIR.parent
TMP_KB = EVAL_DIR / "tmp_activities_ab.json"

# ---- 必须在 import config 之前设置，理由同 run_eval.py ----
# 评测固定用 eval/fixtures 里的题库快照，不直接吃真实业务数据——
# 否则客服今天填一个集合地点，昨天的评测结论就作废（踩过）。
_FIXTURE = EVAL_DIR / "fixtures" / "activities_fixture.json"
_SRC = _FIXTURE if _FIXTURE.exists() else PROJ / "data" / "activities.json"
shutil.copy2(_SRC, TMP_KB)
os.environ["ACTIVITIES_FILE"] = str(TMP_KB)
os.environ.setdefault("TEMPERATURE", "0")

sys.path.insert(0, str(PROJ))

import agent      # noqa: E402
import config     # noqa: E402
import router     # noqa: E402
from run_eval import grade, load_cases  # noqa: E402


def build_full_kb_dump() -> str:
    """把整份知识库拼成一段上下文。

    面向的是「知识库很小」这个事实：activities + policies + 流程 + 文案模板
    加起来才一万多字。与其让模型猜该查哪个工具，不如一次全给它。

    这是第三种架构（C. 全量投喂）的核心 —— 极端灵活，零路由代码。
    """
    import tools

    kb = tools.load_kb()
    procs = tools._load_processes()

    parts = ["【知识库全文】以下是本项目的全部权威资料，请只依据这里的内容回答。",
             "\n=== 一、活动列表 ==="]
    for a in kb.get("activities", []):
        parts.append(json.dumps(a, ensure_ascii=False, indent=1))

    parts.append("\n=== 二、跨期通用规则 ===")
    parts.append(json.dumps(kb.get("policies", {}), ensure_ascii=False, indent=1))

    parts.append("\n=== 三、品牌信息 ===")
    parts.append(json.dumps(kb.get("brand", kb.get("brand_info", {})), ensure_ascii=False, indent=1))

    parts.append("\n=== 四、流程类知识（定稿文案，需原文输出）===")
    parts.append(json.dumps(procs, ensure_ascii=False, indent=1))

    parts.append("\n=== 五、活动群文案模板 ===")
    import config as _cfg
    with open(_cfg.DATA_DIR / "group_messages.json", encoding="utf-8") as f:
        parts.append(json.dumps(json.load(f), ensure_ascii=False, indent=1))

    parts.append(
        "\n【回答要求】只答家长问的部分，不要复述整份资料。"
        "资料里没有的，如实说没有记录，禁止推测或编造。"
    )
    return "\n".join(parts)


def run_one(case: dict, mode: str) -> dict:
    """跑一条用例。mode = rules / llm / stuff"""
    if mode == "llm":
        # 关键：把规则路由整个废掉，只留标准 Function Calling。
        # 不删代码，只是在这次运行里让它永远回答「判断不出来」。
        router.plan = lambda question: None

    t0 = time.time()
    try:
        if mode in ("stuff", "hybrid"):
            # 混合：要写入的走工具（唯一能落库的方式），其余读类问题直接全量投喂。
            # 这样 router.py 里那 9 个意图的关键词表，只需要保留「这是不是写入」一条判断。
            use_tools = mode == "hybrid" and router.detect_intent(case["q"]) == "update"
            if use_tools:
                conv = [{"role": "user", "content": case["q"]}]
                res = agent.ask(conv, verbose=False)
            else:
                msgs = [
                    {"role": "system", "content": agent.SYSTEM_PROMPT + "\n\n" + build_full_kb_dump()},
                    {"role": "user", "content": case["q"]},
                ]
                resp = agent._client.chat.completions.create(
                    model=config.MODEL, messages=msgs, temperature=config.TEMPERATURE,
                )
                u = resp.usage
                res = {
                    "answer": resp.choices[0].message.content or "",
                    "trace": ["<全量知识库>"],
                    "usage": {"total_tokens": getattr(u, "total_tokens", 0) or 0},
                }
        else:
            conv = [{"role": "user", "content": case["q"]}]
            res = agent.ask(conv, verbose=False)
    except Exception as e:
        res = {"answer": f"【调用失败】{e}", "trace": [], "usage": {}}
    res["_elapsed"] = time.time() - t0
    return res


def main() -> None:
    config.check()
    cases = load_cases()

    # 两个模式必须都从「干净的路由」开始，所以把原函数存下来
    original_plan = router.plan
    MODES = ["rules", "llm", "stuff", "hybrid"]
    LABEL = {"rules": "A 规则路由", "llm": "B 模型选工具",
             "stuff": "C 全量投喂", "hybrid": "D 读写分离"}

    print("=" * 78)
    print(f"四种架构对比：{len(cases)} 条 × 4")
    print("  A. rules  —— router.py 关键词判意图，代码执行工具（现状）")
    print("  B. llm    —— 废掉 router，模型自己从 8 个工具里选")
    print("  C. stuff  —— 不用工具，知识库全文一次塞进上下文（零路由代码）")
    print("  D. hybrid —— 写入走工具，其余全量投喂（读写分离）")
    print("=" * 78)

    # 先量一下全量投喂有多大
    dump = build_full_kb_dump()
    print(f"  （知识库全文 {len(dump)} 字，约 {len(dump) // 2} tokens）")
    print()

    rows = []
    t_all = time.time()

    for i, case in enumerate(cases, 1):
        got = {}
        for mode in MODES:
            router.plan = original_plan
            res = run_one(case, mode)
            g = grade(case, res)
            # C / D 的读类问题根本不选工具，「工具选对」这一维不适用，不能让它在这一维被扣分
            if mode in ("stuff", "hybrid") and "<全量知识库>" in g["trace"]:
                g["checks"]["tool_ok"] = True
                g["tool_na"] = True
                # 注意：改完 checks 必须重算 passed，否则会拿旧结果判负（踩过）
                g["passed"] = all(g["checks"].values())
            got[mode] = {**g, "elapsed": res.get("_elapsed", 0)}

        rows.append({
            "id": case["id"], "category": case["category"], "q": case["q"], "got": got,
        })

        mark = " ".join(f"{m[0].upper()}{'✅' if got[m]['passed'] else '❌'}" for m in MODES)
        flag = "" if len({got[m]["passed"] for m in MODES}) == 1 else "   ← 有差异"
        print(f"[{i:2d}/{len(cases)}] {mark}  {case['id']} {case['category']:<6} {case['q'][:20]}{flag}")

    router.plan = original_plan   # 恢复，别影响后续
    elapsed = time.time() - t_all

    # ---------------- 汇总 ----------------
    n = len(rows)
    passcnt = {m: sum(1 for r in rows if r["got"][m]["passed"]) for m in MODES}
    dims = ["tool_ok", "facts_ok", "any_ok", "clean_ok"]
    labels = {"tool_ok": "工具/来源选对", "facts_ok": "关键事实齐全",
              "any_ok": "命中正确说法", "clean_ok": "无编造/错误口径"}

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    header = "  " + f"{'':<16}" + "".join(f"{LABEL[m]:>16}" for m in MODES)
    print(header)
    print("  " + f"{'通过率':<16}" + "".join(
        f"{passcnt[m]:>8}/{n} {passcnt[m] / n * 100:>5.1f}%" for m in MODES))

    print()
    print("  分维度：")
    print("  " + f"{'维度':<16}" + "".join(f"{LABEL[m]:>16}" for m in MODES))
    for d in dims:
        cells = []
        for m in MODES:
            ok = sum(1 for r in rows if r["got"][m]["checks"][d])
            cells.append(f"{ok:>8}/{n}")
        print("  " + f"{labels[d]:<16}" + "".join(f"{c:>16}" for c in cells))

    print()
    print("  成本：")
    for m in MODES:
        tt = sum(r["got"][m].get("elapsed", 0) for r in rows)
        kk = sum(r["got"][m].get("tokens", 0) for r in rows)
        print(f"  {LABEL[m]:<16} 平均 {tt / n:>5.2f}s   总 tokens {kk:>7}")
    print(f"  总耗时 {elapsed:.1f}s")

    # ---------------- 逐条差异 ----------------
    diff = [r for r in rows if len({r["got"][m]["passed"] for m in MODES}) > 1]
    print()
    print("=" * 78)
    print(f"三种架构结果不一致的用例：{len(diff)} 条")
    print("=" * 78)
    for r in diff:
        print(f"\n【{r['id']}】{r['category']}  {r['q']}")
        for m in MODES:
            g = r["got"][m]
            status = "✅" if g["passed"] else "❌"
            print(f"  {LABEL[m]} {status}  trace={g['trace']}")
            if not g["passed"]:
                bad = [d for d, ok in g["checks"].items() if not ok]
                print(f"      失败维度 {bad}")
                print(f"      answer={g['answer'][:120].replace(chr(10), ' ')}")

    # ---------------- 落盘 ----------------
    out = EVAL_DIR / "ab_report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cases": n,
            "results": {
                m: {
                    "passed": passcnt[m],
                    "tokens": sum(r["got"][m].get("tokens", 0) for r in rows),
                    "seconds": round(sum(r["got"][m].get("elapsed", 0) for r in rows), 1),
                } for m in MODES
            },
            "diff": [
                {"id": r["id"], "q": r["q"],
                 **{m: {"passed": r["got"][m]["passed"], "trace": r["got"][m]["trace"],
                        "answer": r["got"][m]["answer"]} for m in MODES}}
                for r in diff
            ],
        }, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{out}")


if __name__ == "__main__":
    main()
