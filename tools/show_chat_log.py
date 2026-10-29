"""查看使用记录（客服问了什么、系统答了什么）。

记录不进界面——工作台是干活的地方，复盘用这个脚本看。
数据在 data/logs/chat.jsonl，永久保留、全量、回答不截断。

用法：
    uv run python tools/show_chat_log.py                 # 最近 20 轮（默认）
    uv run python tools/show_chat_log.py -n 100          # 最近 100 轮
    uv run python tools/show_chat_log.py --all           # 全部
    uv run python tools/show_chat_log.py --grep 集合点    # 只看包含某关键词的（问或答）
    uv run python tools/show_chat_log.py --json          # 原样输出 JSONL（给别的脚本用）
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chatlog           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--num", type=int, default=20, help="看最近多少轮")
    ap.add_argument("--all", action="store_true", help="全部（可能很长）")
    ap.add_argument("--grep", default="", help="只显示问或答里包含该关键词的")
    ap.add_argument("--json", action="store_true", help="原样输出 JSONL")
    ap.add_argument("--no-answer", action="store_true", help="只看问题，不看回答")
    args = ap.parse_args()

    if args.json:
        sys.stdout.write(chatlog.read_all_bytes().decode("utf-8", "replace"))
        return 0

    path = chatlog.path()
    if not path.exists():
        print(f"还没有记录。文件会在第一次问答时创建：{path}")
        return 0

    rows = chatlog.all_rows()                 # 最近的在前，全部
    if args.grep:
        kw = args.grep
        rows = [r for r in rows
                if kw in (r.get("q") or "") or kw in (r.get("a") or "")]
    if not args.all:
        rows = rows[: args.num]

    st = chatlog.stats()
    print("=" * 78)
    print(f"记录文件：{path}")
    print(f"共 {st['turns']} 轮，累计 {st['tokens']:,} tokens"
          + (f"，最早 {st['first']}" if st["turns"] else ""))
    print(f"本次显示 {len(rows)} 轮（最近的在前）")
    print("=" * 78)

    for r in rows:
        wrote = "、".join(r.get("wrote") or [])
        print(f"\n[{r.get('ts', '')}] {r.get('operator', '')}"
              f"  ·  {r.get('trace', '')}  ·  {r.get('tokens', 0)} tokens"
              + (f"  ·  写入 {wrote}" if wrote else ""))
        print(f"问：{r.get('q', '')}")
        if not args.no_answer:
            print("答：")
            for line in (r.get("a") or "").splitlines():
                print("    " + line)
        print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
