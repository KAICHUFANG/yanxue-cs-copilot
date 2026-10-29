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
TMP_DATA = EVAL_DIR / "tmp_eval_data"

# 评测固定用 eval/fixtures 里的题库快照，不直接吃真实业务数据——
# 否则客服今天填一个集合地点，昨天的评测结论就作废（踩过）。
_FIXTURE = EVAL_DIR / "fixtures" / "activities_fixture.json"
_SRC = _FIXTURE if _FIXTURE.exists() else PROJ / "data" / "activities.json"
shutil.copy2(_SRC, TMP_KB)
os.environ["ACTIVITIES_FILE"] = str(TMP_KB)
# 评测要可复现：把温度压到 0，否则同一用例两次结果可能不同（踩过）
os.environ.setdefault("TEMPERATURE", "0")

sys.path.insert(0, str(PROJ))

import agent        # noqa: E402
import config       # noqa: E402
import store        # noqa: E402

# 只隔离 ACTIVITIES_FILE 远远不够。真实踩过两次，第二次是把业务的
# archive/superseded.jsonl 追加了一条「整条替换为 第九十九期」的测试垃圾：
#   ① 写库用例的备份 → data/backups
#   ② 整条替换的归档 → data/archive/superseded.jsonl
# 所以直接把整个 data 目录复制一份，所有路径都指过去（和 write_tests 一致）。
if "--keep-data" not in sys.argv:
    # 数据目录复制一份（语料/推文/流程/归档都在里面），但**活动记录用冻结题库**：
    # 评测必须可复现 —— 客服今天填个集合点，昨天的评测结论就作废（踩过）。
    shutil.rmtree(TMP_DATA, ignore_errors=True)
    shutil.copytree(PROJ / "data", TMP_DATA, ignore=shutil.ignore_patterns("backups"))
    shutil.copy2(_SRC, TMP_KB)                      # _SRC = fixture（或回退到真实数据）
    shutil.copy2(TMP_KB, TMP_DATA / "activities.json")
    # 推文原文也必须冻结！只冻结 activities.json 是不够的：题库是第十六期，
    # 而 articles/current.md 会随真实业务换期。曾经因此出现「第十六期字段 +
    # 第十八期推文」的混合上下文，模型读到最后一行「审核丨付广」，
    # 就编出「付广老师带队」——被访人被当成了带队老师（Q27 就是这么红的）。
    _ART = EVAL_DIR / "fixtures" / "article.md"
    if _ART.exists():
        shutil.copy2(_ART, TMP_DATA / "articles" / "current.md")
    config.DATA_DIR = TMP_DATA
    config.DATA_FILE = TMP_KB
    store.DATA_DIR = TMP_DATA
    store.BACKUP_DIR = TMP_DATA / "backups"


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

    # 1) 走的路径对不对
    #    新架构没有「按意图挑工具」这回事了，只剩两条确定性路径：
    #      · 要文案 → render_group_messages（必须原文输出，所以由代码填模板）
    #      · 补充信息 → update_activity（必须过白名单和备份）
    #    其余一律走上下文直答，trace 里是 <context>。
    expect = case.get("expect_tool")
    if expect:
        if expect in ("render_group_messages", "update_activity"):
            checks["tool_ok"] = expect in trace
        else:
            checks["tool_ok"] = "<context>" in trace
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

    # 5) 口吻：产出的是「客服可以发给家长的话」，不许夹带系统腔
    #    这条是硬约束。真实翻车过：答「报名二维码在客服这边…我让客服把二维码发您」——
    #    读这句话的人就是客服，等于让客服去找客服。家长也不会看见系统、资料库、界面这些词。
    #    「自我认知」那类用例是客服在问工具本身，允许直说，跳过。
    # 只禁「我们自己的系统」的词。
    # 「系统」两个字本身不禁：家长要进海南大学的预约系统，说「系统里选不到日期」是正常的。
    _META = ["资料库", "知识库", "数据库", "界面", "面板", "右侧",
             "我让客服", "我这边看不到", "客服那边"]
    # 客服在问「工具本身」的（还缺什么、你看得到推文吗），本来就是对她说话，
    # 允许出现系统视角；家长问题则一律不许。
    is_meta = case.get("category") == "自我认知" or case.get("voice") == "meta"
    if is_meta:
        checks["voice_ok"] = True
    else:
        leaked = [w for w in _META if w in answer]
        checks["voice_ok"] = not leaked

    # 7) 客气度：家长问题的回答里必须用「您」称呼，且不出现单独的「你」。
    #    这是客服的基本礼貌，也是「这是客服要发出去的话」的直接体现。
    #    只查问答路径（trace 里有 <context>），文案和 meta 问答不查。
    is_qa = "<context>" in trace
    if is_meta or not is_qa or "各位家长" in answer:
        # 群公告体（各位家长…）是整群通知的语气，不适用「您」，不算不客气
        checks["polite_ok"] = True
    else:
        checks["polite_ok"] = ("您" in answer) and not _re.search(r"你(?!们)", answer)

    # 6) 长度（只在用例声明了 max_len 时生效）
    #    文案类需求里长度是硬要求：朋友圈超过 6 行会被折叠成「全文」，家长根本不会点开。
    #    光断言「有没有说到要点」抓不到「写太长」这种错，所以单列一维。
    cap = case.get("max_len")
    checks["len_ok"] = (not cap) or len(answer) <= cap

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
        try:
            res = agent.answer_chat(case["q"])
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
                    elif dim == "voice_ok":
                        reason = f"夹带系统腔 {[w for w in ['资料库','知识库','界面','面板','右侧','我让客服','我这边看不到','客服那边'] if w in g['answer']]}"
                    elif dim == "polite_ok":
                        reason = "家长问题里没有出现「您」，或用了「你」"
                    elif dim == "len_ok":
                        reason = f"太长：{len(g['answer'])} 字，上限 {case.get('max_len')}"
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

    dims = ["tool_ok", "facts_ok", "any_ok", "clean_ok", "voice_ok", "polite_ok", "len_ok"]
    labels = {"tool_ok": "路径选对", "facts_ok": "关键事实齐全",
              "any_ok": "命中正确说法", "clean_ok": "无编造/错误口径",
              "voice_ok": "可直接发家长", "polite_ok": "称呼客气", "len_ok": "长度合规"}
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
