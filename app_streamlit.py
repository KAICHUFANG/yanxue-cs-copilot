"""
山海科普 · 客服工作台（Streamlit 版）

为什么换框架：
    原来的 tkinter 版能用，但每加一个交互都要手写事件绑定和布局，
    而且没法流式输出、没法在手机上打开、样式也很难看。
    换成 Streamlit 之后：
      · 布局用列（st.columns）直接表达，不用手算像素
      · 回答能逐字显示（st.write_stream 的效果）
      · 文本块自带「复制」按钮，客服不用手动选中
      · 浏览器打开，将来放服务器上就能远程用

界面形态没变，还是客服早就定下来的那个：
    左边对话，右边只读状态。所有输入都走对话框，不学表单。

启动：
    双击 启动工作台.bat
    或：.venv\\Scripts\\python -m streamlit run app_streamlit.py
"""

import streamlit as st

import agent
import chatlog
import config
import knowledge
import store

# ---------------------------------------------------------------- 页面设置
st.set_page_config(
    page_title="山海科普 · 客服工作台",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------- 旧进程自检
# 真实遇到过：新增 knowledge.load_team_info 之后，界面直接报
#   AttributeError: module 'knowledge' has no attribute 'load_team_info'
# 看着像代码坏了，其实只是**运行中的进程还是旧代码**（Streamlit 只重跑本脚本，
# 已 import 的模块不会重新加载）。这里把「重启」这句话直接摆到界面上。
_EXPECT_CODE_VERSION = 7
if getattr(config, "CODE_VERSION", 0) != _EXPECT_CODE_VERSION:
    st.error("**页面代码已更新，但当前运行的进程还是旧的。**"
             + chr(10) + chr(10)
             + "关掉那个黑色的启动窗口，重新双击「启动工作台.bat」就好。"
             + chr(10) + chr(10)
             + f"（代码版本 {_EXPECT_CODE_VERSION}，进程里是 "
             + f"{getattr(config, 'CODE_VERSION', '未知')}）")
    st.caption("改完 .py 文件后 Streamlit 会重跑脚本，但已经加载的模块要重启进程才会更新。")
    st.stop()


FONT_CSS = """
<style>
  .block-container { padding-top: 4.2rem; padding-bottom: 2rem; }
  div[data-testid="stMetricValue"] { font-size: 1.4rem; }
  .tiny { color: #888; font-size: 0.82rem; }

  /* ================= 右侧状态面板：独立栏位 =================
     客服的诉求：对话一长，想看一眼缺口就得往上滑半天，因为那块面板随页面一起走。

     为什么不用 position: sticky（试过，不行）：
         sticky 只能在**父容器的高度范围**内停泊。对话滚到最底时父容器已到头，
         面板会被顶出屏幕（实测 top 变成 -130px），而那正好是客服看最新消息的位置。
         给父容器加底部留白也无效：留白让可滚动区域同步变大，钳制位置 1:1 跟着跑
         （实测 0/160/320/480/640px 留白，面板 top 依次是 -130/-290/-441/-601/-761）。

     所以用 position: fixed 把它钉在视口右上角，与父容器无关。
     实测：滚到 0 / 800 / 1600 / 最底，面板 top 恒为 58px。

     两个必需配套：
       ① 左侧对话列要 max-width 让位，否则文字会钻到面板底下；
       ② 面板必须有不透明底色（否则消息会从后面穿过去）。
          颜色不能用 Fixed 值、也不能靠 prefers-color-scheme 猜：
          st.context.theme 只给一个 type（实测明亮实例里也报 dark，不可信），
          而系统主题跟 Streamlit 里手动切的主题是两回事。
          所以用一小段 JS 量页面真实背景色写进 --panel-bg（见下方 THEME_SYNC_HTML）。

     窄屏（<1000px）不启用：流式布局本来就把两列上下堆叠，钉住反而遮挡对话。---- */
  :root { --panel-bg: __PANEL_BG__; --panel-line: __PANEL_LINE__; }
__THEME_FALLBACK__

  @media (min-width: 1000px) {
    div[data-testid="stColumn"]:has(.status-anchor) {
        position: fixed;
        top: 4rem;
        right: 1rem;
        width: 22rem;
        flex: none;
        max-height: calc(100vh - 6rem);
        overflow-y: auto;
        z-index: 50;
        padding: 0.2rem 0.5rem 0.6rem 0.9rem;
        background: var(--panel-bg);
        border-left: 1px solid var(--panel-line);
    }
    div[data-testid="stHorizontalBlock"]:has(.status-anchor)
      > div[data-testid="stColumn"]:first-child {
        max-width: calc(100% - 23.6rem);
    }
  }
</style>
"""
# 右侧固定面板必须有不透明底色，否则消息会从它后面穿过去。
# 但「底色该用什么」不能靠猜：
#   · st.context.theme 只给一个 type（实测在明亮实例里也报 dark），不可靠；
#   · prefers-color-scheme 跟的是「系统」主题，用户在 Streamlit 里手动切成明亮时就不对了
#     （这就是「明亮模式下侧栏有一块不变白」的原因）。
# 所以注入一小段 JS：直接量页面真实背景色，写进 CSS 变量，并监听主题切换。
# 组件 iframe 与主页面同源，可以访问 parent.document；万一被拦，CSS 里的默认值兜底。
# 注意这里是 raw string（r"""）：里面有 JS 正则 \( \s \d，
# 不用 r 前缀 Python 会报 SyntaxWarning: invalid escape sequence。
THEME_SYNC_HTML = r"""
<script>
(function () {
  function paint() {
    try {
      var d = window.parent.document;
      var bg = window.parent.getComputedStyle(d.body).backgroundColor || "";
      var solid = /^rgba?\(/.test(bg) && bg.indexOf("rgba(0, 0, 0, 0)") !== 0;
      var isDark = /rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(bg);
      var dark = false;
      if (isDark) {
        dark = (parseInt(isDark[1], 10) + parseInt(isDark[2], 10) + parseInt(isDark[3], 10)) < 3 * 128;
      }
      var root = d.documentElement;
      root.style.setProperty("--panel-bg", solid ? bg : (dark ? "#0e1117" : "#ffffff"));
      root.style.setProperty("--panel-line", dark ? "rgba(255,255,255,0.16)" : "rgba(0,0,0,0.12)");
    } catch (e) { /* 被沙箱拦住就用 CSS 默认值 */ }
  }
  paint();
  try {
    new MutationObserver(paint).observe(window.parent.document.documentElement,
      { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
  } catch (e) {}
  setInterval(paint, 1200);
})();
</script>
"""

# CSS 默认值：明亮。真正的颜色由上面的 JS 覆盖（读不到就保持白色，安全）
FONT_CSS = FONT_CSS.replace("__PANEL_BG__", "#ffffff").replace("__PANEL_LINE__", "rgba(0,0,0,0.12)")
FONT_CSS = FONT_CSS.replace("__THEME_FALLBACK__", "  /* 颜色由页面内 JS 按真实背景色写入 --panel-bg */")
st.markdown(FONT_CSS, unsafe_allow_html=True)
try:
    from streamlit.components.v1 import html as _html
    _html(THEME_SYNC_HTML, height=0)
except Exception:
    pass


# ---------------------------------------------------------------- 启动自检
@st.cache_resource
def _startup_check() -> str:
    """只在第一次运行时检查一次，别每次交互都查。"""
    try:
        config.check()
        return ""
    except SystemExit as e:
        return str(e)


_err = _startup_check()

# 一次性提示（flash）：保存成功之后要 st.rerun() 刷新界面，
# 而 rerun 会把这一轮画的东西全部冲掉——不存进 session_state，用户就看不到任何反馈。
_flash = st.session_state.pop("cfg_flash", "")
if _flash:
    st.success(_flash)


with st.expander("🔑 API 设置 · 第一次使用请先填这里", expanded=bool(_err)):
    if _err:
        # 首行用红色报错，剩下的用代码块：
        # st.error 走的是 Markdown，Windows 路径里的反斜杠会被当转义符吃掉
        # （实测「...\客服工作台\.env」显示成「...\客服工作台.env」）。
        _lines = _err.splitlines()
        st.error(_lines[0] if _lines else _err)
        if len(_lines) > 1:
            st.code(chr(10).join(_lines[1:]), language=None)
    st.caption("这些值只保存在本机 `.env` 文件里，不会上传、不会进 git。"
               "换 Key / 换模型就再来这里改一次，改完点下面的按钮。")

    st.markdown(
        f"当前：Key `{config.mask(config.API_KEY) if config.API_KEY else '（未填）'}`"
        f"　·　接口 `{config.BASE_URL}`　·　模型 `{config.MODEL}`"
    )

    _c1, _c2 = st.columns([1.7, 1])
    _key_in = _c1.text_input(
        "API Key", value="", type="password", key="cfg_key",
        placeholder="粘贴你自己的 Key（留空 = 不修改现有 Key）",
        help="DeepSeek 开放平台 → API keys → 创建。只显示一次，记得当场粘贴。")
    _base_in = _c2.text_input("接口地址", value=config.BASE_URL, key="cfg_base",
                              help="换别家时填它的 OpenAI 兼容地址，例如带 /compatible-mode/v1")

    _model_in = st.text_input("模型名", value=config.MODEL, key="cfg_model",
                              help="要和接口配套：用了百炼的地址就不能写 deepseek-chat")

    if st.button("保存并测试连接", type="primary", key="cfg_save"):
        _updates = {
            "DEEPSEEK_BASE_URL": _base_in.strip() or config.BASE_URL,
            "MODEL_NAME": _model_in.strip() or config.MODEL,
        }
        if _key_in.strip():                      # 留空就不覆盖已有 Key
            _updates["DEEPSEEK_API_KEY"] = _key_in.strip()

        config.save_env(_updates)
        config.reload()                          # 重读 .env，否则模块常量还是旧值
        agent.reset_client()                     # 客户端创建时就把 Key 定死了，必须重建
        _startup_check.clear()

        _ok, _info = agent.ping_model()           # 真发一次请求，否则错误留给客服
        if _ok:
            # 存成 flash 再 rerun，否则这句提示会被整页重跑冲掉（测试抓到的）
            st.session_state["cfg_flash"] = (
                f"已保存到 .env，连接正常（模型回：{_info}）。可以正常使用了。")
            st.rerun()
        else:
            st.error(f"已保存，但连不上：{_info}")
            st.info("按顺序查三样：① Key 是否复制完整（没带空格）"
                    "② 接口地址是否带 /v1 或 /compatible-mode/v1 "
                    "③ 模型名是否和这个接口配套。")

if _err and not config.API_KEY:
    st.stop()


# ---------------------------------------------------------------- 会话状态
if "msgs" not in st.session_state:
    st.session_state.msgs = []          # [{role, content, wrote, errors, notes}]
if "tokens" not in st.session_state:
    st.session_state.tokens = 0
if "flow_text" not in st.session_state:
    st.session_state.flow_text = ""      # 「文案发布流程」清单（只讲流程，不含正文）

act = knowledge.get_activity() or {}
missing = store.missing_fields(act)
required = {f for f, _ in store.REQUIRED_FOR_COPY}


def _label(field: str) -> str:
    """字段 → 界面上给人看的中文名。

    三级兜底（顺序别改）：
      ① DISPLAY_NAME 里写好的短名（「集合地点」）
      ② ALLOWED_FIELDS 里的描述取前半段（「这次活动有没有导航图：有 / 没有…」→ 截到冒号前）
      ③ 实在没有才显示英文键名 —— 但这时应该去补 ①，不要让客服看英文。
    """
    from app_workbench import DISPLAY_NAME
    if field in DISPLAY_NAME:
        return DISPLAY_NAME[field]
    desc = (store.ALLOWED_FIELDS.get(field) or "").split("，")[0].split("（")[0].split("：")[0].strip()
    return desc or field


def _fmt(value) -> str:
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, dict):
        return "（已录入）"
    if isinstance(value, list):
        return "、".join(map(str, value))[:40] if value and isinstance(value[0], str) else f"（{len(value)} 条）"
    return str(value)[:40]


