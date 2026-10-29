"""把「客服问了什么、系统答了什么」记下来。

为什么单独一个模块，而不是塞进 agent.answer_chat：
    answer_chat 也被评测脚本调用。如果把记录写在里面，
    跑一次 29 条评测就会往业务日志里灌 29 条假问句，
    真数据反而被稀释到看不出来。所以**只在界面层记**。

三个设计决定：
    ① 一行一条 JSON（JSONL）—— 追加写、不用读整个文件、出问题只坏一行
    ② 手机号打码 —— 对话里会带家长的号码，日志是长期存的，不能原样留
    ③ **永久保留，不做自动清理** —— 用户明确要求：这是审计记录，
       删了就查不到「当时到底回了什么」。要清理只能人工处理。
       文件变大不设上限，需要归档时用界面上那个「下载完整记录」。
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

LOG_DIR = Path(config.DATA_DIR) / "logs"
LOG_FILE = LOG_DIR / "chat.jsonl"

MASK_PHONE = True          # 11 位手机号 → 138****0000

# 中国大陆手机号：1 开头、11 位。前面不能紧贴数字，避免把订单号切一半
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


def _mask(text: str) -> str:
    if not MASK_PHONE or not text:
        return text or ""
    return _PHONE_RE.sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], text)


def path() -> Path:
    return LOG_FILE


def append(question: str, result: Dict[str, Any],
           operator: Optional[str] = None) -> None:
    """记一轮对话。

    result 直接用 answer_chat / render_copy 的返回值，所以 trace（走了哪条路）、
    wrote（写了哪些字段）、tokens 都是**事实**，不是事后猜的。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        op = (operator if operator is not None
              else getattr(config, "CHAT_LOG_OPERATOR", "")).strip()
        item = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "operator": op or "未署名",
            "q": _mask(str(result.get("question") or question or "")),
            "a": _mask(str(result.get("answer") or "")),
            "trace": " → ".join(str(t) for t in (result.get("trace") or [])),
            "wrote": result.get("wrote") or [],
            "tokens": (result.get("usage") or {}).get("total_tokens", 0),
            "replaced": bool(result.get("replaced")),
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        # 记日志失败绝不能影响客服干活。宁可丢一条记录，不能让对话挂掉。
        pass


def _read_all() -> List[Dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    out = []
    for ln in LOG_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue            # 坏行跳过，不影响其它记录
    return out


def all_rows() -> List[Dict[str, Any]]:
    """全部记录，最近的在前。

    界面默认显示全部，不做条数上限——这是用户明确要求的：
    「全部保留，不要只保留 20 轮」。数据本来就是全量存的，
    之前只是展示层砍成了 20 条 + 回答前 40 字，等于白存。
    """
    return _read_all()[::-1]


def tail(n: int = 20) -> List[Dict[str, Any]]:
    """最近 n 条（脚本里临时看看用；界面不用它）。"""
    return _read_all()[-n:][::-1]


def stats() -> Dict[str, Any]:
    rows = _read_all()
    return {
        "turns": len(rows),
        "tokens": sum(int(r.get("tokens") or 0) for r in rows),
        "first": rows[0]["ts"] if rows else "",
        "last": rows[-1]["ts"] if rows else "",
    }


def read_all_bytes() -> bytes:
    """给「下载完整记录」用。"""
    return LOG_FILE.read_bytes() if LOG_FILE.exists() else b""


