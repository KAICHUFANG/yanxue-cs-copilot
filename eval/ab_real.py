"""
v2（关键词路由）vs v3（无路由，当前活动上下文直答）

评测集：eval/cases_real.json —— 23 条**真实家长会说的问法**。
   · 10 条来自真实聊天记录（家长原话）
   · 13 条是模拟家长问法，但都不带期次
   · 一条写操作、一条跨期次对比都没收进来，因为那两类不是家长的需求

为什么不用原来的 cases.json 比：
   原来的 28 条里有 15 条带「第十六期」。而真实聊天记录里期次出现率是 0%——
   那 15 条是照着我自己的路由逻辑编的，拿它们比较等于让 v2 在自己的考卷上考试。

用法：
    .venv\\Scripts\\python.exe eval\\ab_real.py
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJ = EVAL_DIR.parent
TMP_KB = EVAL_DIR / "tmp_activities_real.json"

shutil.copy2(PROJ / "data" / "activities.json", TMP_KB)
os.environ["ACTIVITIES_FILE"] = str(TMP_KB)
os.environ.setdefault("TEMPERATURE", "0")

sys.path.insert(0, str(PROJ))

import re  # noqa: E402

import agent     # noqa: E402
import config    # noqa: E402
import knowledge  # noqa: E402


def grade(case: dict, answer: str) -> dict:
    miss = [k for k in case.get("must_contain", []) if k not in answer]
    any_list = case.get("must_contain_any", [])
    any_ok = (not any_list) or any(k in answer for k in any_list)
    bad = [k for k in case.get("must_not_contain", []) if k in answer]
    for pat in case.get("must_not_match", []):
        m = re.search(pat, answer)
        if m:
            bad.append(m.group(0))
    checks = {"facts_ok": not miss, "any_ok": any_ok, "clean_ok": not bad}
    return {"checks": checks, "passed": all(checks.values()),
            "miss": miss, "bad": bad, "answer": answer}


def run(case: dict, mode: str) -> dict:
    t0 = time.time()
    try:
        if mode == "v3":
            r = agent.answer_ctx(case["q"])
        else:
            r = agent.ask([{"role": "user", "content": case["q"]}], verbose=False)
    except Exception as e:
        r = {"answer": f"【失败】{e}", "trace": [], "usage": {}}
    r["_t"] = time.time() - t0
    return r


def main() -> None:
    config.check()
    cases = json.load(open(EVAL_DIR / "cases_real.json", encoding="utf-8"))["cases"]

    print("=" * 78)
    print(f"v2 vs v3：{len(cases)} 条真实家长问法（全部不带期次）")
    print(f"  v3 上下文：{knowledge.context_size()} 字 ≈ {knowledge.context_size() // 2} tokens")
    print("=" * 78)

    rows = []
    t0 = time.time()
    for i, case in enumerate(cases, 1):
        g2 = grade(case, run(case, "v2")["answer"])
        r3 = run(case, "v3")
        g3 = grade(case, r3["answer"])
        rows.append({"case": case, "v2": g2, "v3": g3, "tok3": r3.get("usage", {}).get("total_tokens", 0),
                     "t2": 0, "t3": r3.get("_t", 0)})
        mark = f"v2{'✅' if g2['passed'] else '❌'} v3{'✅' if g3['passed'] else '❌'}"
        flag = "" if g2["passed"] == g3["passed"] else "   ← 有差异"
        print(f"[{i:2d}/{len(cases)}] {mark}  {case['id']} {case['q'][:26]}{flag}")

    n = len(rows)
    p2 = sum(1 for r in rows if r["v2"]["passed"])
    p3 = sum(1 for r in rows if r["v3"]["passed"])
    tok3 = sum(r["tok3"] for r in rows)

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"  v2 关键词路由    {p2}/{n} = {p2 / n * 100:.1f}%")
    print(f"  v3 上下文直答    {p3}/{n} = {p3 / n * 100:.1f}%")
    print(f"  v3 平均 tokens   {tok3 / n:.0f}/条")
    print(f"  总耗时           {time.time() - t0:.1f}s")

    print("\n  分维度（关键事实 / 命中口径 / 无编造）：")
    for d, label in [("facts_ok", "关键事实齐全"), ("any_ok", "命中正确说法"), ("clean_ok", "无编造/错误口径")]:
        o2 = sum(1 for r in rows if r["v2"]["checks"][d])
        o3 = sum(1 for r in rows if r["v3"]["checks"][d])
        print(f"  {label:<14} v2 {o2:>2}/{n}      v3 {o3:>2}/{n}")

    diff = [r for r in rows if r["v2"]["passed"] != r["v3"]["passed"]]
    print()
    print("=" * 78)
    print(f"结果不一致：{len(diff)} 条")
    print("=" * 78)
    for r in diff:
        c = r["case"]
        print(f"\n【{c['id']}】{c['q']}")
        for tag in ("v2", "v3"):
            g = r[tag]
            print(f"  {tag} {'✅' if g['passed'] else '❌'}  缺={g['miss']} 多余={g['bad']}")
            print(f"     {g['answer'][:150].replace(chr(10), ' ')}")

    with open(EVAL_DIR / "ab_real_report.json", "w", encoding="utf-8") as f:
        json.dump({
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cases": n,
            "v2_passed": p2, "v3_passed": p3, "v3_tokens": tok3,
            "diff": [{"id": r["case"]["id"], "q": r["case"]["q"],
                      "v2": r["v2"]["answer"], "v3": r["v3"]["answer"]} for r in diff],
        }, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{EVAL_DIR / 'ab_real_report.json'}")


if __name__ == "__main__":
    main()
