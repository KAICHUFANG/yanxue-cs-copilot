"""集中管理配置。所有可变的东西都从 .env 读，代码里不出现密钥。"""

import os
import shutil
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

# 先记下系统环境变量里原本有没有 Key，用于启动时提示冲突
_ENV_KEY_BEFORE = os.environ.get("DEEPSEEK_API_KEY", "").strip()

# override=True 是必须的：
#   默认情况下 load_dotenv 不会覆盖「已存在的系统环境变量」。
#   如果 Windows 里残留一个旧的 DEEPSEEK_API_KEY，它会静默压过 .env，
#   导致程序报 401 而你盯着 .env 里的新 key 找不到原因（踩过这个坑）。
load_dotenv(ROOT / ".env", override=True)

# ---- 代码版本 ----
# 跨模块改动（新增/改名函数）时手动加一。界面用它检测「进程还是旧的」：
# Streamlit 只重跑入口脚本，已 import 的模块留在内存里不会重新加载，
# 于是新脚本调用旧模块里不存在的函数，报一个看不懂的
#   AttributeError: module 'knowledge' has no attribute 'load_team_info'
# 这张版本号就是为了把这种情况变成一句能看懂的话。
CODE_VERSION = 7

# ---- 模型相关 ----
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
MODEL = os.getenv("MODEL_NAME", "deepseek-chat").strip()

# ---- 数据 ----
# DATA_DIR 固定指向项目的 data 目录：processes.json / group_messages.json 都在这里。
# DATA_FILE 允许用环境变量覆盖（评测时指向副本），但**只影响活动数据本身**，
# 不能连带把 processes.json 的路径也带跑——这个坑踩过：评测时指向 eval/ 目录，
# 结果找不到 processes.json，模型只能回答「查不到记录」。
DATA_DIR = ROOT / "data"
DATA_FILE = Path(os.getenv("ACTIVITIES_FILE", DATA_DIR / "activities.json"))

# ---- 进校预约的被访人信息 ----
# 这两个值会填进「进校预约提醒」的文案里（{{被访人姓名}} / {{被访人电话}}）。
# 属于个人信息，只写在 .env，不进仓库、不进 data/。
VISIT_TARGET_NAME = os.getenv("VISIT_TARGET_NAME", "").strip()
VISIT_TARGET_PHONE = os.getenv("VISIT_TARGET_PHONE", "").strip()

# 使用记录里记成谁。本地模式下一个人一台电脑，所以在 .env 里写死最省事，
# 不需要在界面上多一个输入框（用户明确要求：记录不要放在面板里）。
CHAT_LOG_OPERATOR = os.getenv("CHAT_LOG_OPERATOR", "").strip()

# ---- 运行参数 ----
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "6"))


# ---------------------------------------------------------------- 界面里改配置
# 为什么要在界面上改：新客服不是技术同学，让她去文本编辑器里改 .env
# （还容易带全角空格、忘了保存）是把门槛放在最不该放的地方。
# 所以提供一个「填 Key → 保存并测试」的入口，写回 .env 这一个来源。
ENV_PATH = ROOT / ".env"


def save_env(updates: Dict[str, str]) -> str:
    """把若干项写回 .env，只改这几行，其余原样保留（写前备份 .env.bak）。

    为什么写回 .env 而不是另存一个设置文件：
        代码读的就是 .env（还有 .env.example 做模板）。
        多一个存储位置，就多一处「界面里改了但程序读不到」的可能。
    """
    if ENV_PATH.exists():
        shutil.copy2(ENV_PATH, ROOT / ".env.bak")   # .gitignore 已忽略 .env.*

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    clean = {k: str(v).strip() for k, v in updates.items() if str(v).strip()}
    seen = set()
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip()
        if key in clean:
            out.append(f"{key}={clean[key]}")
            seen.add(key)
        else:
            out.append(ln)
    for k, v in clean.items():
        if k not in seen:
            out.append(f"{k}={v}")

    ENV_PATH.write_text(chr(10).join(out) + chr(10), encoding="utf-8", newline=chr(10))
    return str(ENV_PATH)


def reload() -> None:
    """重新从 .env 读进内存。

    必须调：模块级的 API_KEY / MODEL 是启动时读的一次快照，
    只改文件不重读的话，界面显示「已保存」但程序still 用旧值（这个坑很隐蔽）。
    """
    global API_KEY, BASE_URL, MODEL, VISIT_TARGET_NAME, VISIT_TARGET_PHONE
    global CHAT_LOG_OPERATOR
    global TEMPERATURE, MAX_TOOL_ROUNDS
    load_dotenv(ENV_PATH, override=True)
    API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
    BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    MODEL = os.getenv("MODEL_NAME", "deepseek-chat").strip()
    VISIT_TARGET_NAME = os.getenv("VISIT_TARGET_NAME", "").strip()
    VISIT_TARGET_PHONE = os.getenv("VISIT_TARGET_PHONE", "").strip()
    CHAT_LOG_OPERATOR = os.getenv("CHAT_LOG_OPERATOR", "").strip()
    TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))
    MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "6"))


def mask(key: str) -> str:
    """把 key 打码，只露出头尾，方便核对是哪一个、又不泄露全文。"""
    if not key:
        return "(未配置)"
    if len(key) <= 12:
        return key[:4] + "****"
    return key[:8] + "****" + key[-4:]


def describe() -> str:
    """启动时打印的配置摘要，一眼看出用的是哪个 key、哪个模型。"""
    lines = [
        f"模型      : {MODEL}",
        f"接口地址  : {BASE_URL}",
        f"使用的 Key: {mask(API_KEY)}（来自 .env）",
        f"知识库    : {DATA_FILE}",
        f"进校预约被访人: {VISIT_TARGET_NAME or '(未配置)'} / "
        f"{(VISIT_TARGET_PHONE[:3] + '****' + VISIT_TARGET_PHONE[-2:]) if len(VISIT_TARGET_PHONE) > 5 else (VISIT_TARGET_PHONE or '(未配置)')}",
    ]
    if _ENV_KEY_BEFORE and _ENV_KEY_BEFORE != API_KEY:
        lines.append(
            f"⚠ 注意：系统环境变量里还有一个不同的 Key（{mask(_ENV_KEY_BEFORE)}），"
            f"已被 .env 覆盖。若想彻底避免混淆，建议删掉那个系统环境变量。"
        )
    return "\n".join(lines)


def check() -> None:
    """启动自检：缺东西就直接报清楚，不要等到调用 API 时才炸。"""
    if not API_KEY:
        raise SystemExit(
            "❌ 未找到 DEEPSEEK_API_KEY\n"
            f"   请检查文件: {ROOT / '.env'}\n"
            "   内容应形如: DEEPSEEK_API_KEY=sk-xxxxxxxx"
        )
    if not DATA_FILE.exists():
        raise SystemExit(f"❌ 知识库文件不存在: {DATA_FILE}")
