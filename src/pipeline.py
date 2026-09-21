"""
流程编排模块（Pipeline）
=================================
作用：把各个模块串成一条完整的流水线：

    采集日志 → 异常检测 → RAG 检索资料 → 大模型诊断 → 存入数据库

为什么要单独一层？
  每个模块各司其职（采集只管采集、检测只管检测），
  pipeline 负责「指挥」它们按顺序干活。以后想加新步骤（比如发通知），
  只改这里就行，不用动其他模块。
"""

from typing import Optional

from . import db
from .collector import collect_all, collect_ci_log
from .config import Config
from .detector import detect
from .llm import diagnose
from .rag import KnowledgeBase


# 知识库全局单例：避免每次诊断都重建向量库（那会很慢）
_kb: Optional[KnowledgeBase] = None


def get_kb(force_rebuild: bool = False) -> KnowledgeBase:
    """
    获取知识库实例（第一次调用时自动建库）。
    force_rebuild=True 用于修改了 knowledge/*.md 之后重建。
    """
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
        # 注意：首次创建时也要把 force_rebuild 传下去，
        # 否则第一次调用 --force 会被忽略，仍然读到旧缓存
        _kb.build(force=force_rebuild)
    elif force_rebuild:
        _kb.build(force=True)
    return _kb


def analyze_text(
    text: str,
    source: str = "docker",
    service: str = "unknown",
    save: bool = True,
) -> Optional[dict]:
    """
    对一段日志文本做完整诊断。这是整个系统最核心的函数。

    流程：
      1. detect()      检测是否包含异常，没异常就返回 None
      2. kb.retrieve() 用异常上下文去知识库检索相关资料（RAG）
      3. diagnose()    把日志 + 资料交给大模型，得到根因和建议
      4. db.create()   把结果存进数据库

    参数：
      text    - 日志全文
      source  - 来源标记：docker / ci / demo / manual
      service - 服务名
      save    - 是否写入数据库

    返回诊断结果字典；如果没检测到异常，返回 None。
    """
    # ---------- 第 1 步：异常检测 ----------
    detection = detect(text)
    if not detection.hit:
        return None

    # ---------- 第 2 步：RAG 检索 ----------
    kb = get_kb()
    references = kb.retrieve(detection.snippet, top_k=Config.TOP_K)

    # ---------- 第 3 步：大模型诊断 ----------
    result = diagnose(
        log=detection.snippet,
        references=references,
        source=source,
        service=service,
    )

    # ---------- 第 4 步：组装并入库 ----------
    record = {
        "source": source,
        "service": service,
        "raw_log": detection.snippet,
        "matched_keyword": detection.keyword,
        "severity": result.get("severity", detection.severity),
        "root_cause": result.get("root_cause", ""),
        "suggestions": result.get("suggestions", []),
        "used_references": references,
        "status": "new",
    }

    if save:
        record["id"] = db.create_diagnosis(record)

    record["confidence"] = result.get("confidence", 0.0)
    record["mode"] = result.get("mode", "unknown")
    return record


def run_scan(containers: Optional[list] = None, prefer_docker: bool = True,
             allow_demo: bool = True) -> dict:
    """
    扫描所有被监控的容器，对发现异常的日志做诊断。

    返回统计：{"checked": 3, "hit": 2, "saved": 2, "items": [...], "note": "..."}
      checked - 检查了几个服务
      hit     - 检测到几个异常
      saved   - 成功入库几条
      note    - 采不到日志时的说明（如实告知，而不是默默用假数据）

    allow_demo=False 表示「绝不用演示样本兜底」——
    服务端接口必须传 False，避免假数据混进真实数据库。
    """
    items = collect_all(containers=containers, prefer_docker=prefer_docker,
                        allow_demo=allow_demo)

    checked = len(items)
    hit = 0
    saved_items = []

    for item in items:
        record = analyze_text(
            text=item.content,
            source=item.source,
            service=item.service,
            save=True,
        )
        if record:
            hit += 1
            saved_items.append(record)

    result = {
        "checked": checked,
        "hit": hit,
        "saved": len(saved_items),
        "items": saved_items,
    }

    # 一条都没采到时如实说明原因，避免调用者误以为「扫描成功、只是没异常」
    if checked == 0:
        result["note"] = (
            "未采集到任何日志：Docker 中没有运行中的被监控容器，"
            "且本次禁止用演示样本兜底。"
            "可执行 ingest --dir <日志目录> 分析本地日志文件，"
            "或先 docker compose up -d 启动被监控容器。"
        )
    return result


def run_ci_scan(log_path=None) -> Optional[dict]:
    """
    扫描 CI/CD 流水线日志。
    用于 GitHub Actions 失败时自动诊断（对应 ai-diagnose.yml）。
    """
    item = collect_ci_log(log_path)
    if not item:
        return None
    return analyze_text(
        text=item.content,
        source=item.source,
        service=item.service,
        save=True,
    )
