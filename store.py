"""
知识库读写层：把「客服在对话框里补充信息」这件事做安全

为什么单独一层：
    客服会在对话里陆续补字段（时间、集合点、价格、名额）。
    如果直接让模型改 JSON，风险是：字段名写错、类型写错（capacity 写成"25人"）、
    整个文件覆盖掉、录错了没得回滚。

    所以这里的职责是：
        1. 字段白名单 —— 只允许改约定好的字段，其余一律拒绝
        2. 类型校验   —— 日期必须是 YYYY-MM-DD，名额必须是数字
        3. 写前备份   —— 每次修改前存一份快照，录错能回滚
        4. 写后体检   —— 返回「这一期还缺哪些字段」，让客服知道下一步问什么
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

DATA_DIR = Path(config.DATA_DIR)
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 60          # 备份最多留多少份（超过就删最旧的）

# 允许客服更新的字段（白名单）。不在表里的键会被拒绝，防止模型乱写。
ALLOWED_FIELDS: Dict[str, str] = {
    "session_label": "期次，如 第十七期",
    "title": "活动名",
    "subtitle": "副标题 / 主题",
    "status": "报名中 / 已结束 / 待确认",
    "date": "活动日期，YYYY-MM-DD",
    "time_range": "活动时段，如 15:00-18:00",
    "location": "活动地点",
    "transport": "出行方式",
    "meeting_point": "集合地点",
    "meeting_time": "集合时间",
    "target_audience": "招募对象，如 小学4-6年级学生",
    "participant_format": "参与形式，如 亲子一大一小 / 学生独立参加",
    "indoor_outdoor": "室内还是室外（家长必问，推文常缺）",
    "grouping": "是否分组，如 分组 / 不分组",
    "group_size": "每组人数，如 4-5人一组（家长必问，推文常缺）",
    "capacity": "名额（数字）",
    "pricing": "价格，dict，如 {\"model\":\"阶梯同行价\",\"tiers\":[{\"label\":\"单人报名\",\"price\":88,\"unit\":\"元/人\"}]}",
    "fee_includes": "费用包含项，字符串列表",
    "insurance": "保险说明",
    "dress_code": "着装要求",
}

# 对外发文案前必须具备的字段（缺了就会在文案里留下 {{}}，或者答不上家长的问题）
REQUIRED_FOR_COPY = [
    ("date", "活动日期"),
    ("weekday", "星期几"),
    ("time_range", "活动时段"),
    ("location", "活动地点"),
    ("meeting_point", "集合地点"),
    ("meeting_time", "集合时间"),
    ("target_audience", "招募对象"),
    ("capacity", "名额"),
    ("pricing", "价格"),
    # ---- 活动形式：推文里最常缺、家长最常问的四项 ----
    ("indoor_outdoor", "室内还是室外"),
    ("grouping", "是否分组"),
    ("group_size", "每组人数"),
]

_STATUS_ENUM = {"报名中", "已结束", "待确认"}

# ---------------------------------------------------------------- 派生字段
# 这三个字段都能从别处算出来，所以**不存进文件**：
#   weekday           ← date（2026-10-24 一定是周六）
#   duration_minutes  ← time_range（15:00-18:00 = 180 分钟）
#   session_no        ← session_label（第十六期 → 16）
#
# 为何不允许手填：同一件事存两份，早晚不一致。真实后果是把
# 「10月25日（周六）」这种话发到家长群——日期改了星期没改，没人看得出来。
# 现在读的时候现算（with_derived），写的时候再摘掉（_write），文件里永不出现。
DERIVED_FIELDS = ("weekday", "duration_minutes", "session_no")
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _derive_weekday(date_str: Any) -> Optional[str]:
    try:
        y, m, d = (int(x) for x in str(date_str).split("-"))
        return _WEEKDAYS[datetime(y, m, d).weekday()]
    except Exception:
        return None


def _derive_duration(time_range: Any) -> Optional[int]:
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*[-–~至]\s*(\d{1,2}):(\d{2})",
                 str(time_range or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    minutes = (h2 * 60 + m2) - (h1 * 60 + m1)
    return minutes if minutes > 0 else None


def derive_fields(act: Dict[str, Any]) -> Dict[str, Any]:
    """从 date / time_range / session_label 算出派生字段。算不出就不给。"""
    out: Dict[str, Any] = {}
    wd = _derive_weekday(act.get("date"))
    if wd:
        out["weekday"] = wd
    dm = _derive_duration(act.get("time_range"))
    if dm:
        out["duration_minutes"] = dm
    no = session_number(act.get("session_label"))
    if no is not None:
        out["session_no"] = no
    return out


def with_derived(act: Dict[str, Any]) -> Dict[str, Any]:
    """读的时候把派生字段补上，让上层（面板/文案/体检）看起来字段齐全。"""
    if not isinstance(act, dict):
        return act
    out = dict(act)
    out.update(derive_fields(act))
    return out

# 中文数字：期次常写成「第十五期」，要能转成 15，用来给记录换编号
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def session_number(label: Any) -> Optional[int]:
    """从期次文字里取出编号：「第十六期」→16，「第9期」→9，「二十一期」→21。

    认不出就返回 None —— 宁可返回 None 让上层保留旧编号，
    也不要瞎猜一个数字（编号错了会串期，串期比没有编号麻烦得多）。
    """
    s = str(label or "").strip()
    if not s:
        return None
    m = re.search(r"\d+", s)
    if m:
        return int(m.group())
    m = re.search(r"([零一二两三四五六七八九十]+)", s)
    if not m:
        return None
    z = m.group(1)
    if "十" not in z:
        return _CN_DIGITS.get(z) if len(z) == 1 else None
    head, _, tail = z.partition("十")
    if (head and head not in _CN_DIGITS) or (tail and tail not in _CN_DIGITS):
        return None
    tens = _CN_DIGITS[head] if head else 1
    ones = _CN_DIGITS[tail] if tail else 0
    return tens * 10 + ones


# ---------------------------------------------------------------- 基础读写
def load() -> Dict[str, Any]:
    """读库。派生字段在这里补上——上层不需要知道它们是算出来的。"""
    with open(config.DATA_FILE, encoding="utf-8") as f:
        kb = json.load(f)
    kb["activities"] = [with_derived(a) for a in (kb.get("activities") or [])]
    return kb


def active_session_id() -> Optional[str]:
    """正在报名的那一期。用于客服没指明期次的场景（默认查这一期）。"""
    try:
        kb = load()
        for a in kb.get("activities", []):
            if a.get("status") == "报名中":
                return a["id"]
    except Exception:
        pass
    return None


def _backup() -> str:
    """写库前的快照。相同内容只存一份，并限制总份数。

    为什么不能只加个时间戳就完事（真实踩过的坑）：
        高频写库时同一个内容会被连续存成好几份（实测 446 个文件里有 302 个
        字节完全相同，最多的一组重复了 118 份，占 7.3 MB）。
        备份是为了回滚，不是为了堆垃圾——按内容哈希去重 + 只留最近 60 份。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    raw = Path(config.DATA_FILE).read_bytes()
    digest = hashlib.md5(raw).hexdigest()[:12]

    same = _backup_index().get(digest)
    if same:
        _prune_backups()                     # 顺手裁一次，保证上限永远有效
        return str(same)                     # 内容一模一样，不再存第二份

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"activities.{stamp}.{digest}.json"
    dst.write_bytes(raw)
    _prune_backups()
    return str(dst)


