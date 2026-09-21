"""
AI 容器日志医生 - 命令行入口
=================================
提供几个子命令，方便你一步步验证系统：

    python -m src.main check    环境自检（第一次运行先执行它）
    python -m src.main kb       构建知识库（RAG 向量库）
    python -m src.main seed     生成演示数据
    python -m src.main scan     扫描一次容器日志并诊断
    python -m src.main serve    启动 Web 服务（FastAPI）
"""

import argparse
import json
import sys

from .config import Config


# ============================ check：环境自检 ============================

def _check_item(name: str, condition: bool, hint: str = "") -> bool:
    """打印一行检查结果"""
    mark = "[OK]  " if condition else "[FAIL]"
    print(f"{mark} {name}" + (f"  -> {hint}" if hint and not condition else ""))
    return condition


def cmd_check(args):
    """检查运行环境，逐项确认依赖是否就绪"""
    print("\n===== 环境自检 =====\n")
    all_ok = True

    # 1. Python 版本
    v = sys.version_info
    all_ok &= _check_item(
        f"Python 版本 {v.major}.{v.minor}.{v.micro}",
        v.major == 3 and v.minor >= 9,
        "需要 Python 3.9 及以上",
    )

    # 2. 依赖包
    required = {
        "fastapi": "Web 框架，提供接口",
        "uvicorn": "ASGI 服务器，运行 FastAPI",
        "requests": "调用大模型 API",
        "dotenv": "读取 .env 配置",
    }
    optional = {
        "chromadb": "Chroma 向量数据库（可选，缺失会自动降级）",
        "pytest": "单元测试（可选）",
    }

    print("\n-- 必需依赖 --")
    for pkg, desc in required.items():
        try:
            __import__(pkg)
            all_ok &= _check_item(f"{pkg:<12} {desc}", True)
        except ImportError:
            all_ok &= _check_item(f"{pkg:<12} {desc}", False, "请执行 pip install -r requirements.txt")

    print("\n-- 可选依赖 --")
    for pkg, desc in optional.items():
        try:
            __import__(pkg)
            _check_item(f"{pkg:<12} {desc}", True)
        except ImportError:
            print(f"[--]  {pkg:<12} {desc}  (未安装，不影响运行)")

    # 3. Docker
    print("\n-- Docker --")
    from .collector import docker_available, list_running_containers
    if docker_available():
        _check_item("Docker 可用", True)
        running = list_running_containers()
        watch = Config.WATCH_CONTAINERS
        found = [c for c in watch if c in running]
        _check_item(
            f"被监控容器（{', '.join(watch)}）",
            len(found) > 0,
            "未发现运行中的容器，会降级使用演示样本。可执行 docker compose up -d 启动",
        )
        if running:
            print(f"      当前运行中的容器：{', '.join(running)}")
    else:
        print("[--]  Docker 不可用或没启动，将使用演示样本日志（不影响功能演示）")

    # 4. 知识库
    print("\n-- 知识库 --")
    from .rag import build_chunks
    chunks = build_chunks()
    all_ok &= _check_item(
        f"知识库片段数：{len(chunks)}",
        len(chunks) > 0,
        f"请确认 {Config.KNOWLEDGE_DIR} 下有 .md 文件",
    )

    # 5. 数据库
    print("\n-- 数据库 --")
    try:
        from . import db
        db.init_db()
        _check_item(f"SQLite 可用（{Config.DB_PATH}）", True)
        print(f"      当前记录数：{db.count_diagnoses()}")
    except Exception as e:
        all_ok &= _check_item("SQLite 初始化", False, str(e))

    # 6. 大模型配置
    print("\n-- 大模型 --")
    if Config.LLM_API_KEY:
        _check_item(f"已配置 API Key（{Config.LLM_MODEL} @ {Config.LLM_BASE_URL}）", True)
        print("      模式：真实大模型调用")
    else:
        print("[--]  未配置 LLM_API_KEY")
        print("      模式：Mock 模式（用规则库生成诊断，流程可完整跑通）")
        print("      想接真实模型：复制 .env.example 为 .env，填入 Key 后重启")

    # 总结
    print("\n===== 自检结果 =====")
    if all_ok:
        print("核心项全部通过！接下来可以执行：")
        print("  python -m src.main kb      # 构建知识库")
        print("  python -m src.main seed    # 生成演示数据")
        print("  python -m src.main serve   # 启动服务")
    else:
        print("有项目未通过，请按上面的提示修复后重试。")
    print()
    return 0 if all_ok else 1


