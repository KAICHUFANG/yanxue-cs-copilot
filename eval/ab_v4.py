"""
v3（单次调用）vs v4（拆题→带引用→校验→复审）在难题集上的对比

评测分两层：
  · 正则层：must_contain / must_contain_any / must_not_match（查硬事实和禁用说法）
  · 裁判层：judge.py，四个维度打分（准确/完整/可直发/分寸）

用法：
    .venv\\Scripts\\python.exe eval\\ab_v4.py
    .venv\\Scripts\\python.exe eval\\ab_v4.py --judge false     # 只跑正则层，省时间
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJ = EVAL_DIR.parent
TMP_KB = EVAL_DIR / "tmp_activities_hard.json"

# 评测固定用 eval/fixtures 里的题库快照，不直接吃真实业务数据——
# 否则客服今天填一个集合地点，昨天的评测结论就作废（踩过）。
_FIXTURE = EVAL_DIR / "fixtures" / "activities_fixture.json"
_SRC = _FIXTURE if _FIXTURE.exists() else PROJ / "data" / "activities.json"
shutil.copy2(_SRC, TMP_KB)
os.environ["ACTIVITIES_FILE"] = str(TMP_KB)
os.environ.setdefault("TEMPERATURE", "0")

sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(EVAL_DIR))

import re  # noqa: E402

import agent      # noqa: E402
import config     # noqa: E402
import judge as J  # noqa: E402
import knowledge  # noqa: E402
import pipeline    # noqa: E402


def regex_grade(case: dict, answer: str) -> dict:
    miss = [k for k in case.get("must_contain", []) if k not in answer]
    any_list = case.get("must_contain_any", [])
    any_ok = (not any_list) or any(k in answer for k in any_list)
    bad = [k for k in case.get("must_not_contain", []) if k in answer]
    for pat in case.get("must_not_match", []):
        m = re.search(pat, answer)
        if m:
            bad.append(m.group(0))
    return {"ok": not miss and any_ok and not bad, "miss": miss, "bad": bad}


def main() -> None:
    config.check()
    use_judge = "--judge" not in sys.argv or "false" not in sys.argv
    cases = json.load(open(EVAL_DIR / "cases_hard.json", encoding="utf-8"))["cases"]
    ctx = knowledge.build_context()

    print("=" * 78)
    print(f"难题集：{len(cases)} 条   裁判：{'开' if use_judge else '关'}")
    print(f"当前活动：{knowledge.current_activity_id()}   上下文 {len(ctx)} 字")
    print("=" * 78)

    rows = []
    t_all = time.time()

    for i, case in enumerate(cases, 1):
        # --- v3 ---
        t0 = time.time()
        r3 = agent.answer_ctx(case["q"])
        t3 = time.time() - t0
        g3 = regex_grade(case, r3["answer"])

        # --- v4 ---
        t0 = time.time()
        r4 = pipeline.answer_v4(case["q"])
        t4 = time.time() - t0
        g4 = regex_grade(case, r4["answer"])

        j3 = J.judge(case["q"], r3["answer"], ctx) if use_judge else {"verdict": "-", "avg": 0, "scores": {}}
        j4 = J.judge(case["q"], r4["answer"], ctx) if use_judge else {"verdict": "-", "avg": 0, "scores": {}}

        rows.append({"case": case, "a3": r3["answer"], "a4": r4["answer"],
                     "g3": g3, "g4": g4, "j3": j3, "j4": j4,
                     "t3": t3, "t4": t4, "calls4": r4.get("calls", 0),
                     "fake4": r4.get("fake_quotes", [])})

        mark = (f"正则 v3{'✅' if g3['ok'] else '❌'} v4{'✅' if g4['ok'] else '❌'}  "
                f"裁判 v3:{j3['verdict']}({j3.get('avg')}) v4:{j4['verdict']}({j4.get('avg')})")
        print(f"[{i:2d}/{len(cases)}] {case['id']} {case['category']:<10} {mark}")

    n = len(rows)
    elapsed = time.time() - t_all

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    p3 = sum(1 for r in rows if r["g3"]["ok"])
    p4 = sum(1 for r in rows if r["g4"]["ok"])
    print(f"  正则层通过     v3 {p3}/{n}      v4 {p4}/{n}")
    if use_judge:
        q3 = sum(1 for r in rows if r["j3"]["verdict"] == "pass")
        q4 = sum(1 for r in rows if r["j4"]["verdict"] == "pass")
        print(f"  裁判层通过     v3 {q3}/{n}      v4 {q4}/{n}")
        s3 = sum(r["j3"].get("avg") or 0 for r in rows) / n
        s4 = sum(r["j4"].get("avg") or 0 for r in rows) / n
        print(f"  裁判均分       v3 {s3:.2f}      v4 {s4:.2f}")
        print()
        print("  分维度均分：")
        for k, label in [("accuracy", "准确性"), ("completeness", "完整性"),
                         ("sendable", "可直发"), ("tact", "分寸")]:
            a = sum((r["j3"].get("scores") or {}).get(k) or 0 for r in rows) / n
            b = sum((r["j4"].get("scores") or {}).get(k) or 0 for r in rows) / n
            print(f"    {label:<6} v3 {a:.2f}   v4 {b:.2f}")

    tt3 = sum(r["t3"] for r in rows) / n
    tt4 = sum(r["t4"] for r in rows) / n
    cc4 = sum(r["calls4"] for r in rows) / n
    print()
    print(f"  平均耗时       v3 {tt3:.2f}s     v4 {tt4:.2f}s")
    print(f"  v4 平均调用数  {cc4:.1f} 次/条")
    print(f"  总耗时         {elapsed:.1f}s")

    # ---------------- 逐条看差异 ----------------
    print()
    print("=" * 78)
    print("所有用例的细节（v4 是短板就看这里）")
    print("=" * 78)
    for r in rows:
        c = r["case"]
        print(f"\n【{c['id']}】{c['category']}  {c['q']}")
        print(f"  考的是：{c.get('tests', '')}")
        print(f"  v3 正则{'✅' if r['g3']['ok'] else '❌'} 裁判{r['j3']['verdict']}{r['j3'].get('scores')}")
        if not r["g3"]["ok"]:
            print(f"     缺={r['g3']['miss']} 多余={r['g3']['bad']}")
        print(f"     → {r['a3'][:140].replace(chr(10), ' ')}")
        print(f"  v4 正则{'✅' if r['g4']['ok'] else '❌'} 裁判{r['j4']['verdict']}{r['j4'].get('scores')}")
        if not r["g4"]["ok"]:
            print(f"     缺={r['g4']['miss']} 多余={r['g4']['bad']}")
        if r["fake4"]:
            print(f"     ⚠ 伪造引用 {len(r['fake4'])} 条（已触发重写）")
        if r["j4"].get("problems"):
            print(f"     裁判意见：{r['j4']['problems']}")
        print(f"     → {r['a4'][:140].replace(chr(10), ' ')}")

    with open(EVAL_DIR / "ab_v4_report.json", "w", encoding="utf-8") as f:
        json.dump({
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cases": n,
            "v3": {"regex_pass": p3, "seconds": round(tt3, 2)},
            "v4": {"regex_pass": p4, "seconds": round(tt4, 2), "calls": round(cc4, 1)},
            "detail": [{"id": r["case"]["id"], "q": r["case"]["q"],
                        "v3": r["a3"], "v4": r["a4"],
                        "v3_judge": r["j3"], "v4_judge": r["j4"]} for r in rows],
        }, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{EVAL_DIR / 'ab_v4_report.json'}")


if __name__ == "__main__":
    main()