def _backup_index() -> Dict[str, Path]:
    """内容哈希 → 已有备份路径。老文件名没有哈希段，就现算。"""
    idx: Dict[str, Path] = {}
    if not BACKUP_DIR.exists():
        return idx
    for p in sorted(BACKUP_DIR.glob("activities.*.json")):
        try:
            digest = p.stem.split(".")[-1]
            if len(digest) != 12:
                digest = hashlib.md5(p.read_bytes()).hexdigest()[:12]
            idx.setdefault(digest, p)
        except OSError:
            continue
    return idx


def _prune_backups(keep: Optional[int] = None) -> int:
    """只留最近 keep 份，返回删掉几个。

    注意 keep 不能写成 `keep: int = BACKUP_KEEP`：默认参数在**函数定义时**求值，
    之后改 store.BACKUP_KEEP 不会生效（这个坑测试当场抓到了）。
    """
    keep = BACKUP_KEEP if keep is None else keep
    files = sorted(BACKUP_DIR.glob("activities.*.json"))
    if len(files) <= keep:
        return 0
    removed = 0
    for p in files[: len(files) - keep]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _write(kb: Dict[str, Any]) -> None:
    """落盘前把派生字段摘掉：文件里只留唯一真相（date / time_range / session_label）。"""
    out = dict(kb)
    out["activities"] = [
        {k: v for k, v in a.items() if k not in DERIVED_FIELDS}
        for a in (kb.get("activities") or [])
    ]
    with open(config.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def _archive_record(record: Dict[str, Any], reason: str) -> str:
    """把要被整条替换掉的记录追加进归档（一行一条，只往后加，不改旧行）。

    为什么单独存一份而不是只靠 _backup()：
        备份是整个文件的快照，几十份叠在一起几乎没法翻。
        这里是「某一条被替换了」的结构化流水，出问题能直接 grep 到是哪一期。

    目录用 DATA_DIR 现算，不要提成模块常量 —— 测试会把 DATA_DIR 指到临时目录，
    用常量就会写回真实数据（这个坑踩过）。
    """
    path = DATA_DIR / "archive" / "superseded.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "superseded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "record": record,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return str(path)


# ---------------------------------------------------------------- 校验
def _validate_one(field: str, value: Any) -> Tuple[bool, str]:
    """校验单个字段。返回 (是否通过, 错误说明)。"""
    if field == "date":
        if not (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())):
            return False, "日期必须是 YYYY-MM-DD 格式，例如 2026-11-07"
    elif field == "capacity":
        try:
            int(value)
        except (TypeError, ValueError):
            return False, "名额必须是数字，例如 25"
    elif field == "duration_minutes":
        try:
            int(value)
        except (TypeError, ValueError):
            return False, "时长必须是数字（分钟），例如 180"
    elif field == "status":
        if value not in _STATUS_ENUM:
            return False, f"状态只能是 {'/'.join(sorted(_STATUS_ENUM))}"
    elif field == "pricing":
        if not isinstance(value, dict) or "tiers" not in value:
            return False, "价格必须是 dict，且包含 tiers 列表"
        for t in value.get("tiers", []):
            if "price" not in t:
                return False, "每个价格档位都要有 price 字段"
            try:
                float(t["price"])
            except (TypeError, ValueError):
                return False, "价格档位里的 price 必须是数字"
    elif field == "fee_includes":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return False, "fee_includes 必须是字符串列表"
    return True, ""


