"""
单元测试
=================================
作用：用代码自动验证各模块是否正常工作。

为什么要有测试？
  1. 改代码后能立刻发现有没有改坏（CI 里会自动跑）
  2. 提供了可复现的验证手段 —— 别人拿到项目能自己确认功能是否正常
  3. 对你自己来说，这是理解代码逻辑最好的方式

运行方式：
    pytest tests -v

注意：所有测试都用临时数据库（tmp_path），不会污染你真实的 data/doctor.db。
"""

import pytest

from src import db
from src.config import Config
from src.detector import detect
from src.llm import mock_diagnose
from src.pipeline import analyze_text
from src.rag import build_chunks


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """
    每个测试前自动执行：把数据库路径指向临时目录。
    autouse=True 表示所有测试都会自动用这个 fixture。
    """
    monkeypatch.setattr(Config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


# ============================ 数据库 CRUD 测试 ============================

def test_db_create_and_read():
    """测试 C（创建）和 R（读取）"""
    new_id = db.create_diagnosis({
        "service": "demo-app",
        "source": "docker",
        "raw_log": "Connection refused at demo-mysql:3306",
        "severity": "high",
        "root_cause": "数据库未启动",
        "suggestions": ["启动数据库", "检查网络"],
    })
    assert new_id > 0

    record = db.get_diagnosis(new_id)
    assert record is not None
    assert record["service"] == "demo-app"
    assert record["severity"] == "high"
    # suggestions 存进去是 JSON 字符串，读出来应该还原成列表
    assert record["suggestions"] == ["启动数据库", "检查网络"]


def test_db_update():
    """测试 U（更新）"""
    new_id = db.create_diagnosis({
        "service": "demo-nginx", "raw_log": "502 Bad Gateway", "status": "new"
    })
    assert db.get_diagnosis(new_id)["status"] == "new"

    success = db.update_diagnosis(new_id, {"status": "resolved"})
    assert success is True
    assert db.get_diagnosis(new_id)["status"] == "resolved"


def test_db_delete():
    """测试 D（删除）"""
    new_id = db.create_diagnosis({"service": "demo-app", "raw_log": "some error"})
    assert db.get_diagnosis(new_id) is not None

    db.delete_diagnosis(new_id)
    assert db.get_diagnosis(new_id) is None


def test_db_list_and_filter():
    """测试列表查询与筛选"""
    db.create_diagnosis({"service": "demo-app", "raw_log": "error A", "status": "new"})
    db.create_diagnosis({"service": "demo-mysql", "raw_log": "error B", "status": "resolved"})

    assert db.count_diagnoses() == 2
    assert db.count_diagnoses(status="new") == 1
    assert len(db.list_diagnoses(service="demo-app")) == 1
    assert len(db.list_diagnoses(keyword="error B")) == 1


def test_stats():
    """测试统计功能"""
    db.create_diagnosis({"service": "demo-app", "raw_log": "e1", "severity": "high"})
    db.create_diagnosis({"service": "demo-app", "raw_log": "e2", "severity": "low"})

    s = db.stats()
    assert s["total"] == 2
    assert s["by_severity"]["high"] == 1
    assert s["by_service"]["demo-app"] == 2


# ============================ 异常检测测试 ============================

def test_detect_hit():
    """检测到异常关键字时应该命中"""
    d = detect("pymysql.err.OperationalError: (2003, Can't connect to MySQL server)")
    assert d.hit is True
    assert d.severity in ("high", "medium", "low")


def test_detect_miss():
    """正常日志不应该命中"""
    d = detect("Server started successfully on port 5000")
    assert d.hit is False


def test_detect_severity():
    """OOM 应该被判定为高危"""
    d = detect("MemoryError: Out of memory, process killed by OOM killer")
    assert d.hit is True
    assert d.severity == "high"


# ============================ RAG 测试 ============================

def test_build_chunks():
    """知识库应该能切出片段"""
    chunks = build_chunks()
    assert len(chunks) > 0
    # 每个片段都应该有来源、标题和正文
    for c in chunks:
        assert c.source.endswith(".md")
        assert len(c.text) > 0


def test_chunk_titles():
    """切片应该按二级标题正确分组"""
    chunks = build_chunks()
    titles = [c.title for c in chunks]
    # docker-errors.md 里应该有这个小节
    assert any("Connection refused" in t for t in titles)


# ============================ Mock 诊断测试 ============================

def test_mock_diagnose_connection_refused():
    result = mock_diagnose("Connection refused at mysql:3306", "docker", "demo-app")
    assert "root_cause" in result
    assert len(result["suggestions"]) > 0
    assert result["severity"] == "high"


def test_mock_diagnose_unknown():
    """未匹配到规则时应该有兜底结论"""
    result = mock_diagnose("some totally unknown weird thing happened", "docker", "x")
    assert result["root_cause"]
    assert result["confidence"] < 0.5


# ============================ 端到端流程测试 ============================

def test_analyze_text_end_to_end():
    """
    端到端测试：一段含异常的日志，走完 检测→RAG→诊断 全流程。

    这个测试不入库（save=False），只验证能产出结构完整的结果。
    """
    Config.MOCK_MODE = True
    result = analyze_text(
        text="pymysql.err.OperationalError: (2003, \"Can't connect to MySQL server "
             "on 'demo-mysql' ([Errno 111] Connection refused)\")",
        source="docker",
        service="demo-app",
        save=False,
    )
    assert result is not None
    assert result["service"] == "demo-app"
    assert result["matched_keyword"]
    assert result["root_cause"]
    assert isinstance(result["suggestions"], list)
    # RAG 应该检索到了参考资料（这是本项目的关键能力）
    assert isinstance(result["used_references"], list)


def test_analyze_text_no_error():
    """正常日志不应该产生诊断记录"""
    result = analyze_text(
        text="INFO: Server started on port 5000", source="docker",
        service="demo-app", save=False,
    )
    assert result is None
