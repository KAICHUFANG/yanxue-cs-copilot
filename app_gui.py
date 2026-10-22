"""
山海科普 · 客服助手（图形界面版）

设计原则：
    1. 零新增依赖 —— 只用 Python 自带的 tkinter，装不坏、不会因为版本更新挂掉
    2. 不改动任何现有代码 —— 完全复用 agent.ask()，界面只是外面的一层壳
    3. 多行输入框 —— 推文、聊天记录可以直接粘贴，不需要 /paste 那种别扭的命令

启动方式：
    双击 启动助手.bat
    或：.venv\\Scripts\\pythonw.exe app_gui.py
"""

import json
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

import agent
import config
import tools

# 颜色
BG = "#f7f7f5"
FG_USER = "#1a5fb4"
FG_BOT = "#1c1c1c"
FG_TOOL = "#8a8a8a"
FG_WARN = "#b35c00"
FONT = ("Microsoft YaHei UI", 10)


class ChatApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.conversation = []          # 多轮上下文
        self.session_tokens = 0
        self.busy = False
        self.result_queue = queue.Queue()
        self.last_bot = ""              # 最后一条回复，供「复制」用

        root.title("山海科普 · 客服助手")
        root.geometry("920x720")
        root.configure(bg=BG)
        root.minsize(700, 520)

        self._build_ui()
        self._poll_queue()

        self.append_system(
            "欢迎使用。直接说人话就行，例如：\n"
            "  · 第十六期多少钱？\n"
            "  · 帮我写第十六期的活动群文案\n"
            "  · 第十六期集合时间是14:40，集合地点在海南大学北门\n\n"
            "推文、聊天记录可以直接粘贴到下面的输入框。"
        )

    # ---------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        # 聊天记录
        self.history = scrolledtext.ScrolledText(
            self.root, wrap=tk.WORD, font=FONT, bg="white", fg=FG_BOT,
            padx=14, pady=12, relief=tk.FLAT, state=tk.DISABLED,
        )
        self.history.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 6))
        self.history.tag_config("user", foreground=FG_USER, font=("Microsoft YaHei UI", 10, "bold"))
        self.history.tag_config("bot", foreground=FG_BOT)
        self.history.tag_config("tool", foreground=FG_TOOL, font=("Consolas", 8))
        self.history.tag_config("warn", foreground=FG_WARN)
        self.history.tag_config("sys", foreground="#666666", font=("Microsoft YaHei UI", 9, "italic"))

        # 输入区
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill=tk.X, padx=12, pady=(0, 12))

        self.input_box = tk.Text(
            bottom, height=4, font=FONT, wrap=tk.WORD,
            padx=10, pady=8, relief=tk.SOLID, borderwidth=1,
        )
        self.input_box.pack(fill=tk.X, side=tk.TOP)
        self.input_box.bind("<Control-Return>", lambda e: (self.send(), "break")[1])

        btn_row = tk.Frame(bottom, bg=BG)
        btn_row.pack(fill=tk.X, pady=(8, 0))

        self.send_btn = ttk.Button(btn_row, text="发送（Ctrl+Enter）", command=self.send)
        self.send_btn.pack(side=tk.LEFT)

        ttk.Button(btn_row, text="复制上一条回复", command=self.copy_last).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btn_row, text="清空对话", command=self.reset).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btn_row, text="文案流程总览", command=self.show_flow).pack(side=tk.LEFT, padx=(8, 0))

        self.show_tool = tk.BooleanVar(value=True)
        ttk.Checkbutton(btn_row, text="显示工具调用", variable=self.show_tool).pack(side=tk.LEFT, padx=(16, 0))

        self.status = ttk.Label(btn_row, text="就绪", foreground="#666666")
        self.status.pack(side=tk.RIGHT)

    # ---------------------------------------------------------------- 记录区
    def _append(self, text: str, tag: str) -> None:
        self.history.configure(state=tk.NORMAL)
        self.history.insert(tk.END, text + "\n\n", tag)
        self.history.configure(state=tk.DISABLED)
        self.history.see(tk.END)

    def append_user(self, text: str) -> None:
        self._append("👤 " + text, "user")

    def append_bot(self, text: str) -> None:
        self.last_bot = text
        self._append("🤖 " + text, "bot")

    def copy_last(self) -> None:
        """把最后一条回复复制到剪贴板。

        这个按钮看着不起眼，但它决定工具会不会被真正用起来：
        客服拿到回复后要能直接 Ctrl+V 发到家长群，否则就要手选、手抄。
        """
        if not self.last_bot:
            self.status.configure(text="还没有可复制的回复")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_bot)
        self.status.configure(text="✅ 已复制到剪贴板，可直接粘贴到群里")

    def append_tool(self, text: str) -> None:
        self._append("   " + text, "tool")

    def append_system(self, text: str) -> None:
        self._append(text, "sys")

    def append_warn(self, text: str) -> None:
        self._append("⚠ " + text, "warn")

    # ---------------------------------------------------------------- 动作
    def send(self) -> None:
        if self.busy:
            return
        text = self.input_box.get("1.0", tk.END).strip()
        if not text:
            return
        self.input_box.delete("1.0", tk.END)

        self.append_user(text)
        self.conversation.append({"role": "user", "content": text})

        self.busy = True
        self.send_btn.configure(state=tk.DISABLED)
        self.status.configure(text="思考中…")

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        """在后台线程里调模型，避免界面卡死。结果丢进队列，由主线程取走渲染。"""
        try:
            result = agent.ask(self.conversation, verbose=False)
            self.result_queue.put(("ok", result))
        except Exception as e:
            self.result_queue.put(("error", str(e)))

    def _poll_queue(self) -> None:
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll_queue)
            return

        if kind == "error":
            self.append_warn("出错了：" + payload)
            self.status.configure(text="出错")
        else:
            if self.show_tool.get() and payload.get("trace"):
                self.append_tool("🔧 调用了：" + "、".join(payload["trace"]))
            self.append_bot(payload["answer"])
            self.session_tokens += payload["usage"].get("total_tokens", 0)
            self.status.configure(
                text=f"就绪 · 本次会话 {self.session_tokens} tokens"
            )

        self.busy = False
        self.send_btn.configure(state=tk.NORMAL)
        self.root.after(120, self._poll_queue)

    def reset(self) -> None:
        self.conversation = []
        self.append_system("（对话上下文已清空）")

    def show_flow(self) -> None:
        """把文案流程总览直接渲染进对话区（不经过模型）。"""
        try:
            path = config.DATA_FILE.parent / "group_messages.json"
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.append_warn("读取流程失败：" + str(e))
            return

        lines = ["【活动群文案流程总览】"]
        for st in data["timeline"]:
            lines.append(f"\n{st['stage']}　{st.get('when', '')}")
            if st.get("uses_process"):
                lines.append("  · 引用进校预约定稿（问「怎么预约进校」会给你全文）")
            for msg in st.get("messages", []):
                manual = msg.get("type") == "manual_send_required"
                flag = "★需手动发" if manual else "可复制发"
                lines.append(f"  · {msg['label']} [{flag}]（{msg.get('source', '—')}）")
            for todo in st.get("handler_todos", []):
                lines.append(f"  ⚠ 待办：{todo['title']}")
        lines.append("\n完整版见 docs/活动群文案流程.md")
        self._append("\n".join(lines), "sys")


def main() -> None:
    config.check()
    print(config.describe())          # 控制台里留下启动摘要，方便排查 key/模型问题
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
