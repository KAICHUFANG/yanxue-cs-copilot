"""
山海科普 · 客服工作台（图形界面）

界面形态：左边对话，右边状态。所有输入都走对话框，客服不需要学任何表单。

为什么状态面板要单独一块、而且**由代码算**：
    实测过——问模型「现在信息齐了吗」，它会把「集合时间有值但可疑」算成缺口，
    同时漏掉真正缺的「是否分组」「每组人数」。
    所以缺口的答案不能问模型，只能由 store.missing_fields() 读磁盘算出来。
    模型负责说话，代码负责说真话。

启动：
    双击 启动工作台.bat
    或：.venv\\Scripts\\python.exe app_workbench.py
"""

import json
import queue
import threading
import tkinter as tk
from tkinter import ttk

import agent
import config
import knowledge
import renderer
import store

BG = "#f7f7f5"
PANEL_BG = "#ffffff"
FG_USER = "#1a5fb4"
FG_BOT = "#1c1c1c"
FG_MUTED = "#8a8a8a"
FG_OK = "#2a7d2a"
FG_MISS = "#b35c00"
FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Consolas", 9)

# 面板上显示的字段名。用 store 里的描述会把「室内还是室外」原样露出来，
# 客服照着抄进对话框就变成了一句问句，反而写不进去（真实踩过）。
# 所以这里单独给一列短名。
DISPLAY_NAME = {
    "session_label": "期次",
    "title": "活动名",
    "subtitle": "主题",
    "status": "状态",
    "date": "活动日期",
    "weekday": "星期",
    "time_range": "活动时段",
    "duration_minutes": "时长(分)",
    "location": "活动地点",
    "transport": "出行方式",
    "meeting_point": "集合地点",
    "meeting_time": "集合时间",
    "target_audience": "招募对象",
    "participant_format": "参与形式",
    "indoor_outdoor": "室内/室外",
    "grouping": "分组",
    "group_size": "每组人数",
    "capacity": "名额",
    "pricing": "价格",
    "fee_includes": "费用包含",
    "insurance": "保险",
    "dress_code": "着装要求",
    "segments": "活动环节",
    "highlights": "亮点",
    "awards": "奖项",
    "learning_outcomes": "研学收获",
}

# 枚举字段：告诉客服应该说什么词，避免她自由发挥
ENUM_HINT = {
    "indoor_outdoor": "说「室内」或「室外」",
    "grouping": "说「分组」或「不分组」",
    "status": "说「报名中」「已结束」「待确认」",
}