# ---------------------------------------------------------------- 布局：左对话 / 右状态
left, right = st.columns([2.3, 1], gap="large")

with right:
    # 这个空锚点只干一件事：让上面的 CSS 能精确认出"装面板的那一列"。
    # 它不显示任何内容（高 0），删了面板就不再固定。
    st.markdown("<div class='status-anchor'></div>", unsafe_allow_html=True)

    st.markdown("#### 当前活动")
    st.markdown(
        f"**{act.get('session_label', '—')}**　{act.get('title', '')}"
    )
    st.markdown(
        f"<span class='tiny'>{act.get('date', '')} {act.get('weekday', '')}"
        f"　{act.get('time_range', '')}</span>",
        unsafe_allow_html=True,
    )

    if missing:
        st.warning(f"还缺 **{len(missing)}** 项")
    else:
        st.success("信息齐全")

    # 必填项清单：客服最关心这一块，所以放最上面、不折叠
    rows = []
    for f, name in store.REQUIRED_FOR_COPY:
        v = act.get(f)
        filled = v not in (None, "", [], {})
        rows.append({"": "✓" if filled else "✗", "字段": name, "值": _fmt(v)})
    st.dataframe(rows, hide_index=True, height=min(35 * len(rows) + 40, 380))

    # 其他字段折叠起来，需要时再看
    others = [f for f in store.ALLOWED_FIELDS if f not in required]
    with st.expander(f"其他字段（{len(others)} 项）"):
        rows2 = []
        for f in others:
            v = act.get(f)
            rows2.append({"": "✓" if v not in (None, "", [], {}) else "·",
                          "字段": _label(f), "值": _fmt(v)})
        st.dataframe(rows2, hide_index=True, height=300)

    st.divider()
    if st.button("📋 显示文案发布流程", width="stretch",
                 help="只列「什么时候发什么、哪里容易漏」，不生成正文；要具体文案在左边对话框要"):
        st.session_state.flow_text = agent.show_publish_flow()
        chatlog.append("（点了「显示文案发布流程」）",
                       {"answer": st.session_state.flow_text,
                        "trace": ["button: 文案发布流程"]})

    if st.session_state.flow_text:
        st.markdown(st.session_state.flow_text)


