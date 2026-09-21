"""
FastAPI 接口层
=================================
作用：对外提供 HTTP 接口，让前端页面和命令行都能操作系统。

FastAPI 是什么？
  一个用 Python 写 Web 接口的框架。它有三大好处：
  1. 自动生成交互式 API 文档（访问 /docs 就能看到并直接测试）
  2. 用 Pydantic 自动校验参数，传错了会明确告诉你哪里错
  3. 基于类型提示，代码写起来很简洁

本文件提供的接口分组：
  【页面】      GET  /                     前端诊断面板
  【健康检查】  GET  /api/health
  【配置查看】  GET  /api/config
  【统计】      GET  /api/stats
  【知识库】    GET  /api/kb/info          查看向量库状态
               POST /api/kb/rebuild       重建向量库
               GET  /api/kb/search        RAG 检索测试（重点！能直观看到检索效果）
  【CRUD】      POST   /api/diagnoses      Create：提交日志 → 诊断 → 入库
               GET    /api/diagnoses      Read：分页列表（支持筛选）
               GET    /api/diagnoses/{id} Read：单条详情
               PUT    /api/diagnoses/{id} Update：改状态/严重程度
               DELETE /api/diagnoses/{id} Delete：删除
  【动作】      POST /api/scan             立即扫描容器日志
               POST /api/seed             重新生成演示数据
               DELETE /api/diagnoses      清空所有记录
"""

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .config import Config
from .pipeline import analyze_text, get_kb, run_scan
from .seed import generate_history


