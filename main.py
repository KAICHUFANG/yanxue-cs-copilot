"""
命令行入口

用法：
    python main.py              交互模式，一行一个问题
    python main.py --demo       自动跑 6 个预设问题（用来验证 + 截图）

内置命令（交互模式下输入）：
    /reset   清空对话历史，重新开始
    /usage   看本次会话累计 token
    /quit    退出
"""

import sys

import agent
import config

# Windows 控制台默认是 GBK 编码，直接打印 emoji 会抛 UnicodeEncodeError。
# 这里把输出流切成 UTF-8，避免「代码没问题但一跑就崩」。
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

DEMO_QUESTIONS = [
    "最近有什么活动可以报名？",
    "第十六期多少钱？",
    "第十六期适合多大的孩子？家长要陪着吗？",
    "下雨的话会取消吗？怎么退费？",
    "第二期是什么活动？",
    "第六期和第十六期分别多少钱？",
]


def print_flow() -> None:
    """打印活动群文案流程总览（「栏」的 CLI 版，给人看，不经过模型）。

    这些内容是有标准答案的，直接展示原文比让模型复述更可靠。
    """
    import json

    path = config.DATA_FILE.parent / "group_messages.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    print()
    print("=" * 64)
    print("活动群文案流程总览")
    print("=" * 64)
    for st in data["timeline"]:
        print(f"\n{st['stage']}")
        print(f"  什么时候：{st.get('when','—')}")
        if st.get("uses_process"):
            print(f"  引用流程：{st['uses_process']}（见进校预约定稿）")
        for msg in st.get("messages", []):
            manual = msg.get("type") == "manual_send_required"
            flag = "★需手动发" if manual else "可复制发"
            print(f"    · {msg['label']}  [{flag}]  ({msg.get('source','—')})")
        for todo in st.get("handler_todos", []):
            print(f"    ⚠ 待办：{todo['title']}")
    print("\n" + "=" * 64)
    print("想改某一条文案，直接把那条发给我，我在对话框里帮你改。")
    print("完整版见：docs/活动群文案流程.md")
    print("=" * 64)


def print_answer(result: dict) -> None:
    print()
    print("🤖 " + result["answer"].replace("\n", "\n   "))
    print()
    print(f"   [工具调用 {result['tool_rounds']} 轮 | tokens {result['usage']['total_tokens']}]")


def run_demo() -> None:
    print("=" * 60)
    print("演示模式：依次提问", len(DEMO_QUESTIONS), "个问题")
    print("=" * 60)
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        print()
        print(f"[{i}/{len(DEMO_QUESTIONS)}] 👤 {q}")
        # 每个问题独立成一轮对话：必须把问题本身放进 messages，
        # 否则模型只看到 system prompt，根本不知道家长问的是什么。
        conv = [{"role": "user", "content": q}]
        result = agent.ask(conv, verbose=True)
        print_answer(result)
    print("=" * 60)
    print("演示结束")


def run_interactive() -> None:
    print("=" * 60)
    print("山海科普 · 客服助手 v0.1")
    print("=" * 60)
    print("直接说人话就行，比如：")
    print("  · 第十六期多少钱？")
    print("  · 帮我写第十六期的活动群文案")
    print("  · 第十六期价格是单人88，两人同行78，三人同行68")
    print()
    print("命令：")
    print("  /paste   粘贴多行内容（推文、聊天记录）")
    print("  /flow    看活动群文案流程总览")
    print("  /reset   清空上下文    /usage 看用量    /quit 退出")
    print("=" * 60)

    conversation = []
    session_tokens = 0

    while True:
        try:
            question = input("\n👤 ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not question:
            continue
        if question in ("/quit", "/exit", "/q"):
            print("再见。")
            break
        if question == "/reset":
            conversation = []
            print("（上下文已清空）")
            continue
        if question == "/usage":
            print(f"本次会话累计 tokens: {session_tokens}")
            continue
        if question == "/flow":
            print_flow()
            continue
        if question == "/paste":
            print("（多行模式）把内容粘进来，粘完单独一行输入 /end 结束：")
            lines = []
            while True:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    break
                if line.strip() == "/end":
                    break
                lines.append(line)
            question = "\n".join(lines).strip()
            if not question:
                print("（没有内容，已取消）")
                continue
            print(f"（已接收 {len(lines)} 行，{len(question)} 字）")

        conversation.append({"role": "user", "content": question})
        result = agent.ask(conversation, verbose=True)
        session_tokens += result["usage"]["total_tokens"]
        print_answer(result)


if __name__ == "__main__":
    config.check()
    print(config.describe())
    print()
    if "--demo" in sys.argv:
        run_demo()
    elif "--flow" in sys.argv:
        print_flow()
    else:
        run_interactive()
