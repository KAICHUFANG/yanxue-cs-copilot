"""清理 data/backups/：去重 + 只留最近 N 份。

为什么需要单独一个脚本（而不是让 store 自己搞完）：
    store 里的 _backup() 从今往后已经会「内容相同就不存第二份」，
    但历史上已经堆下来的那堆垃圾不会自己消失。这个脚本处理存量。

用法：
    uv run python tools/clean_backups.py           # 真删
    uv run python tools/clean_backups.py --dry-run # 只看会删什么
"""

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config           # noqa: E402
import store            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", type=int, default=store.BACKUP_KEEP)
    args = ap.parse_args()

    bak_dir = Path(config.DATA_DIR) / "backups"
    files = sorted(bak_dir.glob("activities.*.json"))
    if not files:
        print("没有备份文件")
        return 0

    size_before = sum(p.stat().st_size for p in files)

    # ① 按内容哈希去重：同一份内容只留最早的那一份（最早的那份最接近「原始状态」）
    seen: dict = {}
    dupes = []
    for p in files:
        digest = hashlib.md5(p.read_bytes()).hexdigest()
        if digest in seen:
            dupes.append(p)
        else:
            seen[digest] = p

    print(f"备份总数 {len(files)} 个（{size_before / 1024 / 1024:.2f} MB）")
    print(f"  其中内容完全相同的多余副本：{len(dupes)} 个")
    print(f"  去重后剩：{len(seen)} 个")

    # ② 再按数量上限裁剪：只留最近 keep 份
    uniq = sorted(seen.values())
    overflow = uniq[: max(0, len(uniq) - args.keep)]
    if overflow:
        print(f"  超过上限（留最近 {args.keep} 份），再删：{len(overflow)} 个")

    todo = dupes + overflow
    if args.dry_run:
        print(f"[dry-run] 将删除 {len(todo)} 个，不会真删")
    else:
        ok = 0
        for p in todo:
            try:
                p.unlink()
                ok += 1
            except OSError as e:
                print(f"  删除失败 {p.name}: {e}")
        print(f"已删除 {ok} 个文件")

    left = sorted(bak_dir.glob("activities.*.json"))
    size_after = sum(p.stat().st_size for p in left)
    print(f"\n结果：{len(files)} 个 / {size_before / 1024 / 1024:.2f} MB"
          f" → {len(left)} 个 / {size_after / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
