"""
录入回归测试：用不同说法改活动信息，看模型判断、抽到的字段、磁盘真实结果。

为什么单独一个脚本：
    问答类评测（run_eval / ab_real）测的是「答得对不对」，
    这里测的是「写进去对不对」——两件事，失败的后果也不一样。
    问答答错客服能看出来；写库写错是**事故**，还会覆盖真实数据。

安全：
    所有用例都跑在 eval/tmp_write.json 这个副本上，每条用例前从
    eval/fixtures/activities_fixture.json 复位。**永远不碰 data/activities.json。**

用法：
    .venv\\Scripts\\python.exe eval\\write_tests.py
    退出码 1 表示有用例失败。
"""

import json
import os
import shutil
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJ = EVAL_DIR.parent
FIXTURE = EVAL_DIR / "fixtures" / "activities_fixture.json"
TMP = EVAL_DIR / "tmp_write.json"

shutil.copy2(FIXTURE, TMP)
os.environ["ACTIVITIES_FILE"] = str(TMP)
os.environ.setdefault("TEMPERATURE", "0")
sys.path.insert(0, str(PROJ))

import agent        # noqa: E402
import config       # noqa: E402
import knowledge    # noqa: E402


WATCH = ("session_label", "title", "date", "time_range", "location",
         "meeting_point", "meeting_time", "indoor_outdoor", "grouping",
         "group_size", "capacity", "status", "pricing", "segments")


def reset() -> None:
    shutil.copy2(FIXTURE, TMP)


def snap() -> dict:
    a = knowledge.get_activity("S16") or {}
    return {k: a.get(k) for k in WATCH}


# (输入, 期望的磁盘值；None 表示「一条都不许改」)
CASES = [
    ("室外", {"indoor_outdoor": "室外"}),
    ("这期是室外活动", {"indoor_outdoor": "室外"}),
    ("这次活动不是室内的", {"indoor_outdoor": "室外"}),
    ("刚说错了，其实是室内的", {"indoor_outdoor": "室内"}),
    ("这期是室外，每组4到5个孩子，不分组",
     {"indoor_outdoor": "室外", "grouping": "不分组", "group_size": "4-5人"}),
    ("集合地点改到海南大学北门", {"meeting_point": "海南大学北门"}),
    ("集合时间14:40", {"meeting_time": "14:40"}),
    ("名额改成30人", {"capacity": 30}),
    ("活动日期是2026年11月7日", {"date": "2026-11-07"}),
    ("这期已经结束了", {"status": "已结束"}),
    # 以下这些**不该写**
    ("室内有空调吗", None),
    ("如果下雨就转室内", None),
    ("集合地点", None),
    ("负责人是李老师", None),
    ("几点集合", None),
]

# 跨期护栏：粘贴别期内容，必须被拦下
CROSS = "【第十七期｜海岛珊瑚守护者】研学活动招募\n时间：2026年11月7日（周六）14:00-17:30\n名额：30人\n集合：13:40 在海大北门集合\n"
GUARD_BLOCK = [CROSS]
GUARD_PASS = ["【第十六期】名额改成28人", "名额改成28人"]


def check(expect: dict, before: dict, after: dict) -> bool:
    if expect is None:
        return before == after
    for k, want in expect.items():
        got = after.get(k)
        if k == "capacity":
            if str(got) != str(want):
                return False
        elif isinstance(want, dict):
            if not isinstance(got, dict):
                return False
        else:
            if (got or "") != want:
                return False
    return True


def main() -> int:
    config.check()
    print("=" * 78)
    print(f"录入测试：{len(CASES)} 条常规 + {len(GUARD_BLOCK)} 条跨期拦截 + {len(GUARD_PASS)} 条放行")
    print(f"题库副本：{TMP.name}（不动真实数据）")
    print("=" * 78)

    bad = 0
    for q, expect in CASES:
        reset()
        before = snap()
        r = agent.answer_chat(q, activity_id="S16")
        after = snap()
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        ok = check(expect, before, after)
        if not ok:
            bad += 1
        label = "该写" if expect else "不该写"
        print(f"\n{'✅' if ok else '❌'} [{label}] {q[:40].replace(chr(10), ' / ')}")
        print(f"   写入 {r['wrote']}   磁盘 {changed}")
        if r["write_errors"]:
            print(f"   ⚠ {r['write_errors']}")
        print(f"   回复（{len(r['answer'])}字）：{r['answer'][:80].replace(chr(10), ' ')}")

    print("\n" + "-" * 78)
    print("跨期护栏")
    print("-" * 78)

    for q in GUARD_BLOCK:
        reset()
        before = snap()
        r = agent.answer_chat(q, activity_id="S16")
        ok = before == snap()
        if not ok:
            bad += 1
        print(f"\n{'✅' if ok else '❌'} [应拦下] {q[:34].replace(chr(10), ' / ')}")
        print(f"   磁盘变了吗：{'没变 ✅' if ok else '变了 ❌'}")
        print(f"   回复：{r['answer'][:90].replace(chr(10), ' ')}")

    for q in GUARD_PASS:
        reset()
        r = agent.answer_chat(q, activity_id="S16")
        ok = bool(r["wrote"]) and not r["write_errors"]
        if not ok:
            bad += 1
        print(f"\n{'✅' if ok else '❌'} [应放行] {q}")
        print(f"   写入 {r['wrote']}")

    total = len(CASES) + len(GUARD_BLOCK) + len(GUARD_PASS)
    print("\n" + "=" * 78)
    print(f"合计 {total} 条，失败 {bad} 条")
    print("=" * 78)
    try:
        TMP.unlink()
    except OSError:
        pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
