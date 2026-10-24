"""
路由自检：确保「加进知识库的字段」都能被家长的问法命中

为什么需要这个脚本（踩过的坑）：
    2026-10-22 我把 grouping / group_size 加进了必填字段表，
    但**忘了把「分组」加进路由关键词**。
    结果家长问「事先分好组吗」时，路由落到 auto，模型去查了活动详情，
    答案里没有分组说明——知识明明在库里，却取不出来。

    这类问题靠记性防不住，所以做成脚本：
        每个必填字段 → 列出家长可能的问法 → 检查路由是否命中

用法：
    .venv\\Scripts\\python.exe eval\\check_routing.py
"""

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJ = EVAL_DIR.parent
sys.path.insert(0, str(PROJ))

import router           # noqa: E402
import store            # noqa: E402

# 每个必填字段：家长可能怎么问
FIELD_PROBES = {
    "date": ["第十六期是哪天", "第十六期什么时候"],
    "weekday": ["第十六期是周几"],
    "time_range": ["第十六期几点开始"],
    "location": ["第十六期在哪上课"],
    "meeting_point": ["集合地点在哪里"],
    "meeting_time": ["几点集合"],
    "target_audience": ["几年级能参加", "适合多大的孩子"],
    "capacity": ["名额还有多少", "招多少人"],
    "pricing": ["多少钱", "收费怎么算"],
    "indoor_outdoor": ["是室内还是室外"],
    "grouping": ["事先分好组吗", "怎么分组"],
    "group_size": ["一组几个人", "每组几个孩子"],
}

# 非字段类知识，也要能被问到
EXTRA_PROBES = {
    "退费": ["下雨会退费吗", "能退款吗"],
    "迟到": ["能晚点到吗", "迟到了还能进吗"],
    "陪同": ["家长要陪同吗", "大人可以一起吗"],
    "自带物品": ["要带什么东西", "需要自己带水吗"],
    "报名方式": ["怎么报名", "是在群里接龙吗"],
    "车辆进校": ["车能开进学校吗", "车停哪里"],
    "门禁": ["从哪个门进", "西门能不能走"],
    "预约状态": ["预约一直显示待审核"],
    "证件": ["需要带身份证吗"],
    "分组先行": ["活动开始前分好组吗"],
}


def main() -> None:
    required = [f for f, _ in store.REQUIRED_FOR_COPY]
    print("=" * 76)
    print("路由自检")
    print("=" * 76)

    problems = []
    no_probe = []

    for field, label in store.REQUIRED_FOR_COPY:
        probes = FIELD_PROBES.get(field)
        if not probes:
            no_probe.append(f"{field}（{label}）")
            continue
        for q in probes:
            intent = router.detect_intent(q)
            mark = "❌" if intent == "auto" else "✅"
            if intent == "auto":
                problems.append((field, q, intent))
            print(f"  {mark} {label:<10} {q:<22} → {intent}")

    print()
    print("-" * 76)
    print("非字段类知识")
    print("-" * 76)
    for name, probes in EXTRA_PROBES.items():
        for q in probes:
            intent = router.detect_intent(q)
            mark = "❌" if intent == "auto" else "✅"
            if intent == "auto":
                problems.append((name, q, intent))
            print(f"  {mark} {name:<10} {q:<22} → {intent}")

    print()
    print("=" * 76)
    if no_probe:
        print("⚠ 这些必填字段还没有对应的家长问法（补 FIELD_PROBES）：")
        for x in no_probe:
            print("   ·", x)
    if problems:
        print(f"❌ {len(problems)} 条问法路由到 auto（知识可能在库里，但取不出来）：")
        for f, q, i in problems:
            print(f"   · [{f}] 「{q}」→ {i}")
        sys.exit(1)
    else:
        print("✅ 全部问法都能被路由识别，没有落在 auto 上的")
    print("=" * 76)


if __name__ == "__main__":
    main()