# ============================ kb：构建知识库 ============================

def cmd_kb(args):
    """构建 / 重建 RAG 向量库"""
    from .pipeline import get_kb
    print("\n===== 构建知识库 =====")
    kb = get_kb(force_rebuild=args.force)
    info = kb.info()
    print(f"后端：{info['backend']}")
    print(f"向量化：{info['embedder']}")
    print(f"片段数：{info['chunk_count']}")

    # 演示一次检索，让你直观看到 RAG 效果
    demo_queries = args.query or ["Connection refused", "磁盘空间不足"]
    print("\n--- 检索效果演示 ---")
    for q in demo_queries:
        print(f"\n查询：{q}")
        results = kb.retrieve(q, top_k=2)
        if not results:
            print("  （无结果，请确认知识库已构建）")
        for i, r in enumerate(results, 1):
            print(f"  [{i}] {r['source']} · {r['title']}  （相似度 {r['score']}）")
            print(f"      {r['text'][:80]}...")
    print()
    return 0


# ============================ seed：生成演示数据 ============================

def cmd_seed(args):
    from .seed import generate_history
    from . import db
    print("\n===== 生成演示数据 =====")
    n = generate_history(rounds=args.rounds, clear=not args.keep)
    print(f"已生成 {n} 条记录")
    print("统计：", json.dumps(db.stats(), ensure_ascii=False, indent=2))
    print()
    return 0


# ============================ scan：扫描诊断 ============================

def cmd_scan(args):
    from .pipeline import run_ci_scan, run_scan
    print("\n===== 扫描日志 =====")
    result = run_scan(prefer_docker=not args.no_docker)
    print(f"检查服务数：{result['checked']}")
    print(f"发现异常数：{result['hit']}")
    print(f"入库记录数：{result['saved']}\n")

    for item in result["items"]:
        print(f"[{item['severity'].upper()}] {item['service']}  (命中关键字：{item['matched_keyword']})")
        print(f"  根因：{item['root_cause']}")
        for i, s in enumerate(item.get("suggestions", [])[:3], 1):
            print(f"  建议{i}：{s}")
        refs = item.get("used_references", [])
        if refs:
            print(f"  参考：{refs[0]['source']} · {refs[0]['title']}（相似度 {refs[0]['score']}）")
        print()

    if args.ci:
        print("--- CI 日志诊断 ---")
        r = run_ci_scan()
        if r:
            print(f"根因：{r['root_cause']}")
            for i, s in enumerate(r.get("suggestions", [])[:3], 1):
                print(f"建议{i}：{s}")
        else:
            print("未发现 samples/ci-failed.log 或无异常")
    return 0


# ============================ ingest：批量分析日志文件 ============================