# ---------------------------------------------------------------- 体检
def missing_fields(activity: Dict[str, Any]) -> List[Dict[str, str]]:
    """返回这一期还缺哪些字段（对外发文案前必须补齐的）。"""
    missing = []
    for field, label in REQUIRED_FOR_COPY:
        v = activity.get(field)
        if v in (None, "", [], {}):
            missing.append({"field": field, "label": label})
    return missing


def set_article_pointer(rel: str) -> bool:
    """设置「这一期的推文原文存在哪个文件」。只给代码用，不走模型，不在白名单里。"""
    kb = load()
    acts = kb.get("activities") or []
    if not acts:
        return False
    acts[0]["article"] = rel
    acts[0]["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _backup()
    _write(kb)
    return True


# ---------------------------------------------------------------- 对外接口
def update_activity(activity_id: Optional[str], updates: Dict[str, Any],
                    replace: bool = False, keep_article: bool = True) -> Dict[str, Any]:
    """更新**当前活动**的字段。

    存储模型是单活动：文件里只有一条，就是正在办的那一期。
    所以 activity_id 只是为了兼容旧调用和日志，实际一律写那一条 ——
    传进来的期次和文件里的对不上，说明模型带了过期的 id，以文件为准。

    两种写模式（这是真实报过的 bug 之后加的，不要改回一种）：
      · 合并（replace=False，默认）
            只覆盖这次给的字段，其余保持不动。适用于「同一期的信息补充或更正」，
            比如客服说「集合点改到五教104」。
      · 整条替换（replace=True）
            以一条空白记录为底，**只保留这次抽到的字段**，没抽到的一律清空。
            适用于「客服粘进来的是另一期的推文」。
            为什么必须清空：不清理的话，新一期会继承上一期的集合点、名额、室内外、
            分组这些推文里没写的东西，客服照着答家长就是答错一期的事。
            旧记录先整条进 archive/superseded.jsonl，不做静默丢弃。

    设计要点：
      · 只接受白名单字段，未知字段直接拒绝并在返回里列出
      · 逐字段校验，一个失败就整体不写（避免写一半）
      · 写前自动备份
      · 返回「还缺什么」，让客服知道下一步该问负责人什么
    """
    if not isinstance(updates, dict) or not updates:
        return {"ok": False, "error": "没有收到要更新的字段"}

    kb = load()
    acts = kb.get("activities") or []
    if not acts:
        return {"ok": False, "error": "知识库里还没有活动记录"}
    target = acts[0]
    key = target.get("id")

    rejected: List[Dict[str, str]] = []
    errors: List[str] = []
    accepted: Dict[str, Any] = {}

    for field, value in updates.items():
        if field not in ALLOWED_FIELDS:
            rejected.append({"field": field, "reason": "不在允许更新的字段清单里"})
            continue
        ok, msg = _validate_one(field, value)
        if not ok:
            errors.append(f"{field}: {msg}")
            continue
        accepted[field] = int(value) if field == "capacity" else value

    if errors:
        return {"ok": False, "error": "字段格式有问题，本次未写入", "details": errors}

    if not accepted:
        return {"ok": False, "error": "没有可写入的字段", "rejected": rejected}

    backup_path = _backup()
    archive_path = ""
    dropped: List[str] = []

    if replace:
        old = dict(target)
        archive_path = _archive_record(
            old, reason=f"整条替换为 {accepted.get('session_label') or '新一期'}")
        # 从空白开始：只有 id / article / 这次抽到的字段，别的一概不继承
        new: Dict[str, Any] = {}
        if old.get("id"):
            new["id"] = old["id"]
        if keep_article and old.get("article"):
            new["article"] = old["article"]
        new.update(accepted)

        no = session_number(new.get("session_label"))
        if no is not None:
            new["id"] = f"S{no:02d}"      # session_no 不写，读的时候由 label 算
        # 期次没写或认不出 → id 沿用旧的：编号错会串期，串期比没编号麻烦得多

        new.setdefault("status", "待确认")
        new["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        new["_replaced_from"] = str(old.get("session_label") or old.get("id") or "")
        new["_replaced_at"] = new["_last_updated"]

        # 派生字段不算「被丢掉的字段」：它们本来就不该存在文件里
        dropped = sorted(k for k in old
                         if k not in new
                         and k not in ("id", "article")
                         and k not in DERIVED_FIELDS)
        kb["activities"] = [new]
        target = new
    else:
        target.update(accepted)
        target["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    _write(kb)

    return {
        "ok": True,
        "activity_id": target.get("id") or key,
        "replaced": replace,
        "updated_fields": list(accepted.keys()),
        "rejected_fields": rejected,
        "dropped_fields": dropped,
        "backup": backup_path,
        "archive": archive_path,
        "still_missing": missing_fields(target),
        "note": (
            "已写入并备份。请把 still_missing 里的项目告诉客服，"
            "说明这些还需要向活动负责人确认后再补充。"
        ),
    }
