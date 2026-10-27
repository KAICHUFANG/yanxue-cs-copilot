"""从数据生成「群文案原文」文档。

为什么要有这个脚本：
    文案原文以前同时存在于 data/group_messages.json 和 docs/活动群文案流程.md，
    两份手工维护。最常见的翻车方式是：系统里改了措辞，文档还是旧的——
    客服照着文档发出去，跟系统的输出不一致，而且没人发现。

    现在数据是唯一来源，文档这一页由脚本生成，页头写明「不要手改」。
    改文案 = 只改 data/group_messages.json（或 processes.json），然后跑一次本脚本。

用法：
    uv run python tools/gen_copy_doc.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config           # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "群文案原文.md"


def _load(name: str) -> dict:
    with open(Path(config.DATA_DIR) / name, encoding="utf-8") as f:
        return json.load(f)


def _emit_proc(items: list, lines: list) -> None:
    """写流程发送包里的条目。显式传 lines——用 += 会把它变成本地变量。"""
    for it in items:
        lines.append(f"### 第 {it.get('seq')} 件 · {it.get('label', '')}")
        lines.append("")
        if it.get("type") == "manual_send_required":
            note = it.get("content_note") or it.get("text") or ""
            lines.extend([
                f"**【需要客服手动发】** {note}",
                "",
                f"> 系统发不了这一项，原因：{it.get('why') or '（无）'}",
                "",
            ])
        else:
            lines.extend(["```", (it.get("text") or "").strip(), "```", ""])


def _resolve(procs: dict, ref: str) -> str:
    """解析 campus_booking.entry_gate.ready_to_send_text 这种点号路径。"""
    node: object = procs
    try:
        for part in ref.split("."):
            node = node[part]          # type: ignore[index]
        return node if isinstance(node, str) else ""
    except Exception:
        return f"【引用解析失败：{ref}】"


def main() -> int:
    book = _load("group_messages.json")
    procs = _load("processes.json")

    lines: list = [
        "# 活动群文案原文",
        "",
        "> ⚠️ **本文件由 `tools/gen_copy_doc.py` 自动生成，不要手改。**",
        "> 数据源：`data/group_messages.json` + `data/processes.json`。",
        "> 改文案请改数据文件再重新生成；`{{}}` 会在系统生成时按当期活动自动填入。",
        "",
        "---",
        "",
    ]

    for stage in book["timeline"]:
        lines.append(f"## {stage['stage']}")
        lines.append("")
        if stage.get("when"):
            lines.extend([f"*{stage['when']}*", ""])

        # 阶段引用的流程：按 items[].seq 展开，顺序与系统实际发送顺序一致
        # （顺序只存在 seq 一处，不再维护第二份 order 列表）
        proc = procs.get(stage["uses_process"]) if stage.get("uses_process") else None
        pkg = (proc or {}).get("send_package") or {}
        proc_items = sorted(pkg.get("items") or [], key=lambda x: x.get("seq", 99))
        cut = stage.get("insert_after_seq")
        head = [i for i in proc_items if cut is not None and i.get("seq", 99) <= cut]
        tail = [i for i in proc_items if cut is not None and i.get("seq", 99) > cut]
        if proc_items and cut is None:
            head, tail = proc_items, []

        _emit_proc(head, lines)

        for msg in stage.get("messages") or []:
            if msg.get("type") == "manual_send_required":
                lines.extend([
                    f"### {msg.get('label', '')}",
                    "",
                    f"**【需要客服手动发】** {msg.get('content_note') or msg.get('text') or ''}",
                    "",
                ])
                continue
            if msg.get("type") == "reference":
                text = _resolve(procs, msg.get("ref", ""))
                lines.extend([f"### {msg.get('label', '')}", "", "```",
                              text.strip(), "```", ""])
                continue
            lines.extend([f"### {msg.get('label', '')}", "", "```",
                          (msg.get("text") or "").strip(), "```", ""])

        _emit_proc(tail, lines)

        tips = [t.get("text") for t in (pkg.get("handler_tips") or []) if t.get("text")]
        if tips:
            lines.extend(["**这一步必须人工做的**", ""])
            lines.extend(f"- {t}" for t in tips)
            lines.append("")

        lines.extend(["---", ""])

    out = "\n".join(lines).rstrip() + "\n"
    OUT.write_text(out, encoding="utf-8", newline="\n")
    print(f"已生成 {OUT}")
    print(f"  阶段 {len(book['timeline'])} 个，共 {len(out)} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