def cmd_ingest(args):
    """
    读取一个目录下的所有日志文件，逐个切分成异常块并诊断。

    这是「离线分析」的入口：把 Redis / MySQL / Tomcat 等中间件的日志丢进
    samples/app-logs 目录，执行这个命令就能批量产出诊断结果。
    """
    from . import db
    from .collector import collect_log_directory
    from .detector import split_by_error_patterns
    from .pipeline import analyze_text

    default_dir = Config.SAMPLES_DIR / "app-logs"
    target = args.dir or str(default_dir)

    print("\n===== 批量分析日志文件 =====")
    print(f"目录：{target}\n")

    if args.clear:
        n = db.clear_diagnoses()
        print(f"（--clear）已清空 {n} 条历史记录\n")

    items = collect_log_directory(args.dir)
    if not items:
        print("未找到日志文件。")
        print("请把 .log / .out / .txt 文件放进该目录后重试。")
        return 1

    total_files = len(items)
    total_blocks = 0
    total_hit = 0

    for item in items:
        blocks = split_by_error_patterns(item.content)
        total_blocks += len(blocks)
        print(f"【{item.service}】共 {len(item.content.splitlines())} 行，"
              f"切出 {len(blocks)} 个异常片段")

        for i, block in enumerate(blocks, 1):
            record = analyze_text(
                text=block,
                source="file",
                service=item.service,
                save=not args.dry_run,
            )
            if not record:
                continue
            total_hit += 1
            print(f"  ── 片段 {i} [{record['severity'].upper()}] "
                  f"命中「{record['matched_keyword']}」")
            print(f"     根因：{record['root_cause'][:70]}...")
            refs = record.get("used_references", [])
            if refs:
                print(f"     RAG：{refs[0]['source']} · {refs[0]['title']} "
                      f"（相似度 {refs[0]['score']}）")
        print()

    print("=" * 60)
    print(f"汇总：{total_files} 个日志文件 → {total_blocks} 个异常片段 "
          f"→ 生成 {total_hit} 条诊断记录")
    if args.dry_run:
        print("（--dry-run 模式，未写入数据库）")
    else:
        from . import db
        print(f"数据库现有记录数：{db.count_diagnoses()}")
    print()
    return 0


# ============================ serve：启动服务 ============================

def cmd_serve(args):
    import uvicorn
    host = args.host or Config.API_HOST
    port = args.port or Config.API_PORT
    print(f"\n启动服务： http://{host}:{port}")
    print(f"接口文档： http://{host}:{port}/docs")
    print("按 Ctrl+C 停止\n")
    uvicorn.run(
        "src.api:app",
        host=host,
        port=port,
        reload=args.reload,   # 改代码自动重启，开发时很方便
    )
    return 0


# ============================ 参数解析 ============================

def main():
    parser = argparse.ArgumentParser(
        prog="ai-log-doctor",
        description="AI 容器日志医生：Docker/CI 日志的自动采集、异常检测与 AI 根因分析",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # check
    p_check = sub.add_parser("check", help="环境自检")
    p_check.set_defaults(func=cmd_check)

    # kb
    p_kb = sub.add_parser("kb", help="构建知识库")
    p_kb.add_argument("--force", action="store_true", help="强制重建")
    p_kb.add_argument("--query", nargs="*", help="自定义检索测试词")
    p_kb.set_defaults(func=cmd_kb)

    # seed
    p_seed = sub.add_parser("seed", help="生成演示数据")
    p_seed.add_argument("--rounds", type=int, default=3, help="重复轮数（默认3）")
    p_seed.add_argument("--keep", action="store_true", help="保留已有数据")
    p_seed.set_defaults(func=cmd_seed)

    # scan
    p_scan = sub.add_parser("scan", help="扫描日志并诊断")
    p_scan.add_argument("--no-docker", action="store_true", help="跳过 Docker，只用样本")
    p_scan.add_argument("--ci", action="store_true", help="同时诊断 CI 日志")
    p_scan.set_defaults(func=cmd_scan)

    # ingest
    p_ingest = sub.add_parser("ingest", help="批量分析日志目录下的日志文件")
    p_ingest.add_argument("--dir", help="日志目录（默认 samples/app-logs）")
    p_ingest.add_argument("--dry-run", action="store_true", help="只分析不入库")
    p_ingest.add_argument("--clear", action="store_true", help="分析前先清空历史记录")
    p_ingest.set_defaults(func=cmd_ingest)

    # serve
    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--host", help="监听地址")
    p_serve.add_argument("--port", type=int, help="监听端口")
    p_serve.add_argument("--reload", action="store_true", help="代码热重载（开发用）")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
