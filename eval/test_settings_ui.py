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
        check("团队信息是独立一栏", all(k in keys for k in ("team_name", "team_phone")),
              str(keys))

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

        # ---- ④ 团队信息：独立保存，且换期不丢 ----
        import json
        import store
        at.text_input(key="team_name").set_value("测试老师")
        at.text_input(key="team_phone").set_value("13800001111")
        at.button(key="team_save").click().run()
        check("保存团队信息不报错", not at.exception,
              str(at.exception[0].value) if at.exception else "")
        kb = json.loads(TMP_ACT.read_text(encoding="utf-8"))
        check("团队信息存在 activities.json 顶层",
              (kb.get("team_info") or {}).get("visit_target_name") == "测试老师")
        check("团队信息没有被写进 .env",
              "测试老师" not in ENV.read_text(encoding="utf-8"))
        # 模拟换期：整条替换后 team_info 必须还在
        store.update_activity(None, {"session_label": "第九十九期"}, replace=True)
        kb2 = json.loads(TMP_ACT.read_text(encoding="utf-8"))
        check("换期后团队信息还在（因为不在 activities[0] 里）",
              (kb2.get("team_info") or {}).get("visit_target_name") == "测试老师")
        check("换期后 activities[0] 里没有 team_info 副本",
              "team_info" not in (kb2.get("activities") or [{}])[0])

        # ---- ⑤ 使用记录：问一句就应该落一行日志 ----
        import chatlog
        # 日志也要指到副本，否则测试的假问句会混进客服真实记录
        chatlog.LOG_DIR = TMP_DATA / "logs"
        chatlog.LOG_FILE = chatlog.LOG_DIR / "chat.jsonl"

        check("界面上有「使用记录」面板（认它的说明文字）",
              "永久保留" in texts(at) and "手机号已打码" in texts(at), texts(at)[:120])
        at.text_input(key="operator_name").set_value("测试客服")
        at.chat_input[0].set_value("集合地点在哪").run()
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
