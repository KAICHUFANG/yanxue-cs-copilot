"""用 Streamlit 官方 AppTest 驱动界面：验证「填 Key → 保存并测试」这条路真的通。

为什么不用浏览器点：浏览器只能验证「看起来对不对」，验证不了按钮背后的逻辑。
AppTest 直接跑脚本、set 输入、click 按钮、读 st.success/st.error，等于把界面当函数测。

用法：
    .venv\\Scripts\\python.exe eval\\test_settings_ui.py
"""

import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from streamlit.testing.v1 import AppTest        # noqa: E402

import config                                   # noqa: E402

ENV = PROJ / ".env"
BAK = PROJ / ".env.testbak"

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
        check("五个输入框都在", all(k in [w.key for w in at.text_input]
                                for k in ("cfg_key", "cfg_base", "cfg_model",
                                          "cfg_name", "cfg_phone")),
              str([w.key for w in at.text_input]))

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
