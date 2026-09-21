"""
演示用故障应用（被监控对象）
=================================
这是一个「故意制造故障」的 Flask 应用，作用是产生真实的容器日志，
好让我们的诊断系统有东西可分析。

为什么需要它？
  如果容器一直正常运行，日志里就没有错误，诊断系统无事可做。
  这个应用提供了几个「一键制造故障」的接口，方便你随时演示。

接口一览：
  GET /health   健康检查（始终正常，给探针用）
  GET /         首页
  GET /db       模拟数据库连接失败 → 产生 Connection refused
  GET /boom     模拟代码异常     → 产生 Traceback / KeyError
  GET /slow     模拟响应超时     → 产生 Timeout（默认睡 35 秒）
  GET /oom      模拟内存溢出     → 产生 MemoryError
  GET /random   随机触发以上某一种故障

此外还有一个后台线程，每 20 秒会随机打印一条错误日志，
这样即使你不主动访问接口，容器日志里也一直有内容可采集。
"""

import logging
import os
import random
import socket
import sys
import threading
import time

from flask import Flask, jsonify

app = Flask(__name__)

# 日志必须输出到 stdout，否则 docker logs 收集不到
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("demo-app")

MYSQL_HOST = os.getenv("MYSQL_HOST", "demo-mysql")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))


@app.get("/health")
def health():
    """健康检查接口：只做轻量检查，不查数据库"""
    return jsonify(status="ok")


@app.get("/")
def index():
    log.info("index page visited")
    return jsonify(
        message="demo app is running",
        endpoints=["/health", "/db", "/boom", "/slow", "/oom", "/random"],
    )


@app.get("/db")
def db_error():
    """模拟数据库连接失败：用 socket 尝试连 MySQL，连不上就报错"""
    try:
        sock = socket.create_connection((MYSQL_HOST, MYSQL_PORT), timeout=3)
        sock.close()
        log.info("mysql connected")
        return jsonify(result="connected")
    except OSError as err:
        log.error("Failed to connect MySQL at %s:%s -> %s", MYSQL_HOST, MYSQL_PORT, err)
        log.error(
            'pymysql.err.OperationalError: (2003, "Can\'t connect to MySQL server '
            "on '%s' ([Errno 111] Connection refused)\")", MYSQL_HOST
        )
        return jsonify(error="database connection failed", detail=str(err)), 500


@app.get("/boom")
def boom():
    """模拟代码异常：访问不存在的字典键"""
    try:
        data = {}
        user_id = data["user_id"]          # 这里必然抛 KeyError
        return jsonify(user_id=user_id)
    except KeyError:
        # logger.exception 会把完整堆栈打进日志
        log.exception("Exception on /boom [GET]")
        return jsonify(error="internal server error"), 500


@app.get("/slow")
def slow():
    """模拟响应超时：睡眠指定秒数"""
    seconds = float(os.getenv("SLOW_SECONDS", "35"))
    log.warning("start slow task, will sleep %s seconds", seconds)
    time.sleep(seconds)
    log.info("slow task done")
    return jsonify(result="done", slept=seconds)


@app.get("/oom")
def oom():
    """模拟内存溢出：不断申请内存"""
    log.warning("start allocating memory, this may trigger OOM")
    chunks = []
    for i in range(500):
        chunks.append(" " * 10 * 1024 * 1024)   # 每次 10MB
        log.info("allocated chunk %d", i + 1)
    return jsonify(result="allocated")


@app.get("/random")
def random_error():
    """随机触发一种故障，方便演示"""
    choice = random.choice(["db", "boom", "slow", "ok", "ok"])
    if choice == "db":
        return db_error()
    if choice == "boom":
        return boom()
    if choice == "slow":
        return slow()
    log.info("random check: everything is fine")
    return jsonify(result="ok", lucky=True)


# ---------------- 后台噪声日志 ----------------

NOISE_TEMPLATES = [
    "ERROR in worker: Failed to connect to Redis at redis:6379, Connection refused",
    '[ERROR] Failed to write /var/log/app/access.log: [Errno 28] No space left on device',
    "ERROR: database query timeout after 30s, SQL: SELECT * FROM orders WHERE status='pending'",
    "WARNING: memory usage 480MiB / 512MiB,接近上限",
    "ERROR in app: Exception on /api/order [POST] -> KeyError: 'order_id'",
]


def background_noise():
    """后台线程：定期打印一条错误日志，保证容器日志持续有内容"""
    while True:
        time.sleep(20)
        log.error(random.choice(NOISE_TEMPLATES))


if __name__ == "__main__":
    threading.Thread(target=background_noise, daemon=True).start()
    log.info("demo app starting on 0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)
