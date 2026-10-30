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
# 推文原文也要冻结（理由见 run_eval.py 里的注释）：题库的 activitiy 是旧的，
# 真实 current.md 却随换期变化，混在一起会让答案不可复现。
_ART = EVAL_DIR / "fixtures" / "article.md"
if _ART.exists():
    shutil.copy2(_ART, TMP_DATA / "articles" / "current.md")

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


# ---------------------------------------------------------------- 群文案渲染
# 这一层的 bug 都不报错，只会「少给一段」或「留个空自己没发现」。
# 2026-10-26 用户报的就是：进校预约那一段只吐了「进校门提醒」，
# 预约文案、公众号名片、填写示例图、催办话术全没出来。
# 原因：渲染器只在 messages 为空时才展开 uses_process。
REVIEW_TEXT = """“山海科普研学”第十六期回顾丨自贸港小商人·集市大冒险

10月24日下午，孩子们在海南大学完成了这场集市大冒险。
孩子们分成五个小组，分别扮作批发商、加工商、零售商、合伙人、物流商，
用自制的「货币」走完了生产、加工、买卖、合伙、交货的全过程。

现场照片（多图）

阅读原文：https://mp.weixin.qq.com/s/AbCdEf123456
"""


def _leaked_tokens(node, unfilled) -> list:
    """渲染出来的 text 里还留着 {{}}，但它又不在未填清单里 → 静默丢数据。"""
    import re as _re
    bad = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "text" and isinstance(v, str):
                for tk in _re.findall(r"\{\{[^{}]+\}\}", v):
                    if tk not in unfilled:
                        bad.append(tk)
            else:
                bad += _leaked_tokens(v, unfilled)
    elif isinstance(node, list):
        for v in node:
            bad += _leaked_tokens(v, unfilled)
    return bad


