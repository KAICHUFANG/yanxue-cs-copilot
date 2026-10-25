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
TMP_DATA = EVAL_DIR / "tmp_data"          # 整个 data 目录的副本

# 只隔离 ACTIVITIES_FILE 是不够的：articles/processes/group_messages 都走 config.DATA_DIR。
# 粘贴推文的用例会调 save_article 覆盖 articles/current.md —— 曾经把真实推文冲掉过，
# 靠 git checkout 才恢复。所以这里把整个 data 目录复制出来，连 DATA_DIR 一起指过去。
shutil.rmtree(TMP_DATA, ignore_errors=True)
shutil.copytree(PROJ / "data", TMP_DATA, ignore=shutil.ignore_patterns("backups"))

shutil.copy2(FIXTURE, TMP)
os.environ["ACTIVITIES_FILE"] = str(TMP)
os.environ.setdefault("TEMPERATURE", "0")
sys.path.insert(0, str(PROJ))

import config       # noqa: E402

config.DATA_DIR = TMP_DATA
config.DATA_FILE = TMP

import agent        # noqa: E402
import knowledge    # noqa: E402
import store        # noqa: E402

store.DATA_DIR = TMP_DATA
store.BACKUP_DIR = TMP_DATA / "backups"


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

# 粘贴新一期推文 = 整条替换成新活动。单活动模型下这是**正常用法**，不是错误：
# 一期办完，客服直接把新推文粘进来，旧的那条被覆盖（覆盖前 store 会自动备份）。
NEW_TWEET = """【第十七期｜海岛珊瑚守护者】研学活动招募
时间：2026年11月7日（周六）14:00-17:30
名额：30人
集合：13:40 在海大北门集合
"""

# (推文, 必须被写入的字段, 推文里没写、因此必须被清空的字段)
#
# 第二列是这个 bug 的核心断言：
#   以前是「只覆盖推文里提到的字段」，推文没提的集合时间、室内外、分组、价格
#   全留着上一期的值，客服照着答家长就答错了期。所以「没提到的必须清空」。
REPLACE_CASES = [
    (NEW_TWEET,
     {"session_label": "第十七期", "capacity": 30, "meeting_point": "海大北门"},
     ["pricing", "indoor_outdoor", "grouping", "group_size"]),
]
# 不该被当成「换活动」的普通改字段
STAY_CASES = ["名额改成28人", "这支队伍17个人，名额改成28人"]


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


def stale_left(after: dict, must_clear) -> list:
    """推文没写的字段，还留着上一期的值 → 返回这些字段名（空列表 = 干净）。"""
    return [k for k in must_clear if after.get(k) not in (None, "", [], {})]