with left:
    st.markdown("#### 对话")
    st.markdown(
        "<span class='tiny'>家长问什么直接打进来；要补信息也直接说"
        "（「室外」「名额改成30人」「集合点改到五教104」）；要文案就说要哪种。</span>",
        unsafe_allow_html=True,
    )

    if not st.session_state.msgs:
        st.info("还没有对话。下面是几条示例：\n\n"
                "· 集合地点在哪里？\n"
                "· 需要带身份证吗\n"
                "· 名额改成30人\n"
                "· 写个私域群推广文案")

    for m in st.session_state.msgs:
        with st.chat_message(m["role"], avatar="👤" if m["role"] == "user" else "🌿"):
            # 文案类结果用代码块显示，自带复制按钮
            if m.get("code"):
                st.code(m["content"], language=None)
            else:
                st.markdown(m["content"])
            if m.get("wrote"):
                st.caption("✎ 已写入：" + "、".join(m["wrote"]))
            if m.get("replaced"):
                st.warning(
                    f"这次是**整条替换**成新一期：上一期已归档，"
                    f"推文没写到的 {m.get('dropped', 0)} 项旧字段已清空。\n\n"
                    f"清空是故意的，不是出错 —— 那些值属于上一期，留着会让客服答错期。"
                    f"还缺什么看右边面板。")
            for e in m.get("errors") or []:
                st.error("没写进去：" + e)
            if m.get("notes"):
                with st.expander("客服提示（不发给家长）"):
                    for n in m["notes"]:
                        st.markdown(f"· {n}")

    # ---- 输入区：放在对话这一栏里，不再用全宽的 st.chat_input ----
    # 原来 chat_input 固定在页面底部、横跨整个宽度，右栏底部会被它盖住
    # （「清空对话」看不见就是这个原因），而且输入框跟对话不在一个区域，
    # 看着不像「在这一栏里说话」。现在改成左栏内的表单。
    st.divider()
    with st.form("ask_form", clear_on_submit=True, border=False):
        _in1, _in2 = st.columns([6, 1])
        prompt_in = _in1.text_input(
            "输入", label_visibility="collapsed", key="ask_text",
            placeholder="说人话就行：问问题，或直接补信息（Enter 也能发）")
        submitted = _in2.form_submit_button("发送", use_container_width=True)

    _t1, _t2, _t3 = st.columns([1.0, 1.2, 2.0])
    if _t1.button("清空对话", key="clear_chat", use_container_width=True):
        st.session_state.msgs = []
        st.rerun()
    _t2.markdown("<span class='tiny'>本次会话 "
                 f"{st.session_state.tokens} tokens</span>", unsafe_allow_html=True)
    want_notes = _t3.checkbox("回复后附「客服提示」（不发给家长）",
                              value=True, key="want_notes")


