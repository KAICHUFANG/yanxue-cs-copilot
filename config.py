"""集中管理配置。所有可变的东西都从 .env 读，代码里不出现密钥。"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

# 先记下系统环境变量里原本有没有 Key，用于启动时提示冲突
_ENV_KEY_BEFORE = os.environ.get("DEEPSEEK_API_KEY", "").strip()

# override=True 是必须的：
#   默认情况下 load_dotenv 不会覆盖「已存在的系统环境变量」。
#   如果 Windows 里残留一个旧的 DEEPSEEK_API_KEY，它会静默压过 .env，
#   导致程序报 401 而你盯着 .env 里的新 key 找不到原因（踩过这个坑）。
load_dotenv(ROOT / ".env", override=True)

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

# ---- 运行参数 ----
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
