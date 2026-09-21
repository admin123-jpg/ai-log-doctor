"""
日志采集模块
=================================
作用：从不同来源把日志「抓」回来，统一成同一种格式交给下游处理。

三种数据来源：
  1. Docker 容器日志  —— 真实来源，通过 docker logs 命令获取
  2. CI/CD 流水线日志 —— 从本地文件读取（模拟 GitHub Actions 的输出）
  3. 演示样本日志    —— Docker 没装 / 容器没启动时，用它兜底，保证流程能跑通

为什么要设计「兜底」？
  新手环境经常遇到 Docker 没启动、容器还没起来，如果程序直接报错，
  你就看不到后面的 RAG、AI、前端效果了。降级到样本日志，能先跑通全链路。
"""

import json
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config


@dataclass
class LogItem:
    """统一格式的日志条目"""
    source: str    # 来源：docker / ci / demo
    service: str   # 服务名：demo-app / demo-mysql / github-actions
    content: str   # 日志正文


# ============================ 1. Docker 相关 ============================

def docker_available() -> bool:
    """检查本机 Docker 是否可用（能执行 docker version 就算可用）"""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_running_containers() -> list:
    """列出正在运行的容器名，等价于命令：docker ps --format {{.Names}}"""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def fetch_container_logs(container: str, tail: Optional[int] = None) -> Optional[str]:
    """
    获取某个容器最近的日志，等价于命令：
        docker logs --tail 100 <容器名>

    参数 container：容器名或容器 ID
    返回日志文本；容器不存在或命令失败返回 None
    """
    tail = tail or Config.LOG_TAIL
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        # docker logs 把正常输出放 stdout，把部分警告放 stderr，所以两个都拼上
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and not output.strip():
            return None
        return output.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ============================ 2. 本地文件（CI 日志）============================

def read_log_file(path) -> str:
    """读取本地日志文件（用于模拟 CI/CD 流水线日志）"""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


# ============================ 3. 演示样本日志（兜底数据）============================

def load_demo_logs() -> dict:
    """
    从 samples/demo-logs.json 加载模拟日志。
    文件不存在时返回内置的默认样本（保证一定能跑）。
    """
    path = Config.SAMPLES_DIR / "demo-logs.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return _default_demo_logs()


def _default_demo_logs() -> dict:
    """内置默认样本：模拟三种典型故障"""
    return {
        "demo-app": [
            "Traceback (most recent call last):\n"
            '  File "/app/app.py", line 42, in index\n'
            "    conn = pymysql.connect(host='demo-mysql', user='root')\n"
            '  File "/usr/local/lib/python3.11/site-packages/pymysql/connections.py", line 644, in __init__\n'
            "    raise OperationalError(2003, \"Can't connect to MySQL server on 'demo-mysql'\")\n"
            'pymysql.err.OperationalError: (2003, "Can\'t connect to MySQL server on \'demo-mysql\' ([Errno 111] Connection refused)")',
        ],
        "demo-nginx": [
            '2026/09/15 01:20:11 [error] 31#31: *12 connect() failed (111: Connection refused) '
            'while connecting to upstream, client: 172.18.0.1, server: localhost, '
            'request: "GET /api/data HTTP/1.1", upstream: "http://172.18.0.3:5000/api/data"',
        ],
        "demo-mysql": [
            "2026-09-15T01:20:05.123456Z 0 [ERROR] [MY-010311] [Server] "
            "Failed to start mysqld: Can't create/write to file '/var/lib/mysql/ibdata1' (OS errno 13 - Permission denied)",
        ],
    }


def demo_log(service: str) -> str:
    """随机取一条该服务的模拟日志"""
    logs = load_demo_logs()
    pool = logs.get(service) or logs.get("demo-app") or []
    if not pool:
        return ""
    return random.choice(pool)


# ============================ 统一入口 ============================

def collect_all(
    containers: Optional[list] = None,
    prefer_docker: bool = True,
    allow_demo: bool = True,
) -> list:
    """
    采集所有来源的日志。

    返回 LogItem 列表。逻辑：
      1. 如果 Docker 可用且容器在跑 → 抓真实日志
      2. 否则 → 用 demo 样本兜底（source 标记为 demo）

    ⚠️ allow_demo=False 时**不**用演示样本兜底，采不到就返回空列表。

    为什么需要这个开关？
      样本日志是「编造的数据」。在服务端静默用它兜底，会把假数据
      混进真实数据库 —— 面板看起来一切正常，内容却全是假的。
      这种「静默失败」比直接报错危险得多。
      所以：服务端（HTTP 接口）一律传 False，只有本地体验/演示才显式允许。
    """
    containers = containers or Config.WATCH_CONTAINERS
    items = []

    use_docker = prefer_docker and docker_available()
    running = set(list_running_containers()) if use_docker else set()

    for name in containers:
        if use_docker and name in running:
            content = fetch_container_logs(name)
            source = "docker"
        elif allow_demo:
            content = demo_log(name)
            source = "demo"
        else:
            continue          # 明确不兜底：宁可少一条数据，也不要假数据

        if content and content.strip():
            items.append(LogItem(source=source, service=name, content=content))

    return items


def collect_ci_log(path=None) -> Optional[LogItem]:
    """采集 CI 流水线日志（从本地文件读，模拟 GitHub Actions 输出）"""
    p = Path(path) if path else (Config.SAMPLES_DIR / "ci-failed.log")
    content = read_log_file(p)
    if not content.strip():
        return None
    return LogItem(source="ci", service="github-actions", content=content)


# ============================ 4. 日志文件目录 ============================

# 常见日志文件名 → 服务名 的映射（用于自动识别日志来自哪个中间件）
LOG_NAME_HINTS = {
    "redis": "redis",
    "mysql": "mysql",
    "mariadb": "mysql",
    "tomcat": "tomcat",
    "catalina": "tomcat",
    "nginx": "nginx",
    "elasticsearch": "elasticsearch",
    "kafka": "kafka",
    "rabbitmq": "rabbitmq",
    "mongodb": "mongodb",
    "ci-failed": "github-actions",
}


def guess_service(filename: str) -> str:
    """
    根据文件名猜测是哪个服务的日志。
    例如 redis.log → redis，tomcat-catalina.out → tomcat
    """
    lower = filename.lower()
    for hint, service in LOG_NAME_HINTS.items():
        if hint in lower:
            return service
    return Path(filename).stem      # 猜不出就用文件名（去掉扩展名）


def collect_log_directory(directory=None) -> list:
    """
    批量读取一个目录下的所有日志文件。

    这是「离线分析」的入口：把你收集到的 Redis / MySQL / Tomcat 等日志丢进目录，
    程序会逐个读取并送去诊断。

    支持扩展名：.log / .out / .txt
    返回 LogItem 列表，每个文件的 service 由文件名自动推断。
    """
    d = Path(directory) if directory else (Config.SAMPLES_DIR / "app-logs")
    if not d.exists():
        return []

    items = []
    patterns = ("*.log", "*.out", "*.txt")

    for pattern in patterns:
        for path in sorted(d.glob(pattern)):
            content = read_log_file(path)
            if not content.strip():
                continue
            items.append(LogItem(
                source="file",
                service=guess_service(path.name),
                content=content,
            ))

    return items
