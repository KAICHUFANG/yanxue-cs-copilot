"""用 Streamlit 官方 AppTest 驱动界面：验证「填 Key → 保存并测试」这条路真的通。

为什么不用浏览器点：浏览器只能验证「看起来对不对」，验证不了按钮背后的逻辑。
AppTest 直接跑脚本、set 输入、click 按钮、读 st.success/st.error，等于把界面当函数测。

用法：
    .venv\\Scripts\\python.exe eval\\test_settings_ui.py
"""

import json
import os
import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
EVAL_DIR = Path(__file__).resolve().parent
TMP_DATA = EVAL_DIR / "tmp_ui_data"

# ⚠️ 必须先复制数据目录、把路径指过去，再 import config/store/knowledge。
#    第一版没做这件事，测试里的「模拟换期」把真实的 activities.json 冲成了
#    「第九十九期」（只剩 7 个字段），靠备份才恢复。
#    教训：凡是会走写库路径的测试，隔离 DATA_DIR 一个都不能少。
shutil.rmtree(TMP_DATA, ignore_errors=True)
shutil.copytree(PROJ / "data", TMP_DATA, ignore=shutil.ignore_patterns("backups"))
os.environ["ACTIVITIES_FILE"] = str(TMP_DATA / "activities.json")

sys.path.insert(0, str(PROJ))

from streamlit.testing.v1 import AppTest        # noqa: E402

import config                                   # noqa: E402
import store                                    # noqa: E402
import agent                                    # noqa: E402

config.DATA_DIR = TMP_DATA
config.DATA_FILE = TMP_DATA / "activities.json"
store.DATA_DIR = TMP_DATA
store.BACKUP_DIR = TMP_DATA / "backups"

ENV = PROJ / ".env"
BAK = PROJ / ".env.testbak"
TMP_ACT = TMP_DATA / "activities.json"

fails = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global fails
    if not ok:
        fails += 1
    print(f"  {'✅' if ok else '❌'} {name}{('   ' + extra) if extra and not ok else ''}")


def texts(at) -> str:
    """把界面上所有提示类元素拼成一段，方便断言。"""
    parts = []
    for kind in ("success", "error", "info", "warning", "caption", "markdown"):
        for el in getattr(at, kind, []):
            parts.append(str(getattr(el, "value", "")))
    return "\n".join(parts)