# ---------------------------------------------------------------- 处理提交
if submitted and prompt_in.strip():
    prompt = prompt_in.strip()
    hist = [{"role": m["role"], "content": m["content"]}
            for m in st.session_state.msgs[-6:] if m.get("content")]
    st.session_state.msgs.append({"role": "user", "content": prompt})

    with left:
        with st.chat_message("user", avatar="👤"):
            st.markdown(prompt)

        with st.chat_message("assistant", avatar="🌿"):
            ph = st.empty()
            buf = []

            def on_token(piece: str) -> None:
                buf.append(piece)
                ph.markdown("".join(buf) + "▌")

            try:
                r = agent.answer_chat(prompt, history=hist, stream_cb=on_token)
            except Exception as e:
                ph.empty()
                st.error(f"出错了：{e}")
                st.stop()

            is_copy = (r.get("trace") or [""])[-1] in (
                "render_group_messages", "write_promo")
            if is_copy:
                ph.empty()
                st.code(r["answer"], language=None)
            else:
                ph.markdown(r["answer"])

            if r.get("wrote"):
                st.caption("✎ 已写入：" + "、".join(r["wrote"]))
            if r.get("replaced"):
                st.warning(
                    f"这次是**整条替换**成新一期：上一期已归档，"
                    f"推文没写到的 {len(r.get('dropped') or [])} 项旧字段已清空。\n\n"
                    f"清空是故意的，不是出错 —— 那些值属于上一期，留着会让客服答错期。"
                    f"还缺什么看右边面板。")
            for e in r.get("write_errors") or []:
                st.error("没写进去：" + e)

            notes = []
            if want_notes:
                with st.spinner("生成客服提示…"):
                    notes = agent.internal_notes(
                        prompt, r["answer"],
                        [m["label"] for m in r.get("missing", [])],
                    )
                if notes:
                    with st.expander("客服提示（不发给家长）", expanded=True):
                        for n in notes:
                            st.markdown(f"· {n}")

    chatlog.append(prompt, r)
    st.session_state.tokens += r.get("usage", {}).get("total_tokens", 0)
    st.session_state.msgs.append({
        "role": "assistant",
        "content": r["answer"],
        "code": is_copy,
        "wrote": r.get("wrote", []),
        "replaced": bool(r.get("replaced")),
        "dropped": len(r.get("dropped") or []),
        "errors": r.get("write_errors", []),
        "notes": notes,
    })
    # 重跑一次，让右边状态面板跟着刷新（写库之后缺口会变）
    st.rerun()
