"""
配置模块
=================================
作用：集中管理项目的所有配置项，从 .env 文件 / 环境变量里读取。

为什么要有这个文件？
  如果把 API 密钥、路径这些写死在各个代码文件里，改一次要翻遍所有文件。
  集中到这里后，你只需要改 .env 一个文件。

你需要理解的概念：
  - os.getenv("名字", "默认值")：读环境变量，读不到就用默认值
  - Path：路径对象，比字符串拼接更安全可靠（Windows / Linux 都能用）
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# BASE_DIR = 项目根目录（ai-log-doctor 这一层）
# __file__ 是当前文件路径，.parent 是 src，.parent.parent 是项目根
BASE_DIR = Path(__file__).resolve().parent.parent

# 加载 .env 文件里的配置（如果文件不存在也不报错）
load_dotenv(BASE_DIR / ".env")


def _get_bool(name: str, default: bool = False) -> bool:
    """把环境变量里的字符串 'true'/'1'/'yes' 转成 Python 的 True/False"""
    value = os.getenv(name, str(default)).strip().lower()
    return value in ("1", "true", "yes", "y", "on")


# 先读取大模型密钥，方便后面判断是否启用 Mock 模式
_LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
_EMBED_API_KEY = os.getenv("EMBED_API_KEY", "").strip() or _LLM_API_KEY


class Config:
    """全局配置类：所有配置都是类属性，用 Config.XXX 访问"""

    # ---------- 路径配置 ----------
    BASE_DIR = BASE_DIR
    DATA_DIR = BASE_DIR / "data"              # 存放 SQLite 数据库
    KNOWLEDGE_DIR = BASE_DIR / "knowledge"    # 存放 RAG 知识库 Markdown
    SAMPLES_DIR = BASE_DIR / "samples"        # 存放示例日志（CI 日志模拟用）
    WEB_DIR = BASE_DIR / "web"                # 前端页面
    DB_PATH = DATA_DIR / "doctor.db"          # SQLite 数据库文件

    # ---------- 大模型配置（OpenAI 兼容格式）----------
    # DeepSeek / 通义千问 / OpenAI / 月之暗面 都支持这种格式
    LLM_API_KEY = _LLM_API_KEY
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip()
    LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat").strip()

    # ---------- Embedding（向量化）配置 ----------
    EMBED_API_KEY = _EMBED_API_KEY
    EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "").strip() or LLM_BASE_URL
    EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-v3").strip()

    # ---------- 运行模式 ----------
    # Mock 模式：没有 API Key 时用规则生成假诊断结果，保证流程能跑通
    MOCK_MODE = _get_bool("MOCK_MODE", False) or (not _LLM_API_KEY)

    # 向量库后端：chroma（真实向量库）或 simple（纯 Python 内存版，零依赖）
    VECTOR_BACKEND = os.getenv("VECTOR_BACKEND", "auto").strip().lower()
    # 向量数据持久化目录（chroma 用）
    CHROMA_DIR = str(DATA_DIR / "chroma")
    CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "ops_knowledge")

    # ---------- 日志采集配置 ----------
    # 要监控的容器名（对应 docker-compose.yml 里的服务名）
    WATCH_CONTAINERS = [
        c.strip()
        for c in os.getenv(
            "WATCH_CONTAINERS", "demo-nginx,demo-app,demo-mysql"
        ).split(",")
        if c.strip()
    ]
    # 每次采集多少行日志
    LOG_TAIL = int(os.getenv("LOG_TAIL", "100"))

    # ---------- 异常检测关键字 ----------
    ERROR_KEYWORDS = [
        "ERROR", "Error", "error",
        "Exception", "Traceback",
        "FAILED", "Failed", "failed",
        "Connection refused", "connection refused",
        "Timeout", "timeout", "timed out",
        "502", "503", "504",
        "Out of memory", "OOM",
        "No such file", "Permission denied",
        "崩溃", "连接失败", "超时", "拒绝连接",
    ]

    # ---------- RAG 配置 ----------
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "350"))      # 每个知识片段的字数
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50")) # 片段之间重叠字数
    TOP_K = int(os.getenv("TOP_K", "3"))                  # 检索返回几条参考资料

    # ---------- API 服务配置 ----------
    API_HOST = os.getenv("API_HOST", "127.0.0.1")
    API_PORT = int(os.getenv("API_PORT", "8000"))

    @classmethod
    def ensure_dirs(cls):
        """确保数据目录存在（不存在就创建）"""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        return cls.DATA_DIR

    @classmethod
    def summary(cls) -> dict:
        """返回一份配置摘要，用于启动时打印检查（密钥会打码）"""
        def mask(key: str) -> str:
            if not key:
                return "(未设置)"
            return key[:6] + "****" + key[-4:] if len(key) > 12 else "****"

        return {
            "项目根目录": str(cls.BASE_DIR),
            "数据库路径": str(cls.DB_PATH),
            "知识库目录": str(cls.KNOWLEDGE_DIR),
            "运行模式": "Mock 模式（无需 API Key）" if cls.MOCK_MODE else "真实大模型模式",
            "大模型地址": cls.LLM_BASE_URL,
            "大模型模型": cls.LLM_MODEL,
            "API Key": mask(cls.LLM_API_KEY),
            "向量库后端": cls.VECTOR_BACKEND,
            "监控容器": ", ".join(cls.WATCH_CONTAINERS),
        }
