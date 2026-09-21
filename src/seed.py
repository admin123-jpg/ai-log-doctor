"""
种子数据生成脚本
=================================
作用：往数据库里灌一批「历史诊断记录」，让前端页面一开始就有东西可看。

数据来源（本项目演示数据的三个来源）：
  1. samples/demo-logs.json  —— 手写的故障日志样本，模拟容器真实输出
  2. samples/ci-failed.log   —— 模拟 GitHub Actions 流水线失败的日志
  3. 程序随机生成时间戳和状态 —— 让数据看起来像积累了几天

注意：这些是「编造的演示数据」，目的是让你在没有真实生产环境时，
依然能完整体验 采集 → 检测 → RAG → 诊断 → 展示 的全流程。
"""

import random
from datetime import datetime, timedelta

from . import db
from .collector import load_demo_logs, read_log_file
from .config import Config
from .pipeline import analyze_text, get_kb

# 状态分布：new 最多，符合真实情况（大部分新告警还没处理）
STATUS_POOL = ["new"] * 6 + ["resolved"] * 3 + ["ignored"]

SERVICE_SOURCE = {
    "demo-app": "docker",
    "demo-nginx": "docker",
    "demo-mysql": "docker",
}


def _random_past_time(days: int = 7) -> str:
    """生成过去 N 天内的随机时间"""
    now = datetime.now()
    delta = timedelta(
        days=random.randint(0, days),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )
    return (now - delta).strftime("%Y-%m-%d %H:%M:%S")


def generate_history(rounds: int = 3, days: int = 7, clear: bool = True) -> int:
    """
    生成历史诊断记录。

    参数：
      rounds - 每个样本重复几轮（用来凑数据量）
      days   - 时间分布在过去多少天内
      clear  - 是否先清空旧数据

    返回生成的记录条数。
    """
    db.init_db()
    get_kb()  # 确保知识库已构建

    if clear:
        db.clear_diagnoses()

    count = 0
    demo_logs = load_demo_logs()

    # ---------- 来源1 & 2：容器日志样本 ----------
    for _ in range(rounds):
        for service, logs in demo_logs.items():
            if service.startswith("_"):   # 跳过说明字段
                continue
            for log in logs:
                record = analyze_text(
                    text=log,
                    source=SERVICE_SOURCE.get(service, "docker"),
                    service=service,
                    save=False,          # 先不入库，改完时间和状态再存
                )
                if not record:
                    continue
                record["created_at"] = _random_past_time(days)
                record["status"] = random.choice(STATUS_POOL)
                db.create_diagnosis(record)
                count += 1

    # ---------- 来源3：CI 流水线失败日志 ----------
    ci_log = read_log_file(Config.SAMPLES_DIR / "ci-failed.log")
    if ci_log.strip():
        record = analyze_text(
            text=ci_log, source="ci", service="github-actions", save=False
        )
        if record:
            record["created_at"] = _random_past_time(days)
            record["status"] = random.choice(STATUS_POOL)
            db.create_diagnosis(record)
            count += 1

    return count


if __name__ == "__main__":
    n = generate_history()
    print(f"已生成 {n} 条历史诊断记录")
    print("统计数据：", db.stats())