# ============================ 启动/关闭钩子 ============================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理。
    yield 之前的代码在「启动时」执行一次（建表、建知识库）
    yield 之后的代码在「关闭时」执行
    """
    db.init_db()
    print("=" * 60)
    print("AI 容器日志医生 启动中...")
    for key, value in Config.summary().items():
        print(f"  {key}: {value}")
    print("=" * 60)
    print(f"接口文档（可在此测试 CRUD）： http://{Config.API_HOST}:{Config.API_PORT}/docs")
    print(f"前端面板：                   http://{Config.API_HOST}:{Config.API_PORT}/")
    yield
    print("服务已停止")


app = FastAPI(
    title="AI 容器日志医生",
    description="自动采集 Docker / CI 日志，用 RAG + 大模型做根因分析",
    version="1.0.0",
    lifespan=lifespan,
)

# 允许跨域：方便前端页面（哪怕用别的端口打开）也能调接口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================ 请求 / 响应模型 ============================
# Pydantic 模型：FastAPI 会用它自动校验请求参数，并在 /docs 里生成示例

class DiagnosisCreate(BaseModel):
    """提交日志触发诊断（Create）"""
    raw_log: str = Field(..., description="日志内容", min_length=1)
    source: str = Field("manual", description="来源：docker / ci / manual")
    service: str = Field("unknown", description="服务名")

    model_config = {
        "json_schema_extra": {
            "example": {
                "raw_log": "pymysql.err.OperationalError: (2003, \"Can't connect to MySQL server on 'demo-mysql' ([Errno 111] Connection refused)\")",
                "source": "manual",
                "service": "demo-app",
            }
        }
    }


class DiagnosisUpdate(BaseModel):
    """更新记录（Update），所有字段都可选，传什么改什么"""
    status: Optional[str] = Field(None, description="new / resolved / ignored")
    severity: Optional[str] = Field(None, description="high / medium / low")
    root_cause: Optional[str] = Field(None, description="根因描述")
    suggestions: Optional[List[str]] = Field(None, description="处置建议列表")

    model_config = {
        "json_schema_extra": {
            "example": {"status": "resolved", "severity": "high"}
        }
    }


VALID_STATUS = {"new", "resolved", "ignored"}
VALID_SEVERITY = {"high", "medium", "low"}


def ok(data=None, message: str = "success"):
    """统一成功响应格式"""
    return {"code": 0, "message": message, "data": data}


# ============================ 页面 & 健康检查 ============================

@app.get("/", include_in_schema=False)
def index():
    """返回前端面板页面"""
    index_file = Config.WEB_DIR / "index.html"
    if not index_file.exists():
        return {"message": "前端页面未找到，请确认 web/index.html 存在"}
    return FileResponse(str(index_file), media_type="text/html")


@app.get("/api/health", tags=["系统"])
def health():
    """健康检查：确认服务活着（容器健康检查探针会调它）"""
    return ok({
        "status": "healthy",
        "mock_mode": Config.MOCK_MODE,
        "db": str(Config.DB_PATH),
    })


@app.get("/api/config", tags=["系统"])
def get_config():
    """查看当前配置（密钥会打码），排查配置问题时很有用"""
    return ok(Config.summary())


@app.get("/api/stats", tags=["系统"])
def get_stats():
    """统计数据：总数、按状态/服务/严重程度分组"""
    return ok(db.stats())


# ============================ 知识库（RAG）============================

@app.get("/api/kb/info", tags=["知识库"])
def kb_info():
    """查看向量库状态：用的什么后端、多少条片段"""
    kb = get_kb()
    return ok(kb.info())


@app.post("/api/kb/rebuild", tags=["知识库"])
def kb_rebuild():
    """重建知识库。修改了 knowledge/*.md 之后调用它"""
    kb = get_kb(force_rebuild=True)
    return ok(kb.info(), "知识库已重建")


@app.get("/api/kb/search", tags=["知识库"])
def kb_search(
    q: str = Query(..., description="检索关键词，例如 Connection refused"),
    top_k: int = Query(3, ge=1, le=10, description="返回几条"),
):
    """
    RAG 检索测试接口（强烈建议试试）
    输入一段错误描述，看看系统能从知识库里检索到哪些资料、相似度多少。
    这能帮你直观理解 RAG 到底做了什么。
    """
    kb = get_kb()
    results = kb.retrieve(q, top_k=top_k)
    return ok({"query": q, "count": len(results), "results": results})


# ============================ CRUD：C = Create ============================

@app.post("/api/diagnoses", tags=["诊断记录"])
def create_diagnosis(payload: DiagnosisCreate):
    """
    提交一段日志，系统自动完成：异常检测 → RAG 检索 → AI 诊断 → 入库。

    这是最常用的接口。在 /docs 里点 Try it out 就能体验完整流程。
    如果日志里不含异常关键字，会返回 400 提示。
    """
    record = analyze_text(
        text=payload.raw_log,
        source=payload.source,
        service=payload.service,
        save=True,
    )
    if not record:
        raise HTTPException(
            status_code=400,
            detail="未检测到异常关键字，未生成诊断记录。"
                   f"当前关键字列表：{Config.ERROR_KEYWORDS[:8]} ...",
        )
    return ok(record, "诊断完成")


# ============================ CRUD：R = Read ============================

@app.get("/api/diagnoses", tags=["诊断记录"])
def list_diagnoses(
    limit: int = Query(20, ge=1, le=200, description="每页条数"),
    offset: int = Query(0, ge=0, description="跳过几条（分页用）"),
    service: Optional[str] = Query(None, description="按服务名筛选"),
    status: Optional[str] = Query(None, description="按状态筛选：new/resolved/ignored"),
    severity: Optional[str] = Query(None, description="按严重程度筛选：high/medium/low"),
    keyword: Optional[str] = Query(None, description="在日志内容里模糊搜索"),
):
    """分页查询诊断记录，支持多种筛选"""
    items = db.list_diagnoses(
        limit=limit, offset=offset,
        service=service, status=status,
        severity=severity, keyword=keyword,
    )
    return ok({
        "total": db.count_diagnoses(),
        "count": len(items),
        "items": items,
    })


@app.get("/api/diagnoses/{did}", tags=["诊断记录"])
def get_diagnosis(did: int):
    """按 ID 查询单条记录详情"""
    record = db.get_diagnosis(did)
    if not record:
        raise HTTPException(status_code=404, detail=f"记录 {did} 不存在")
    return ok(record)


# ============================ CRUD：U = Update ============================

@app.put("/api/diagnoses/{did}", tags=["诊断记录"])
def update_diagnosis(did: int, payload: DiagnosisUpdate):
    """更新记录。常用场景：把状态从 new 改成 resolved（标记已解决）"""
    data = payload.model_dump(exclude_none=True)

    if "status" in data and data["status"] not in VALID_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"status 只能是 {sorted(VALID_STATUS)} 之一",
        )
    if "severity" in data and data["severity"] not in VALID_SEVERITY:
        raise HTTPException(
            status_code=400,
            detail=f"severity 只能是 {sorted(VALID_SEVERITY)} 之一",
        )

    success = db.update_diagnosis(did, data)
    if not success:
        raise HTTPException(status_code=404, detail=f"记录 {did} 不存在或无字段更新")
    return ok(db.get_diagnosis(did), "更新成功")


# ============================ CRUD：D = Delete ============================

@app.delete("/api/diagnoses/{did}", tags=["诊断记录"])
def delete_diagnosis(did: int):
    """删除单条记录"""
    if not db.delete_diagnosis(did):
        raise HTTPException(status_code=404, detail=f"记录 {did} 不存在")
    return ok(None, f"记录 {did} 已删除")


@app.delete("/api/diagnoses", tags=["诊断记录"])
def clear_all():
    """清空所有记录（演示重置用，生产环境慎用）"""
    n = db.clear_diagnoses()
    return ok({"deleted": n}, f"已清空 {n} 条记录")


# ============================ 动作类接口 ============================

@app.post("/api/scan", tags=["动作"])
def scan_now(prefer_docker: bool = True):
    """
    立即扫描一次所有被监控的容器。

    ⚠️ 这里刻意**不使用演示样本兜底**（allow_demo=False）。

    原因：服务端一旦用假数据兜底，就会把编造的日志写进真实数据库，
    而前端面板上看起来一切正常 —— 这种「静默失败」比直接报错危险得多。

    想体验演示数据，请用本地命令 `python -m src.main scan`，
    或调用 /api/seed 显式生成（那样来源明确标注为 demo）。
    """
    result = run_scan(prefer_docker=prefer_docker, allow_demo=False)
    if result["checked"] == 0:
        return ok(result, "未采集到日志（Docker 中没有被监控的容器），已跳过，未写入任何数据")
    return ok(result, f"扫描完成：检查 {result['checked']} 个服务，发现 {result['hit']} 个异常")


@app.post("/api/seed", tags=["动作"])
def seed_data(rounds: int = 3, clear: bool = True):
    """
    生成演示数据。

    ⚠️ **这是破坏性操作**：clear=True（默认）会先清空整张 diagnoses 表，
    从真实日志分析出来的记录也会被一起删掉，且无法恢复。

    曾经踩过的坑：前端按钮没做二次确认，误点一次就丢掉了 33 条真实分析记录。
    所以现在前端必须先弹确认框，这里也会在返回值里如实报告清空了多少条。
    """
    before = db.stats()["total"]
    n = generate_history(rounds=rounds, clear=clear)

    msg = f"已生成 {n} 条演示记录"
    if clear and before:
        msg += f"（已清空原有 {before} 条记录，无法恢复）"

    return ok({
        "generated": n,
        "cleared": before if clear else 0,
        "stats": db.stats(),
    }, msg)