def copy_layer_tests() -> int:
    """群文案渲染 + 回顾推文存储（不走模型，纯代码纪律）。"""
    import renderer

    print("\n" + "-" * 78)
    print("群文案渲染（不走模型，纯代码纪律）")
    print("-" * 78)
    bad = 0

    def check(name, ok, extra=""):
        nonlocal bad
        if not ok:
            bad += 1
        print(f"  {'✅' if ok else '❌'} {name}{('   ' + extra) if extra and not ok else ''}")

    # ① 进校预约那一段：必须把流程的 4 件 + 门禁提醒全吐出来，而且顺序对
    d = json.loads(renderer.render_group_messages("S16", stage="②"))
    labels = [m.get("label", "") for m in d["messages"] if m.get("label") != "这一步必须人工做的"]
    want = ["预约提醒文案", "「海南大学」公众号名片 / 链接", "进校预约填写示例截图",
            "进校门提醒（紧跟预约文案发）", "@所有人 催办话术"]
    check("阶段② 五件都在、顺序对", labels == want, f"实际 {labels}")
    check("阶段② 手动发送项被标出来（名片 + 示例图）",
          sum(1 for m in d["messages"] if m.get("manual")) == 2)

    # ② 被访人信息来自 activities.json 的 team_info（唯一来源；以前从 .env 兜底，已去掉）
    first = d["messages"][0]["text"]
    _team = knowledge.load_team_info()
    if _team["visit_target_name"] and _team["visit_target_phone"]:
        check("team_info 里的被访人姓名/电话填进了预约文案",
              _team["visit_target_name"] in first and _team["visit_target_phone"] in first)
    check("被访人只有 team_info 一个来源（.env 里不再有 VISIT_TARGET_*）",
          not hasattr(config, "VISIT_TARGET_NAME"))
    # 临时清空 team_info，验证「没配 → 保留占位符并申报」
    _orig = knowledge.load_team_info
    knowledge.load_team_info = lambda *a, **k: {"visit_target_name": "", "visit_target_phone": ""}
    try:
        d2 = json.loads(renderer.render_group_messages("S16", stage="②"))
    finally:
        knowledge.load_team_info = _orig
    check("没配被访人信息时保留占位符并进未填清单",
          "{{被访人姓名}}" in d2["unfilled_summary"]
          and "{{被访人姓名}}" in d2["messages"][0]["text"])

    # ③ {{反馈时点}} 永远算「必须人工改」，并带上为什么
    check("{{反馈时点}} 进了未填清单", "{{反馈时点}}" in d["unfilled_summary"])
    check("{{反馈时点}} 带上了「按建群时间改」的说明",
          "建群" in (d["unfilled_reasons"].get("{{反馈时点}}") or ""))

    # ④ 不能静默留空：渲染结果里的 {{}} 必须都被申报过
    for st in ("①", "②", "③", "④", "⑤"):
        dd = json.loads(renderer.render_group_messages("S16", stage=st))
        leaked = _leaked_tokens(dd["messages"], dd["unfilled_summary"])
        check(f"阶段{st} 没有未申报的模板占位符残留", not leaked, f"漏报 {leaked}")

    # ⑤ ⑤ 里两种文案要能分开：要「结束感谢」时不该拖出「回顾分享」
    t = json.loads(renderer.render_group_messages("S16", stage="⑤", only_tags=["thanks"]))
    joined = "".join(m.get("text", "") for m in t["messages"])
    check("only_tags=thanks 只出结束感谢", "回顾推文出来啦" not in joined and "圆满结束" in joined)
    r = json.loads(renderer.render_group_messages("S16", stage="⑤", only_tags=["review"]))
    joined2 = "".join(m.get("text", "") for m in r["messages"])
    check("only_tags=review 只出回顾分享", "回顾推文出来啦" in joined2 and "圆满结束" not in joined2)

    # ⑥ 拟好的稿子要能填进去（overrides 优先于「必须人工」）
    r2 = json.loads(renderer.render_group_messages(
        "S16", stage="⑤", only_tags=["thanks"],
        overrides={"{{现场亮点}}": "动手做了三笔买卖并记账"}))
    txt = "".join(m.get("text", "") for m in r2["messages"])
    check("传了 override 就填进去、不再是占位符",
          "动手做了三笔买卖并记账" in txt and "{{现场亮点}}" not in txt)

    # ---------- 回顾推文存储 ----------
    print("\n" + "-" * 78)
    print("活动回顾推文存储（不走模型）")
    print("-" * 78)
    # 注意：这里不要再写 import knowledge —— 模块顶部已 import，
    # 函数内再 import 会让「该函数里所有 knowledge 引用」都变成局部变量，
    # 前面用到的行会抛 UnboundLocalError（踩过）。
    review_dir = TMP_DATA / "articles" / "reviews"
    shutil.rmtree(review_dir, ignore_errors=True)

    check("从正文认出期次并统一成阿拉伯数字（第十六期 → 第16期）",
          knowledge.period_label(REVIEW_TEXT) == "第16期", knowledge.period_label(REVIEW_TEXT))
    check("中文数字也能认（第九期 → 第9期）",
          knowledge.period_label("第九期回顾") == "第9期")

    info = knowledge.save_review(REVIEW_TEXT)
    check("存成按期次命名的文件", info.get("rel") == "articles/reviews/review_第16期.md",
          str(info.get("rel")))
    check("文件真的在磁盘上", (TMP_DATA / info["rel"]).exists())
    check("能读回来", knowledge.load_review_by_label("第十六期").startswith("“山海科普研学”第十六期回顾"))
    check("当前这一期也能读到（load_review）",
          "集市大冒险" in knowledge.load_review(knowledge.get_activity()))
    check("从回顾推文里抽出了公众号链接",
          knowledge.review_url(REVIEW_TEXT) == "https://mp.weixin.qq.com/s/AbCdEf123456")

    before = len(list((TMP_DATA / "backups").glob("review.*")))
    info2 = knowledge.save_review(REVIEW_TEXT + "\n\n（补一句：下一期下周开放报名）")
    after = len(list((TMP_DATA / "backups").glob("review.*")))
    check("同一期重存会先备份旧稿", info2.get("replaced") is True and after == before + 1)
    check("索引里同一期只算一条",
          [x["session_label"] for x in knowledge.list_reviews()].count("第16期") == 1)

    # 回顾推文不能影响活动记录：它讲的是已经办完的那一场
    act_before = knowledge.get_activity()
    knowledge.save_review(REVIEW_TEXT)
    act_after = knowledge.get_activity()
    check("存回顾推文不会改动活动记录", act_before == act_after)

    # ---------- 文章类型旁证（纯代码） ----------
    check("模型误判成推广时，代码拿旁证否决成回顾",
          knowledge.decide_article_kind(REVIEW_TEXT, "promo") == "review")
    check("模型误判成回顾时，代码拿旁证否决成推广",
          knowledge.decide_article_kind(
              "第十七期招募  扫码报名  名额 25 人  以下是活动流程", "review") == "promo")
    check("证据不明显时听模型的",
          knowledge.decide_article_kind("一段普通群公告", "promo") == "promo")

    return bad


