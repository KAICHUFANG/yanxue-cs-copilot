"""
命令行入口（图形界面是主入口，这个用于排查和批量试问）

用法：
    .venv\\Scripts\\python.exe main.py              # 交互模式
    .venv\\Scripts\\python.exe main.py --demo       # 跑几条预设，验证环境
    .venv\\Scripts\\python.exe main.py --flow       # 打印群文案流程总览
    .venv\\Scripts\\python.exe main.py 集合地点在哪   # 直接问一句

命令（交互模式里可用）：
    /reset   清空多轮上下文
    /usage   看本次会话花了多少 token
    /quit    退出
"""

import sys

import agent
import config
import knowledge
import store

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


DEMO = [
    "集合地点在哪里？",
    "几年级能参加？",
    "要自己带什么东西？",
    "是不是只能从北门进出，西门能不能走",
    "这次活动不是室内的",
    "现在信息齐了吗",
]


def show(label: str, r: dict) -> None:
    print(f"\n👤 {label}")
    if r.get("wrote"):
        print(f"   ✎ 已写入：{'、'.join(r['wrote'])}")
    for e in r.get("write_errors") or []:
        print(f"   ✗ 没写进去：{e}")
    print(f"🤖 {r['answer']}")
    print(f"   [调用 {' → '.join(r.get('trace', []))} | {r['usage'].get('total_tokens', 0)} tokens]")


def print_flow() -> None:
    for st in knowledge.load_group_messages().get("timeline", []):
        print(f"\n{st['stage']}　{st.get('when', '')}")
        for msg in st.get("messages", []):
            manual = msg.get("type") == "manual_send_required"
            print(f"  · {msg.get('label')} [{'需手动发' if manual else '可复制发'}]")


def run_demo() -> None:
    for q in DEMO:
        show(q, agent.answer_chat(q))
    act = knowledge.get_activity() or {}
    left = [m["label"] for m in store.missing_fields(act)]
    print(f"\n当前活动：{act.get('session_label')} {act.get('title')}")
    print(f"还缺：{'、'.join(left) if left else '无'}")


def run_interactive() -> None:
    history = []
    tokens = 0
    print("输入问题回车发送。命令：/reset /usage /quit\n")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ("/quit", "/q"):
            break
        if q == "/reset":
            history = []
            print("（上下文已清空）")
            continue
        if q == "/usage":
            print(f"本次会话共 {tokens} tokens")
            continue

        r = agent.answer_chat(q, history=history)
        show(q, r)
        tokens += r["usage"].get("total_tokens", 0)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": r["answer"]})


def main() -> None:
    config.check()
    print(config.describe())
    act = knowledge.get_activity() or {}
    print(f"当前活动：{act.get('session_label')} {act.get('title')}\n")

    args = sys.argv[1:]
    if "--flow" in args:
        print_flow()
    elif "--demo" in args:
        run_demo()
    elif args:
        show(" ".join(args), agent.answer_chat(" ".join(args)))
    else:
        run_interactive()


if __name__ == "__main__":
    main()
