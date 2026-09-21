"""
数据库层（SQLite）
=================================
作用：负责「诊断记录」的增删改查（CRUD）。

为什么用 SQLite？
  - Python 自带，不需要安装数据库软件
  - 数据就是一个文件（data/doctor.db），方便备份和查看
  - 语法和 MySQL 几乎一样，学会了以后迁移很容易

CRUD 是哪四个操作？
  C = Create  创建（INSERT）
  R = Read    读取（SELECT）
  U = Update  更新（UPDATE）
  D = Delete  删除（DELETE）

表结构（diagnoses 诊断记录表）：
  id              主键，自增
  created_at      发现时间
  source          来源：docker（容器日志）或 ci（流水线日志）
  service         哪个服务出的问题，比如 demo-app
  raw_log         原始日志内容
  matched_keyword 命中的异常关键字
  severity        严重程度：low / medium / high
  root_cause      AI 分析的根因
  suggestions     AI 给的建议（JSON 字符串数组）
  used_references RAG 检索到的参考来源（JSON 字符串数组）
  status          处理状态：new（未处理）/ resolved（已解决）/ ignored（已忽略）
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional

from .config import Config

# 建表 SQL（IF NOT EXISTS 表示表已存在就不重复创建）
SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnoses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'docker',
    service         TEXT NOT NULL DEFAULT 'unknown',
    raw_log         TEXT NOT NULL,
    matched_keyword TEXT,
    severity        TEXT NOT NULL DEFAULT 'medium',
    root_cause      TEXT,
    suggestions     TEXT,
    used_references TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
);

CREATE INDEX IF NOT EXISTS idx_diagnoses_created ON diagnoses(created_at);
CREATE INDEX IF NOT EXISTS idx_diagnoses_status  ON diagnoses(status);
"""


def get_conn() -> sqlite3.Connection:
    """
    获取数据库连接。
    row_factory = sqlite3.Row 的作用是：让查询结果可以像字典一样用列名取值，
    例如 row["service"]，而不是 row[3] 这种看不懂的下标。
    """
    Config.ensure_dirs()
    conn = sqlite3.connect(str(Config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库：建库 + 建表。项目启动时会调用一次。"""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    """返回当前时间字符串，格式 2026-09-15 01:20:33"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    """把数据库行转成字典，并把 JSON 字符串字段还原成 Python 列表"""
    d = dict(row)
    for field in ("suggestions", "used_references"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = json.loads(raw)
            except json.JSONDecodeError:
                d[field] = []
        else:
            d[field] = []
    return d


# ============================ C = Create ============================

def create_diagnosis(data: dict) -> int:
    """
    新增一条诊断记录（Create）。
    data 里可以包含：source / service / raw_log / matched_keyword /
                    severity / root_cause / suggestions / used_references / status
    返回新记录的 id。
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO diagnoses
                (created_at, source, service, raw_log, matched_keyword,
                 severity, root_cause, suggestions, used_references, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("created_at") or _now(),
                data.get("source", "docker"),
                data.get("service", "unknown"),
                data.get("raw_log", ""),
                data.get("matched_keyword"),
                data.get("severity", "medium"),
                data.get("root_cause"),
                json.dumps(data.get("suggestions", []), ensure_ascii=False),
                json.dumps(data.get("used_references", []), ensure_ascii=False),
                data.get("status", "new"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ============================ R = Read ============================

def get_diagnosis(did: int) -> Optional[dict]:
    """按 id 查询单条记录（Read），查不到返回 None"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM diagnoses WHERE id = ?", (did,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_diagnoses(
    limit: int = 50,
    offset: int = 0,
    service: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    keyword: Optional[str] = None,
) -> list:
    """
    分页 + 条件查询列表（Read）。
    - limit / offset：分页，limit 每页几条，offset 跳过几条
    - service / status / severity：精确筛选
    - keyword：在原始日志里模糊搜索
    """
    sql = "SELECT * FROM diagnoses WHERE 1=1"
    params = []

    if service:
        sql += " AND service = ?"
        params.append(service)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if keyword:
        sql += " AND raw_log LIKE ?"
        params.append(f"%{keyword}%")

    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def count_diagnoses(status: Optional[str] = None) -> int:
    """统计总条数（用于前端显示「共 N 条」）"""
    conn = get_conn()
    try:
        if status:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM diagnoses WHERE status = ?", (status,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM diagnoses").fetchone()
        return row["c"]
    finally:
        conn.close()


# ============================ U = Update ============================

def update_diagnosis(did: int, data: dict) -> bool:
    """
    更新一条记录（Update）。
    只更新传入的字段（比如只想改状态 status，就只传 {"status": "resolved"}）。
    返回 True 表示确实更新了，False 表示没找到这条记录。
    """
    allowed = [
        "source", "service", "raw_log", "matched_keyword", "severity",
        "root_cause", "suggestions", "used_references", "status",
    ]
    fields, params = [], []
    for key in allowed:
        if key in data and data[key] is not None:
            if key in ("suggestions", "used_references"):
                params.append(json.dumps(data[key], ensure_ascii=False))
            else:
                params.append(data[key])
            fields.append(f"{key} = ?")

    if not fields:
        return False

    params.append(did)
    sql = f"UPDATE diagnoses SET {', '.join(fields)} WHERE id = ?"

    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ============================ D = Delete ============================

def delete_diagnosis(did: int) -> bool:
    """删除一条记录（Delete）。返回 True 表示删除成功。"""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM diagnoses WHERE id = ?", (did,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_diagnoses() -> int:
    """清空所有记录（演示/重置用）。返回删除的条数。"""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM diagnoses")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ============================ 统计（给前端看板用）============================

def stats() -> dict:
    """返回统计数据：总数、按状态分组、按服务分组、按严重程度分组"""
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM diagnoses").fetchone()["c"]

        by_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM diagnoses GROUP BY status"
            )
        }
        by_service = {
            r["service"]: r["c"]
            for r in conn.execute(
                "SELECT service, COUNT(*) AS c FROM diagnoses GROUP BY service"
            )
        }
        by_severity = {
            r["severity"]: r["c"]
            for r in conn.execute(
                "SELECT severity, COUNT(*) AS c FROM diagnoses GROUP BY severity"
            )
        }
        return {
            "total": total,
            "by_status": by_status,
            "by_service": by_service,
            "by_severity": by_severity,
        }
    finally:
        conn.close()