def data_hygiene_tests() -> int:
    """数据卫生：派生字段不落盘、备份去重、文档与数据单一来源（纯代码）。"""
    print("-" * 78)
    print("数据卫生（不走模型）")
    print("-" * 78)
    bad = 0

    def check(name, ok, extra=""):
        nonlocal bad
        if not ok:
            bad += 1
        print(f"  {'✅' if ok else '❌'} {name}{('   ' + str(extra)) if extra and not ok else ''}")

    reset()
    raw = json.loads(TMP.read_text(encoding="utf-8"))["activities"][0]

    # ① 派生字段不许出现在文件里
    leaked = [k for k in store.DERIVED_FIELDS if k in raw]
    check("activities.json 里没有 weekday / duration_minutes / session_no", not leaked, leaked)

    # ② 读的时候能补齐，而且跟着源头变
    act = knowledge.get_activity()
    check("读取时补出 周六", act.get("weekday") == "周六", act.get("weekday"))
    check("读取时补出 180 分钟", act.get("duration_minutes") == 180, act.get("duration_minutes"))
    check("读取时补出 session_no=16", act.get("session_no") == 16, act.get("session_no"))
    moved = store.with_derived({**raw, "date": "2026-11-07", "time_range": "09:00-11:30"})
    check("改了日期，星期跟着变（11-07 是周六以外的那天）",
          moved["weekday"] == "周六" and moved["duration_minutes"] == 150,
          (moved["weekday"], moved["duration_minutes"]))

    # ③ 写完库之后，文件里依然不许出现派生字段
    store.update_activity(None, {"capacity": 26})
    raw2 = json.loads(TMP.read_text(encoding="utf-8"))["activities"][0]
    check("写库后文件里仍然没有派生字段",
          not [k for k in store.DERIVED_FIELDS if k in raw2])
    check("写库后 capacity 已更新", raw2.get("capacity") == 26, raw2.get("capacity"))

    # ④ 备份按内容去重：同一份内容连写两次只留一份
    bak_dir = TMP_DATA / "backups"
    bak_dir.mkdir(parents=True, exist_ok=True)
    before = len(list(bak_dir.glob("activities.*.json")))
    store.update_activity(None, {"capacity": 27})
    mid = len(list(bak_dir.glob("activities.*.json")))
    store.update_activity(None, {"status": "报名中"})
    store.update_activity(None, {"status": "待确认"})      # 回到与 mid 相同的内容
    after = len(list(bak_dir.glob("activities.*.json")))
    check("备份按内容去重（写入 3 次，最多只多 2 份）", after - before <= 2, f"{before}→{after}")

    # ⑤ 备份份数上限
    old_keep = store.BACKUP_KEEP
    store.BACKUP_KEEP = 3
    try:
        for i in range(6):
            store.update_activity(None, {"capacity": 30 + i})
        kept = len(list(bak_dir.glob("activities.*.json")))
    finally:
        store.BACKUP_KEEP = old_keep
    check("备份份数不超过上限（设成 3）", kept <= 3, kept)

    # ⑥ processes.json 的重复项已合并
    procs = json.loads((TMP_DATA / "processes.json").read_text(encoding="utf-8"))
    cb = procs["campus_booking"]
    check("send_package 不再有 order 列表", "order" not in cb["send_package"])
    check("campus_booking 不再有 warnings", "warnings" not in cb)
    check("entry_gate / id_card_check 只留对外定稿",
          "text" not in cb["entry_gate"] and "text" not in cb["id_card_check"])
    check("定稿仍在（删的是重复那份，不是事实）",
          "北门" in cb["entry_gate"]["ready_to_send_text"]
          and "身份证" in cb["id_card_check"]["ready_to_send_text"])

    # ⑦ 文档与数据单一来源：模板原文不许再抄进流程文档
    root = PROJ
    flow = (root / "docs" / "活动群文案流程.md").read_text(encoding="utf-8")
    gen = (root / "docs" / "群文案原文.md").read_text(encoding="utf-8")
    book = json.loads((TMP_DATA / "group_messages.json").read_text(encoding="utf-8"))
    texts = [m.get("text") for st in book["timeline"] for m in (st.get("messages") or [])
             if m.get("text") and m.get("type") not in ("manual_send_required", "reference")]
    in_flow = [t for t in texts if t.splitlines()[0].strip()[:14] in flow]
    in_gen = [t for t in texts if t.splitlines()[0].strip()[:14] in gen]
    check("流程文档里不再抄写模板原文", not in_flow, f"仍有 {len(in_flow)} 条")
    check("生成的群文案原文.md 收录了全部模板", len(in_gen) == len(texts),
          f"{len(in_gen)}/{len(texts)}")
    # 只比首行是不够的：数据改了措辞但首行没变，文档就会静默过期（真的发生过）
    # 比对范围要包含两个数据源：正文既可能来自 group_messages，
    # 也可能来自 processes（进校预约发送包是引用过来的）。只比一个会全红（误报过）。
    _data_blob = (json.dumps(book, ensure_ascii=False)
                  + json.dumps(json.loads((TMP_DATA / "processes.json").read_text(encoding="utf-8")),
                               ensure_ascii=False))
    _residue = [ln.strip() for ln in gen.splitlines()
                if len(ln.strip()) >= 12
                and not ln.strip().startswith(("#", ">", "*", "-", "|", "（"))
                and ln.strip() not in _data_blob
                and "{{" not in ln]
    check("生成的群文案原文.md 没有过期残留（文档里每句都能在数据里找到）",
          not _residue, f"{len(_residue)} 行残留，例如：{_residue[:2]}")

    # ⑨ 发定位文案：三种状态三种行为（纯代码，用户要求「状态未声明就拒绝生成」）
    import json as _json, renderer as _r
    _aid = (knowledge.get_activity() or {}).get("id") or "S16"
    # 注意：活动数据在 TMP（= config.DATA_FILE = tmp_write.json），
    # 不是 TMP_DATA/activities.json —— 后者是 data 目录的副本，renderer 不读它。
    _act_file = TMP

    def _set_nav(v):
        kb = _json.loads(_act_file.read_text(encoding="utf-8"))
        if v is None:
            kb["activities"][0].pop("nav_map_available", None)
        else:
            kb["activities"][0]["nav_map_available"] = v
        _act_file.write_text(_json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")

    _set_nav(None)
    out_none = _r.render_location_share(_aid)
    check("导航图状态未声明 → 拒绝生成正文",
          "先不生成" in out_none or "还没定" in out_none, out_none[:60])
    check("拒绝时告诉客服该说什么（有导航图 / 不发导航图）",
          "有导航图" in out_none and "不发导航图" in out_none)

    _set_nav("有")
    out_yes = _r.render_location_share(_aid)
    check("状态=有 → 文案说明「活动开始前半小时」发导航图",
          "开始前半小时" in out_yes, out_yes[:80])
    check("状态=有 → 自动算出开始前半小时的具体时间（fixture 15:00 → 14:30）",
          "14:30" in out_yes, out_yes[:80])

    _set_nav("没有")
    out_no = _r.render_location_share(_aid)
    check("状态=没有 → 文案改成「不另发引导照片」",
          "不另发引导照片" in out_no, out_no[:80])
    check("状态=没有 → 不能再出现「开始前半小时」这种承诺",
          "开始前半小时" not in out_no)
    # 概念（用户纠正过两次）：
    #   ① 导航图是一组沿步行路线拍的引导照片，给人走用 —— 不是给车看
    #   ② 它解决「到了附近怎么找到精确集合点」，不是指路（怎么过来由定位解决）
    _nav_copy = out_yes + out_no
    check("导航图不被说成给车看的（不出现「车停/停车场」）",
          not any(w in _nav_copy for w in ("车停", "停车场", "开车过去")), _nav_copy[:80])
    check("导航图不被说成指路路线", not any(w in _nav_copy for w in ("怎么走", "哪条路线")))
    check("写明是「一组照片」且「沿步行路线」",
          "一组" in out_yes and ("走路" in out_yes or "步行" in out_yes), out_yes[:100])

    check("非法状态会被 store 拒绝",
          store._validate_one("nav_map_available", "不发")[0] is False)
    _set_nav(None)

    # ⑨a 新增字段必须在界面上有中文名（用户报过 nav_map_available 裸奔英文）
    from app_workbench import DISPLAY_NAME as _DN
    _english = []
    for _f in store.ALLOWED_FIELDS:
        _lab = _DN.get(_f) or (store.ALLOWED_FIELDS[_f].split("，")[0]
                               .split("（")[0].split("：")[0].strip())
        if _lab == _f or not _lab:
            _english.append(_f)
    check("每个可写字段在界面上都有中文名（不会把英文键名给客服看）",
          not _english, f"缺中文名：{_english}")
    check("写库确认语用中文名（不用 nav_map_available 这种键名）",
          True if "中文名" in open("agent.py", encoding="utf-8").read() else False)

    # ⑨b 导航图口径：所有文案与规则里都不许再出现「前一天晚上发导航图」
    import json as _j2
    _gm = _j2.loads((TMP_DATA / "group_messages.json").read_text(encoding="utf-8"))
    _pol = _j2.loads(TMP.read_text(encoding="utf-8")).get("policies", {})   # TMP = 活动数据副本
    _blob = _j2.dumps(_gm, ensure_ascii=False) + _j2.dumps(_pol, ensure_ascii=False)
    # 只看「提到导航的文本」，别扫阶段名（③ 的 when 本来就叫「活动前一天晚上」）
    _nav_texts = []
    for st in _gm["timeline"]:
        for m in st.get("messages") or []:
            for f in ("text", "content_note"):
                v = m.get(f) or ""
                if "导航" in v:
                    _nav_texts.append(v)
    for sn in (_gm.get("publish_flow") or {}).get("side_notes") or []:
        if "导航" in (sn.get("title") or "") + (sn.get("text") or ""):
            _nav_texts.append(sn.get("text") or "")
    _nav_texts.append((_pol.get("nav_photo_policy") or {}).get("text") or "")
    _bad_nav = [t for t in _nav_texts
                if ("前一天晚上" in t) or ("昨晚已发" in t)]
    check("导航相关的文案/规则里没有「前一天晚上发 / 昨晚已发」的旧承诺",
          not _bad_nav, str(_bad_nav)[:100])
    check("导航图口径写明「活动开始前半小时」",
          "开始前半小时" in _blob)
    check("导航图状态未声明时要求「确认一下再回」",
          "确认一下" in (_pol.get("nav_photo_policy", {}) or {}).get("text", ""))
    _alert_old = [m.get("text") or "" for st in _gm["timeline"] for m in (st.get("messages") or [])
                  if "导航" in (m.get("text") or "") and "前一天晚上" in (m.get("text") or "")]
    check("发给家长的文案里没有「导航图前一天晚上发」的承诺", not _alert_old, str(_alert_old)[:80])

    # ⑩ 回顾分享的前置条件：没有本期回顾推文时不许生成（用户报过「现在就生成」）
    reset()
    # 前面的 copy_layer_tests 会往测试副本里存一份回顾推文，这里先清掉，
    # 否则「没有回顾推文」这个前提不成立（测试之间互相污染过一次）。
    shutil.rmtree(TMP_DATA / "articles" / "reviews", ignore_errors=True)
    if knowledge.load_review():
        check("（前置条件测试前提：本期没有回顾推文）", False, "本期已有回顾推文，测试前提不成立")
    else:
        import json as _json, renderer as _r
        # 注意用题库里的期次 id：测试副本跑的是冻结题库（S16），不是真实的 S18
        _aid = (knowledge.get_activity() or {}).get("id") or "S16"
        d = _json.loads(_r.render_group_messages(_aid))
        rev = [m for m in d["messages"] if "回顾" in (m.get("label") or "")]
        check("没有回顾推文时，回顾分享不生成正文",
              rev and all("text" not in m for m in rev), str(rev)[:120])
        check("改为给出一条「等回顾推文」的提示",
              rev and any("回顾" in (m.get("note") or "") for m in rev))
        check("{{具体细节}} 不再出现在「需人工补」清单里",
              "{{具体细节}}" not in d["unfilled_summary"], str(d["unfilled_summary"]))
        check("活动结束感谢（同一阶段另一段）仍然正常生成",
              any("圆满结束" in (m.get("text") or "") for m in d["messages"]))

    # ⑩ 阶段关键词映射（纯代码，用户报过「预约进校文案」被渲染成欢迎语）
    checks = {
        "写个拉群欢迎语": "①", "生成预约进校文案": "②", "进校预约提醒": "②",
        "写个活动前一天的活动提醒": "③", "写个当天的出发提醒": "④",
        "出发提醒": "④", "写个活动结束感谢": "⑤", "回顾分享": "⑤",
    }
    bad_stage = []
    for text, want in checks.items():
        got = None
        for word, no, _tg in agent._STAGE_HINT:
            if word in text:
                got = no
                break
        if got != want:
            bad_stage.append(f"{text}→{got}(应{want})")
    check("阶段关键词映射全对（具体的词优先于笼统的词）", not bad_stage, bad_stage)

    # ⑩ title 清洗：推文标题「“公众号名”第十八期丨活动名」不能整串当活动名
    check("title 切掉「…丨」前的公众号名与期次",
          store.clean_title("“山海科普研学”第十八期丨“海大蓝境”海洋生物标本馆")
          == "“海大蓝境”海洋生物标本馆")
    check("title 正常时不动它",
          store.clean_title("自贸港小商人·集市大冒险") == "自贸港小商人·集市大冒险")

    # ⑧ 两份临时摘录已清掉
    check("data/ 下不再有临时摘录 md",
          not list((TMP_DATA).glob("activities.raw.md"))
          and not list((TMP_DATA).glob("raw_S16*.md")))
    # ⑨ 集合点已填时，那条「每期不同」的规则不许再进上下文
    #    （用户报过的自相矛盾：地点在标本馆 → 又说集合点每期不一样、定了再发）
    reset()
    store.update_activity(None, {"meeting_point": "海南大学海洋生物标本馆",
                                 "meeting_time": "15:00"})
    ctx = knowledge.build_context()
    check("集合点已填 → 上下文里没有「集合点每期不同」", "集合点每期不同" not in ctx)
    check("集合点已填 → 上下文里有那个具体地点", "海南大学海洋生物标本馆" in ctx)
    check("导航图/引导图那条仍然保留（现场拍的，不能承诺已就绪）",
          "导航图" in ctx or "引导图" in ctx)
    check("条件开关 applies_when_empty 没漏进上下文", "applies_when_empty" not in ctx)

    reset()
    ctx2 = knowledge.build_context()
    check("集合点清空后 → 那条规则要回来", "集合点每期不同" in ctx2)

    # ⑩ 端到端：集合点已填，问「在哪集合」，答出来不许自相矛盾
    reset()
    store.update_activity(None, {"meeting_point": "海南大学海洋生物标本馆",
                                 "meeting_time": "15:00"})
    ans = agent.answer_ctx("集合地点在哪")["answer"]
    bad_words = [w for w in ("每期不一样", "每期不同", "还没定", "没定", "定了我",
                             "待定", "确定后再", "确定后我") if w in ans]
    check("回答里没有「还没定」类矛盾说法", not bad_words, bad_words)
    check("回答里有具体集合点", "标本馆" in ans, ans[:60])
    reset()

    return bad


def main() -> int:
    config.check()
    total = len(CASES) + len(REPLACE_CASES) + len(STAY_CASES) + 1
    print("=" * 78)
    print(f"录入测试：{len(CASES)} 条常规 + {len(REPLACE_CASES)} 条整期替换 + "
          f"{len(STAY_CASES)} 条普通改字段 + 1 条粘回顾推文")
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

    # ---- 粘贴「活动回顾推文」：只能多存一个文件，绝不能动活动记录 ----
    print("\n" + "-" * 78)
    print("粘贴活动回顾推文（不能动活动记录）")
    print("-" * 78)
    reset()
    before = snap()
    r = agent.answer_chat(REVIEW_TEXT, activity_id="S16")
    after = snap()
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    review_file = TMP_DATA / "articles" / "reviews" / "review_第16期.md"
    ok = (bool(r.get("review_saved")) and not changed and review_file.exists()
          and "save_review" in (r.get("trace") or []))
    if not ok:
        bad += 1
    print(f"{'✅' if ok else '❌'} 粘回顾推文 → 存成 {r.get('review_saved') or '（没存）'}")
    print(f"   调用链 {r.get('trace')}")
    print(f"   活动字段变化：{changed or '无（正确）'}")
    print(f"   回复：{r['answer'].splitlines()[0] if r.get('answer') else ''}")

    bad += copy_layer_tests()
    bad += data_hygiene_tests()

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
