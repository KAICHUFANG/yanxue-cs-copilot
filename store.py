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

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

DATA_DIR = Path(config.DATA_DIR)
BACKUP_DIR = DATA_DIR / "backups"

# 允许客服更新的字段（白名单）。不在表里的键会被拒绝，防止模型乱写。
ALLOWED_FIELDS: Dict[str, str] = {
    "session_label": "期次，如 第十七期",
    "title": "活动名",
    "subtitle": "副标题 / 主题",
    "status": "报名中 / 已结束 / 待确认",
    "date": "活动日期，YYYY-MM-DD",
    "weekday": "星期，如 周六",
    "time_range": "活动时段，如 15:00-18:00",
    "duration_minutes": "时长（分钟，数字）",
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
    "segments": "活动环节，列表，每项含 name 与 detail",
    "highlights": "活动亮点，字符串列表",
    "awards": "奖项，字符串列表",
    "learning_outcomes": "研学收获，字符串列表",
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


# ---------------------------------------------------------------- 基础读写
def load() -> Dict[str, Any]:
    with open(config.DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


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
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"activities.{stamp}.json"
    shutil.copy2(config.DATA_FILE, dst)
    return str(dst)


def _write(kb: Dict[str, Any]) -> None:
    with open(config.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(kb, f, ensure_ascii=False, indent=2)


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
    elif field in ("fee_includes", "highlights", "awards", "learning_outcomes"):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return False, f"{field} 必须是字符串列表"
    elif field == "segments":
        if not isinstance(value, list):
            return False, "segments 必须是列表"
        for s in value:
            if not isinstance(s, dict) or "name" not in s:
                return False, "每个活动环节都要有 name 字段"
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


# ---------------------------------------------------------------- 对外接口
def update_activity(activity_id: Optional[str], updates: Dict[str, Any]) -> Dict[str, Any]:
    """更新**当前活动**的字段。

    存储模型是单活动：文件里只有一条，就是正在办的那一期。
    所以 activity_id 只是为了兼容旧调用和日志，实际一律写那一条 ——
    传进来的期次和文件里的对不上，说明模型带了过期的 id，以文件为准。

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
        accepted[field] = int(value) if field in ("capacity", "duration_minutes") else value

    if errors:
        return {"ok": False, "error": "字段格式有问题，本次未写入", "details": errors}

    if not accepted:
        return {"ok": False, "error": "没有可写入的字段", "rejected": rejected}

    backup_path = _backup()
    target.update(accepted)
    target["_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _write(kb)

    return {
        "ok": True,
        "activity_id": key,
        "updated_fields": list(accepted.keys()),
        "rejected_fields": rejected,
        "backup": backup_path,
        "still_missing": missing_fields(target),
        "note": (
            "已写入并备份。请把 still_missing 里的项目告诉客服，"
            "说明这些还需要向活动负责人确认后再补充。"
        ),
    }
