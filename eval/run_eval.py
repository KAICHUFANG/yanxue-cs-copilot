"""
评测脚本：跑一批用例，按四个维度打分

用法：
    .venv\\Scripts\\python.exe eval\\run_eval.py            # 全部跑
    .venv\\Scripts\\python.exe eval\\run_eval.py Q14 Q24    # 只跑指定用例

安全措施（重要）：
    用例里包含「写入知识库」的题（补录信息）。脚本启动时会先把 activities.json
    复制成 eval/tmp_activities.json，并通过 ACTIVITIES_FILE 环境变量指向副本。
    所以**无论用例怎么跑，都不会改到真实知识库**。
"""

# ---- 必须在 import config 之前设置环境变量，否则 config 已经读完路径了 ----
import json
import os
import shutil
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJ = EVAL_DIR.parent
TMP_KB = EVAL_DIR / "tmp_activities.json"

shutil.copy2(PROJ / "data" / "activities.json", TMP_KB)
os.environ["ACTIVITIES_FILE"] = str(TMP_KB)
# 评测要可复现：把温度压到 0，否则同一用例两次结果可能不同（踩过）
os.environ.setdefault("TEMPERATURE", "0")

sys.path.insert(0, str(PROJ))

import agent        # noqa: E402
import config       # noqa: E402


def load_cases() -> list:
    """默认读 cases.json；可用 --file cases_blind.json 指定别的用例集。"""
    name = "cases.json"
    if "--file" in sys.argv:
        i = sys.argv.index("--file")
        if i + 1 < len(sys.argv):
            name = sys.argv[i + 1]
    path = Path(name) if Path(name).is_absolute() else EVAL_DIR / name
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    print(f"用例集：{path.name}（{len(data['cases'])} 条）")
    return data["cases"]


def grade(case: dict, result: dict) -> dict:
    """四个维度打分。返回 {dim: bool} + 失败原因。"""
    answer = result.get("answer", "") or ""
    trace = result.get("trace", []) or []

    checks = {}

    # 1) 工具是否选对
    expect = case.get("expect_tool")
    if expect:
        checks["tool_ok"] = expect in trace
    else:
        checks["tool_ok"] = True   # 没指定就不判

    # 2) 关键事实是否齐全
    miss = [k for k in case.get("must_contain", []) if k not in answer]
    checks["facts_ok"] = not miss

    # 3) 多个正确说法至少命中一个
    any_list = case.get("must_contain_any", [])
    checks["any_ok"] = (not any_list) or any(k in answer for k in any_list)

    # 4) 有没有出现不该出现的内容
    bad = [k for k in case.get("must_not_contain", []) if k in answer]

    # 4b) 正则版编造检测：避免「否定句里引用了关键词」被误判
    #     例：答「没有『已经报了』的人数」是正确行为，不该算编造；
    #     真正编造是给出具体数字，所以用正则抓「已报名 15」这类。
    import re as _re
    for pat in case.get("must_not_match", []):
        m = _re.search(pat, answer)
        if m:
            bad.append(m.group(0))
    checks["clean_ok"] = not bad

    return {
        "checks": checks,
        "passed": all(checks.values()),
        "miss_facts": miss,
        "bad_hits": bad,
        "trace": trace,
        "answer": answer,
        "tokens": result.get("usage", {}).get("total_tokens", 0),
    }


def main() -> None:
    config.check()
    cases = load_cases()

    only = set(a for a in sys.argv[1:] if a.upper().startswith("Q"))
    if only:
        cases = [c for c in cases if c["id"] in only]

    print("=" * 78)
    print(f"评测开始：{len(cases)} 条用例")
    print(f"使用的知识库副本：{TMP_KB}")
    print("=" * 78)

    results = []
    t0 = time.time()

    for i, case in enumerate(cases, 1):
        conv = [{"role": "user", "content": case["q"]}]
        try:
            res = agent.ask(conv, verbose=False)
        except Exception as e:
            res = {"answer": f"【调用失败】{e}", "trace": [], "usage": {}}

        g = grade(case, res)
        g.update({"id": case["id"], "category": case["category"], "q": case["q"]})
        results.append(g)

        mark = "✅" if g["passed"] else "❌"
        print(f"[{i:2d}/{len(cases)}] {mark} {case['id']} {case['category']:<6} {case['q'][:26]}")

        if not g["passed"]:
            for dim, ok in g["checks"].items():
                if not ok:
                    reason = ""
                    if dim == "tool_ok":
                        reason = f"期望工具 {case.get('expect_tool')}，实际 {g['trace']}"
                    elif dim == "facts_ok":
                        reason = f"缺少关键事实 {g['miss_facts']}"
                    elif dim == "any_ok":
                        reason = f"未命中任一正确说法 {case.get('must_contain_any')}"
                    elif dim == "clean_ok":
                        reason = f"出现不该有的内容 {g['bad_hits']}"
                    print(f"        └─ {dim}: {reason}")

    elapsed = time.time() - t0

    # ---- 汇总 ----
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    tokens = sum(r["tokens"] for r in results)

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"总通过率：{passed}/{total} = {passed / total * 100:.1f}%")
    print(f"总耗时：{elapsed:.1f}s    总 tokens：{tokens}")

    dims = ["tool_ok", "facts_ok", "any_ok", "clean_ok"]
    labels = {"tool_ok": "工具选对", "facts_ok": "关键事实齐全",
              "any_ok": "命中正确说法", "clean_ok": "无编造/错误口径"}
    print()
    for d in dims:
        ok = sum(1 for r in results if r["checks"][d])
        print(f"  {labels[d]:<12} {ok}/{total} = {ok / total * 100:.1f}%")

    print()
    print("分类通过率：")
    cats = {}
    for r in results:
        c = cats.setdefault(r["category"], [0, 0])
        c[1] += 1
        if r["passed"]:
            c[0] += 1
    for cat, (p, n) in sorted(cats.items(), key=lambda x: x[1][0] / x[1][1]):
        bar = "█" * p + "░" * (n - p)
        print(f"  {cat:<8} {p}/{n}  {bar}")

    # ---- 落盘 ----
    report = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total, "passed": passed,
        "pass_rate": round(passed / total, 4),
        "elapsed_seconds": round(elapsed, 1),
        "total_tokens": tokens,
        "dimensions": {d: sum(1 for r in results if r["checks"][d]) for d in dims},
        "by_category": {c: {"passed": p, "total": n} for c, (p, n) in cats.items()},
        "failed": [
            {"id": r["id"], "q": r["q"], "checks": r["checks"],
             "miss": r["miss_facts"], "bad": r["bad_hits"], "trace": r["trace"],
             "answer": r["answer"]}
            for r in results if not r["passed"]
        ],
    }
    out = EVAL_DIR / "report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print(f"报告已写入：{out}")
    print(f"（知识库副本保留在 {TMP_KB.name}，可删除）")


if __name__ == "__main__":
    main()
