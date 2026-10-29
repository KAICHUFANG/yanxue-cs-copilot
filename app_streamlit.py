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

FONT_CSS = """
<style>
  .block-container { padding-top: 2rem; padding-bottom: 6rem; }
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
       ② 面板必须有不透明底色（否则消息会从后面穿过去），
          而 Streamlit 没有暴露主题背景变量，所以用 prefers-color-scheme 兜底。

     窄屏（<1000px）不启用：流式布局本来就把两列上下堆叠，钉住反而遮挡对话。---- */
  :root { --panel-bg: #ffffff; --panel-line: rgba(0, 0, 0, 0.12); }
  @media (prefers-color-scheme: dark) {
    :root { --panel-bg: #0e1117; --panel-line: rgba(255, 255, 255, 0.16); }
  }

  @media (min-width: 1000px) {
    div[data-testid="stColumn"]:has(.status-anchor) {
        position: fixed;
        top: 3.6rem;
        right: 1rem;
        width: 22rem;
        flex: none;
        max-height: calc(100vh - 9rem);
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
st.markdown(FONT_CSS, unsafe_allow_html=True)


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
if _err:
    st.error(_err)
    st.stop()


# ---------------------------------------------------------------- 会话状态
if "msgs" not in st.session_state:
    st.session_state.msgs = []          # [{role, content, wrote, errors, notes}]
if "tokens" not in st.session_state:
    st.session_state.tokens = 0
if "copy_text" not in st.session_state:
    st.session_state.copy_text = ""

act = knowledge.get_activity() or {}
missing = store.missing_fields(act)
required = {f for f, _ in store.REQUIRED_FOR_COPY}


def _label(field: str) -> str:
    from app_workbench import DISPLAY_NAME
    return DISPLAY_NAME.get(field, field)


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
    if st.button("生成全部群文案", width="stretch",
                 help="5 个阶段一起生成，走代码填模板，不会改写原文"):
        st.session_state.copy_text = agent.render_copy("")

    if st.session_state.copy_text:
        st.caption("点右上角图标复制")
        st.code(st.session_state.copy_text, language=None)

    st.divider()
    st.markdown("<span class='tiny'>本次会话 "
                f"{st.session_state.tokens} tokens</span>", unsafe_allow_html=True)
    if st.button("清空对话", width="stretch"):
        st.session_state.msgs = []
        st.rerun()


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


# ---------------------------------------------------------------- 输入
with st.expander("发送时一并生成客服提示", expanded=False):
    want_notes = st.checkbox(
        "同时生成「该注意什么 / 该去找谁确认」的内部提示（不发给家长）",
        value=True,
    )

prompt = st.chat_input("说人话就行：问问题，或直接补信息")

if prompt:
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