def main() -> int:
    shutil.copy2(ENV, BAK)
    print("=" * 72)
    print("界面「API 设置」：填 Key → 保存并测试")
    print("=" * 72)

    try:
        # ---- ① 打开页面：设置面板应该在，且显示当前 Key 打码 ----
        at = AppTest.from_file(str(PROJ / "app_streamlit.py"), default_timeout=90)
        at.run()
        body = texts(at)
        check("页面能跑起来（没有异常）", not at.exception,
              str(at.exception[0].value) if at.exception else "")
        check("显示当前 Key 的打码值", config.mask(config.API_KEY)[:8] in body
              or "当前：Key" in body)
        keys = [w.key for w in at.text_input]
        check("API 面板只有三个字段（Key / 接口 / 模型）",
              all(k in keys for k in ("cfg_key", "cfg_base", "cfg_model")),
              str(keys))
        check("被访人不在 API 面板里（它是业务信息，不是 API 配置）",
              "cfg_name" not in keys and "cfg_phone" not in keys, str(keys))
        check("团队信息也不在界面上（走对话框改，用户要求移出窗口）",
              "team_name" not in keys and "team_phone" not in keys, str(keys))

        # ---- ② 不改 Key，只点「保存并测试连接」----
        at.button(key="cfg_save").click().run()
        body = texts(at)
        check("点按钮后没有异常", not at.exception,
              str(at.exception[0].value) if at.exception else "")
        check("提示保存成功 / 连接正常", ("已保存" in body) or ("连接正常" in body), body[:200])
        check("没有报连接失败", "连不上" not in body)

        # ---- ③ 填一个不存在的模型名：应该「保存了但连不上」----
        at.text_input(key="cfg_model").set_value("no-such-model-xyz")
        at.button(key="cfg_save").click().run()
        body = texts(at)
        check("错误模型名会报错，不是静默通过", "连不上" in body, body[:200])
        check("报错里给了排查方向", "接口地址" in body or "/v1" in body, body[:200])
        check(".env 里确实写成了那个模型名",
              "no-such-model-xyz" in ENV.read_text(encoding="utf-8"))

        # 立刻还原！不然后面那次真实问答会拿着坏模型名去请求，直接 404，
        # 表现为「日志里 0 条」——排查了半天才发现是测试自己挖的坑。
        shutil.copy2(BAK, ENV)
        config.reload()
        import agent as _agent
        _agent.reset_client()
        check("坏模型名验证完后已还原为可用模型", config.MODEL in ("deepseek-chat", "deepseek-flash"),
              config.MODEL)

        # ---- ④ 团队信息：界面上没有面板，走对话框改，且换期不丢 ----
        import knowledge
        check("界面上没有「团队信息」面板（用户要求移出窗口）",
              "团队信息" not in texts(at), "界面上仍有团队信息相关文字")

        kb = json.loads(TMP_ACT.read_text(encoding="utf-8"))
        before_team = dict(kb.get("team_info") or {})
        r = agent.answer_chat("被访人电话改成 13900002222")
        check("走对话框改团队信息，走的是 save_team_info",
              "save_team_info" in (r.get("trace") or []), str(r.get("trace")))
        kb2 = json.loads(TMP_ACT.read_text(encoding="utf-8"))
        check("新电话写进了顶层 team_info",
              (kb2.get("team_info") or {}).get("visit_target_phone") == "13900002222")
        check("没写进 activities[0]（否则换期会丢）",
              "visit_target_phone" not in (kb2.get("activities") or [{}])[0])
        # 模拟换期：整条替换后 team_info 必须还在
        store.update_activity(None, {"session_label": "第九十九期"}, replace=True)
        kb3 = json.loads(TMP_ACT.read_text(encoding="utf-8"))
        check("换期后团队信息还在",
              (kb3.get("team_info") or {}).get("visit_target_phone") == "13900002222")
        knowledge.save_team_info(before_team.get("visit_target_name", ""),
                                before_team.get("visit_target_phone", ""))

        # ---- ④b 「显示文案发布流程」按钮：只给流程，不给正文 ----
        _flow_btn = [b for b in at.button if "发布流程" in (b.label or "")]
        check("右边有「显示文案发布流程」按钮", bool(_flow_btn),
              str([b.label for b in at.button]))
        check("「生成全部群文案」按钮已移除",
              not [b for b in at.button if "生成全部群文案" in (b.label or "")])
        if _flow_btn:
            _flow_btn[0].click().run()
            body_flow = texts(at)
        else:
            body_flow = ""
        check("流程里说明了时间线（活动前 2 天 / 前一天 / 当天）",
              "活动前 2 天" in body_flow and "前一天" in body_flow, body_flow[:120])
        check("流程里包含「流程之外」的定位与引导图",
              "定位" in body_flow and "引导图" in body_flow)
        check("提醒了「发定位时要跟家长说明引导图稍后补」",
              "引导图" in body_flow and "说明" in body_flow)
        check("流程里不含正文文案（不应出现群公告开头）",
              "各位家长好，欢迎加入" not in body_flow, "流程里混进了正文")

        # ---- ⑤ 使用记录：问一句就应该落一行日志 ----
        import chatlog
        # 日志也要指到副本，否则测试的假问句会混进客服真实记录。
        # 另外必须先清空副本里的日志：data/logs 已经有真实使用记录，
        # 跟着 copytree 一起复制过来，断言「恰好 1 条」就会不成立（踩过）。
        chatlog.LOG_DIR = TMP_DATA / "logs"
        chatlog.LOG_FILE = chatlog.LOG_DIR / "chat.jsonl"
        if chatlog.LOG_FILE.exists():
            chatlog.LOG_FILE.unlink()

        check("界面上没有「使用记录」面板（记录只落文件）",
              "使用记录" not in texts(at), "界面上仍有使用记录相关文字")
        config.CHAT_LOG_OPERATOR = "测试客服"      # 现在从 .env 读，测试里直接给
        # 输入框已从左栏表单提交（不再是全宽 chat_input）
        at.text_input(key="ask_text").set_value("集合地点在哪")
        _send = [b for b in at.button if (b.label or "") == "发送"]
        check("左栏里有「发送」按钮", bool(_send), str([b.label for b in at.button]))
        _send[0].click().run()
        rows = chatlog.tail(10)
        check("问一句之后日志里有 1 条", len(rows) == 1, f"实际 {len(rows)} 条")
        if rows:
            r0 = rows[0]
            check("日志记下了问题原文", "集合地点在哪" in (r0.get("q") or ""), str(r0.get("q"))[:60])
            check("日志记下了答案", bool(r0.get("a")))
            check("日志记下了操作者", r0.get("operator") == "测试客服", r0.get("operator"))
            check("日志记下了走的路和 tokens",
                  bool(r0.get("trace")) and int(r0.get("tokens") or 0) > 0,
                  str(r0.get("trace")))
        check("真实日志目录没被写", not (PROJ / "data" / "logs" / "chat.jsonl").exists()
              or "集合地点在哪" not in (PROJ / "data" / "logs" / "chat.jsonl").read_text(encoding="utf-8"))

        # ---- ⑥ 全量保存 + 回答不截断（数据层，界面不显示记录）----
        for i in range(24):
            chatlog.append(f"历史问题{i:02d}", {"answer": f"历史回答{i:02d}",
                                             "trace": ["<context>"],
                                             "usage": {"total_tokens": 100}})
        long_tail = "【这一句在回答的最后，必须还能看到】"
        long_answer = "集合点在海南大学海洋生物标本馆。" * 12 + long_tail
        chatlog.append("长回答测试", {"answer": long_answer, "trace": ["<context>"],
                                     "usage": {"total_tokens": 4200}})
        rows2 = chatlog.all_rows()
        check("all_rows 返回全部 26 条（不限条数）", len(rows2) == 26, f"实际 {len(rows2)}")
        check("最早那条也在（证明不是只留 20 轮）",
              any(r.get("q") == "历史问题00" for r in rows2))
        check("长回答完整保存，末尾标记在（证明没截断到 40 字）",
              (rows2[0].get("a") or "").endswith(long_tail), (rows2[0].get("a") or "")[-25:])
    finally:
        # 无论上面怎么挂，都把 .env 还原，绝不留一个坏模型名给客服
        shutil.copy2(BAK, ENV)
        BAK.unlink(missing_ok=True)
        config.reload()
        print(f"\n  ↩️  .env 已还原（当前模型：{config.MODEL}）")

    print("\n" + ("全部通过" if not fails else f"失败 {fails} 条"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