class Workbench:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.conversation = []
        self.activity_id = knowledge.current_activity_id()
        self.busy = False
        self.q = queue.Queue()
        self.last_bot = ""
        self.tokens = 0

        root.title("山海科普 · 客服工作台")
        root.geometry("1180x760")
        root.minsize(900, 600)
        root.configure(bg=BG)

        self._build()
        self.refresh_status()
        self._poll()
        self.sys("欢迎。左边打字就行：\n"
                 "  · 家长问什么，直接打进来\n"
                 "  · 要补信息，**直接把值说出来**：「室外」「每组4到5人」「集合时间16:00」\n"
                 "    只打一个词也可以，程序会直接写库，不用等模型判断\n"
                 "  · 如果你打的是问句（「室内还是室外」「几点集合」），会当成提问来回答\n"
                 "  · 右边会实时显示这一期还缺什么，写完立刻更新")

    # ------------------------------------------------------------ 界面骨架
    def _build(self):
        # 右边先 pack，保证窗口变窄时它不会被挤掉
        right = tk.Frame(self.root, bg=BG, width=330)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 12), pady=12)
        right.pack_propagate(False)

        left = tk.Frame(self.root, bg=BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12, pady=12)

        self._build_right(right)
        self._build_left(left)

    # ------------------------------------------------------------ 右侧：状态
    def _build_right(self, parent):
        # 当前活动（单活动模型：没有期次可选，就是正在办的那一期）
        top = tk.Frame(parent, bg=BG)
        top.pack(fill=tk.X)
        tk.Label(top, text="当前活动", font=FONT_SMALL, bg=BG, fg=FG_MUTED).pack(anchor="w")
        act = knowledge.get_activity() or {}
        head = f"{act.get('session_label', '')}　{act.get('title', '')}".strip() or "（还没有活动）"
        self.act_label = tk.Label(top, text=head, font=("Microsoft YaHei UI", 10, "bold"),
                                  bg=BG, fg=FG_BOT, anchor="w", justify="left", wraplength=300)
        self.act_label.pack(fill=tk.X, pady=(2, 8))

        # 字段状态清单（可滚动）
        box = tk.Frame(parent, bg=PANEL_BG, relief=tk.SOLID, borderwidth=1)
        box.pack(fill=tk.BOTH, expand=True)

        head = tk.Frame(box, bg=PANEL_BG)
        head.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(head, text="信息状态", font=("Microsoft YaHei UI", 10, "bold"),
                 bg=PANEL_BG).pack(side=tk.LEFT)
        self.miss_label = tk.Label(head, text="", font=FONT_SMALL, bg=PANEL_BG, fg=FG_MISS)
        self.miss_label.pack(side=tk.RIGHT)

        wrap = tk.Frame(box, bg=PANEL_BG)
        wrap.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.canvas = tk.Canvas(wrap, bg=PANEL_BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.rows = tk.Frame(self.canvas, bg=PANEL_BG)
        self.rows.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 底部按钮
        btns = tk.Frame(parent, bg=BG)
        btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btns, text="生成群文案", command=self.gen_copy).pack(fill=tk.X)
        ttk.Button(btns, text="刷新状态", command=self.refresh_status).pack(fill=tk.X, pady=(4, 0))

        # 内部提示
        tk.Label(parent, text="客服提示（不发给家长）", font=FONT_SMALL,
                 bg=BG, fg=FG_MUTED).pack(anchor="w", pady=(10, 2))
        self.notes = tk.Text(parent, height=7, font=FONT_SMALL, wrap=tk.WORD,
                             bg="#fffbe8", relief=tk.SOLID, borderwidth=1, padx=8, pady=6)
        self.notes.pack(fill=tk.X)
        self.notes.configure(state=tk.DISABLED)

    # ------------------------------------------------------------ 左侧：对话
    def _build_left(self, parent):
        self.history = tk.Text(parent, wrap=tk.WORD, font=FONT, bg="white",
                               relief=tk.FLAT, padx=14, pady=12, state=tk.DISABLED)
        self.history.pack(fill=tk.BOTH, expand=True)
        for tag, kw in {
            "user": {"foreground": FG_USER, "font": ("Microsoft YaHei UI", 10, "bold")},
            "bot": {"foreground": FG_BOT},
            "sys": {"foreground": FG_MUTED, "font": FONT_SMALL},
            "warn": {"foreground": FG_MISS},
            "tool": {"foreground": FG_MUTED, "font": FONT_MONO},
        }.items():
            self.history.tag_config(tag, **kw)

        box = tk.Frame(parent, bg=BG)
        box.pack(fill=tk.X, pady=(8, 0))
        self.input = tk.Text(box, height=4, font=FONT, wrap=tk.WORD,
                             relief=tk.SOLID, borderwidth=1, padx=10, pady=8)
        self.input.pack(fill=tk.X)
        self.input.bind("<Control-Return>", lambda e: (self.send(), "break")[1])

        row = tk.Frame(box, bg=BG)
        row.pack(fill=tk.X, pady=(6, 0))
        self.send_btn = ttk.Button(row, text="发送（Ctrl+Enter）", command=self.send)
        self.send_btn.pack(side=tk.LEFT)
        ttk.Button(row, text="复制上一条", command=self.copy_last).pack(side=tk.LEFT, padx=6)
        ttk.Button(row, text="清空对话", command=self.reset).pack(side=tk.LEFT)

        self.want_notes = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="同时生成客服提示", variable=self.want_notes).pack(side=tk.LEFT, padx=12)

        self.status = ttk.Label(row, text="就绪", foreground=FG_MUTED)
        self.status.pack(side=tk.RIGHT)

    # ------------------------------------------------------------ 状态刷新
    def refresh_status(self):
        """完全由代码算，不看模型说了什么。"""
        act = knowledge.get_activity(self.activity_id)
        for w in self.rows.winfo_children():
            w.destroy()
        if not act:
            self.miss_label.configure(text="无此期次")
            return

        missing = {m["field"] for m in store.missing_fields(act)}
        required = {f for f, _ in store.REQUIRED_FOR_COPY}

        # 必填在前，其他在后
        order = [f for f, _ in store.REQUIRED_FOR_COPY]
        order += [f for f in store.ALLOWED_FIELDS if f not in required]

        for f in order:
            if f not in store.ALLOWED_FIELDS:
                continue
            v = act.get(f)
            filled = v not in (None, "", [], {})
            is_req = f in required
            if filled:
                icon, color = "✓", FG_OK
            elif is_req:
                icon, color = "✗", FG_MISS
            else:
                icon, color = "·", FG_MUTED

            row = tk.Frame(self.rows, bg=PANEL_BG)
            row.pack(fill=tk.X, padx=4, pady=1)
            tk.Label(row, text=icon, font=FONT_SMALL, bg=PANEL_BG, fg=color,
                     width=2).pack(side=tk.LEFT)
            label = DISPLAY_NAME.get(f) or store.ALLOWED_FIELDS[f].split("，")[0].split("（")[0]
            if is_req:
                label = "★" + label
            tk.Label(row, text=label, font=FONT_SMALL, bg=PANEL_BG,
                     fg=FG_BOT if filled else FG_MUTED, width=9, anchor="w").pack(side=tk.LEFT)
            shown = self._fmt(v)
            if not filled and f in ENUM_HINT:
                shown = ENUM_HINT[f]
            tk.Label(row, text=shown, font=FONT_SMALL, bg=PANEL_BG,
                     fg=FG_BOT if filled else "#bbbbbb", anchor="w",
                     wraplength=170, justify="left").pack(side=tk.LEFT, fill=tk.X, expand=True)

        n = len(missing)
        self.miss_label.configure(
            text="信息齐全" if n == 0 else f"还缺 {n} 项",
            fg=FG_OK if n == 0 else FG_MISS,
        )

    @staticmethod
    def _fmt(v) -> str:
        if v in (None, "", [], {}):
            return "—"
        if isinstance(v, dict):
            return "（复杂字段，已录入）"
        if isinstance(v, list):
            if v and isinstance(v[0], str):
                return "、".join(v)[:60]
            return f"（{len(v)} 条，已录入）"
        return str(v)[:60]

    # ------------------------------------------------------------ 动作
    def send(self):
        if self.busy:
            return
        text = self.input.get("1.0", tk.END).strip()
        if not text:
            return
        self.input.delete("1.0", tk.END)
        self.append("👤 " + text, "user")
        self._pending = text
        self.busy = True
        self.send_btn.configure(state=tk.DISABLED)
        self.status.configure(text="思考中…")
        threading.Thread(target=self._work, args=(text,), daemon=True).start()

    def _work(self, text):
        try:
            r = agent.answer_chat(text, activity_id=self.activity_id,
                                  history=self.conversation)
            notes = []
            if self.want_notes.get():
                try:
                    import pipeline
                    notes = pipeline.internal_notes(
                        text, r["answer"], knowledge.build_context(self.activity_id),
                        [text], [m["label"] for m in r.get("missing", [])],
                    )
                except Exception:
                    notes = []
            self.q.put(("ok", r, notes))
        except Exception as e:
            self.q.put(("err", str(e), []))

    def _poll(self):
        try:
            kind, payload, notes = self.q.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll)
            return

        if kind == "err":
            self.append("⚠ 出错了：" + payload, "warn")
            self.status.configure(text="出错")
        else:
            if payload.get("wrote"):
                self.append("   ✎ 已写入：" + "、".join(payload["wrote"]), "tool")
            for err in (payload.get("write_errors") or []):
                self.append("   ✗ 没写进去：" + err, "warn")
            self.append("🤖 " + payload["answer"], "bot")
            self.last_bot = payload["answer"]
            self.tokens += payload.get("usage", {}).get("total_tokens", 0)
            # 只保留「问-答」文本进上下文；工具调用细节不进，避免污染后续对话
            self.conversation.append({"role": "user", "content": self._pending})
            self.conversation.append({"role": "assistant", "content": payload["answer"]})
            # 写完立刻刷新，让「状态有没有变」当场可见
            self.refresh_status()
            self.set_notes(notes)
            self.status.configure(text=f"就绪 · 本次会话 {self.tokens} tokens")

        self.busy = False
        self.send_btn.configure(state=tk.NORMAL)
        self.root.after(120, self._poll)

    def gen_copy(self):
        try:
            text = renderer.render_group_messages(self.activity_id)
        except Exception as e:
            self.append("⚠ 生成失败：" + str(e), "warn")
            return
        self.append("📋 群文案（可直接复制）\n" + text, "bot")
        self.last_bot = text

    def copy_last(self):
        if not self.last_bot:
            self.status.configure(text="还没有可复制的内容")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_bot)
        self.status.configure(text="✅ 已复制，可直接粘到群里")

    def reset(self):
        self.conversation = []
        self.sys("（对话上下文已清空，状态面板不受影响）")

    # ------------------------------------------------------------ 小工具
    def append(self, text, tag):
        self.history.configure(state=tk.NORMAL)
        self.history.insert(tk.END, text + "\n\n", tag)
        self.history.configure(state=tk.DISABLED)
        self.history.see(tk.END)

    def sys(self, text):
        self.append(text, "sys")

    def set_notes(self, notes):
        self.notes.configure(state=tk.NORMAL)
        self.notes.delete("1.0", tk.END)
        self.notes.insert("1.0", "\n".join("· " + n for n in notes) if notes else "（无）")
        self.notes.configure(state=tk.DISABLED)


def main():
    config.check()
    print(config.describe())
    root = tk.Tk()
    Workbench(root)
    root.mainloop()


if __name__ == "__main__":
    main()