def store_replace_tests() -> int:
    """不走模型，直接测 store 的整条替换。

    为什么单独测这一层：
        上面那条端到端用例的结果有一半取决于模型抽了哪些字段，会抖。
        而「没抽到的字段必须清空」是纯代码纪律，必须 100% 稳定 —— 它塌了就是串期事故。
    """
    print("\n" + "-" * 78)
    print("store 整条替换（不走模型，纯代码纪律）")
    print("-" * 78)

    bad = 0
    archive = TMP_DATA / "archive" / "superseded.jsonl"
    reset()
    before = knowledge.get_activity("S16") or {}
    n_before = len(archive.read_text(encoding="utf-8").splitlines()) if archive.exists() else 0

    # 只给三个字段，看剩下的是不是真的清空
    res = store.update_activity(None, {
        "session_label": "第十七期",
        "capacity": 30,
        "meeting_point": "海大北门",
    }, replace=True)
    after = knowledge.get_activity("S16") or {}
    n_after = len(archive.read_text(encoding="utf-8").splitlines()) if archive.exists() else 0

    checks = [
        ("写入成功", res.get("ok") is True),
        ("id 跟着期次换成 S17", after.get("id") == "S17"),
        ("session_no 同步成 17", after.get("session_no") == 17),
        ("抽到的字段写进去了", after.get("capacity") == 30
         and after.get("meeting_point") == "海大北门"),
        # 关键：没给的一律清空，不许继承上一期
        # （status 要排除：它会被默认成「待确认」，本来就不是空值）
        ("没给的字段全部清空",
         stale_left(after, [k for k in store.ALLOWED_FIELDS
                            if k not in ("session_label", "capacity", "meeting_point",
                                         "status")]) == []),
        ("_ 开头的内部字段没跟过来",
         [k for k in after if k.startswith("_")
          and k not in ("_last_updated", "_replaced_from", "_replaced_at")] == []),
        ("白名单外旧字段（capacity_text / source / _missing）没跟过来",
         [k for k in ("capacity_text", "source", "_missing", "capacity_incentive",
                      "award_note", "curriculum_support") if k in after] == []),
        ("旧记录进了归档（多了一行）", n_after == n_before + 1),
        ("归档里存的是上一期", "第十六期" in (archive.read_text(encoding="utf-8").splitlines()[-1]
                                             if n_after else "")),
        ("status 默认成待确认（不瞎猜报名中）", after.get("status") == "待确认"),
    ]
    for name, ok in checks:
        if not ok:
            bad += 1
        print(f"  {'✅' if ok else '❌'} {name}")

    # 反向用例：合并模式**必须**保留没提到的字段，别把这条修复做成「一律清空」
    reset()
    store.update_activity(None, {"meeting_point": "海大北门"}, replace=False)
    after2 = knowledge.get_activity("S16") or {}
    ok = after2.get("capacity") == before.get("capacity") and after2.get("session_label") == "第十六期"
    if not ok:
        bad += 1
    print(f"  {'✅' if ok else '❌'} 合并模式仍然只覆盖提到的字段（capacity 保留 "
          f"{before.get('capacity')}→{after2.get('capacity')}）")
    return bad


def main() -> int:
    config.check()
    total = len(CASES) + len(REPLACE_CASES) + len(STAY_CASES)
    print("=" * 78)
    print(f"录入测试：{len(CASES)} 条常规 + {len(REPLACE_CASES)} 条整期替换 + {len(STAY_CASES)} 条普通改字段")
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
    print("整期替换（粘新一期推文 → 整条换成新活动，旧记录进归档）")
    print("-" * 78)

    for q, expect, must_clear in REPLACE_CASES:
        reset()
        before = snap()
        r = agent.answer_chat(q, activity_id="S16")
        after = snap()
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        stale = stale_left(after, must_clear)
        ok = check(expect, before, after) and not stale and r.get("replaced") is True
        if not ok:
            bad += 1
        print(f"\n{'✅' if ok else '❌'} {q.splitlines()[0]}")
        print(f"   写入 {len(r['wrote'])} 个字段   磁盘变化 {list(changed)}")
        print(f"   session_label：{before.get('session_label')} → {after.get('session_label')}")
        print(f"   整条替换判定：{r.get('replaced')}")
        print(f"   该清空的还留着：{stale or '无（已清空）'}")
        print(f"   回复：{r['answer'][:90].replace(chr(10), ' ')}")

    bad += store_replace_tests()

    print("\n" + "-" * 78)
    print("普通改字段（不应该被当成换活动）")
    print("-" * 78)

    for q in STAY_CASES:
        reset()
        r = agent.answer_chat(q, activity_id="S16")
        after = snap()
        ok = after.get("capacity") == 28 or str(after.get("capacity")) == "28"
        if not ok:
            bad += 1
        print(f"\n{'✅' if ok else '❌'} {q}")
        print(f"   写入 {r['wrote']}   capacity={after.get('capacity')}")

    print("\n" + "=" * 78)
    print(f"合计 {total} 条，失败 {bad} 条")
    print("=" * 78)
    for p in (TMP, TMP_DATA):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError:
            pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
