"""
AI 容器日志医生（AI Docker Log Doctor）
======================================
自动采集 Docker 容器 / CI 流水线日志，检测异常，
用 RAG（向量检索）从运维知识库找参考资料，
再交给大模型生成根因分析和处置建议，最后展示到 Web 面板。
"""

__version__ = "1.0.0"
__author__ = "your-name"

__all__ = ["config", "db", "collector", "detector", "rag", "llm", "pipeline", "api"]
