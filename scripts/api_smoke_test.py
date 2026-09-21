"""
API 冒烟测试脚本
=================================
作用：服务启动后，用它一键验证所有接口是否正常，顺便走一遍完整 CRUD。

用法（先确保服务已启动）：
    python scripts/api_smoke_test.py
    python scripts/api_smoke_test.py --base http://127.0.0.1:8000

这个脚本会依次验证：
  1. 健康检查         GET  /api/health
  2. 配置查看         GET  /api/config
  3. 统计信息         GET  /api/stats
  4. 知识库状态       GET  /api/kb/info
  5. RAG 检索         GET  /api/kb/search
  6. C 创建记录       POST   /api/diagnoses
  7. R 查询列表       GET    /api/diagnoses
  8. R 查询单条       GET    /api/diagnoses/{id}
  9. U 更新记录       PUT    /api/diagnoses/{id}
  10. D 删除记录      DELETE /api/diagnoses/{id}
  11. 立即扫描        POST   /api/scan
"""

import argparse
import sys
import time

import requests

PASS, FAIL = "[PASS]", "[FAIL]"


def wait_for_service(base: str, retries: int = 15) -> bool:
    """等待服务启动就绪（最多重试 15 次，每次间隔 1 秒）"""
    for i in range(retries):
        try:
            r = requests.get(f"{base}/api/health", timeout=3)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def print_step(no: int, title: str):
    print(f"\n{'=' * 60}")
    print(f"步骤 {no}：{title}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="API 冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print(f"目标服务：{base}")
    print("正在等待服务就绪...")

    if not wait_for_service(base):
        print(f"{FAIL} 服务未启动。请先执行：python -m src.main serve")
        return 1
    print(f"{PASS} 服务已就绪\n")

    success = True

    # ---------- 1. 健康检查 ----------
    print_step(1, "健康检查  GET /api/health")
    r = requests.get(f"{base}/api/health", timeout=10)
    d = r.json()["data"]
    print(f"{PASS} 状态码 {r.status_code}")
    print(f"      运行状态：{d['status']}")
    print(f"      运行模式：{'Mock 模式（未配置 API Key）' if d['mock_mode'] else '真实大模型模式'}")
    print(f"      数据库：{d['db']}")

    # ---------- 2. 配置查看 ----------
    print_step(2, "配置查看  GET /api/config")
    r = requests.get(f"{base}/api/config", timeout=10)
    for k, v in r.json()["data"].items():
        print(f"      {k}: {v}")
    print(f"{PASS} 配置读取正常")

    # ---------- 3. 统计信息 ----------
    print_step(3, "统计信息  GET /api/stats")
    r = requests.get(f"{base}/api/stats", timeout=10)
    s = r.json()["data"]
    print(f"      总数：{s['total']}")
    print(f"      按状态：{s['by_status']}")
    print(f"      按服务：{s['by_service']}")
    print(f"      按等级：{s['by_severity']}")
    print(f"{PASS} 统计接口正常")

    # ---------- 4. 知识库状态 ----------
    print_step(4, "知识库状态  GET /api/kb/info")
    r = requests.get(f"{base}/api/kb/info", timeout=10)
    k = r.json()["data"]
    print(f"      向量库后端：{k['backend']}")
    print(f"      向量化方式：{k['embedder']}")
    print(f"      知识片段数：{k['chunk_count']}")
    if k["chunk_count"] == 0:
        print(f"{FAIL} 知识库为空，请执行：python -m src.main kb")
        success = False
    else:
        print(f"{PASS} 知识库已就绪")

    # ---------- 5. RAG 检索 ----------
    print_step(5, "RAG 检索  GET /api/kb/search")
    q = "Connection refused 数据库连接失败"
    r = requests.get(f"{base}/api/kb/search", params={"q": q, "top_k": 2}, timeout=20)
    results = r.json()["data"]["results"]
    print(f"      查询词：{q}")
    for i, item in enumerate(results, 1):
        print(f"      [{i}] {item['source']} · {item['title']}  相似度 {item['score']}")
    if results:
        print(f"{PASS} RAG 检索正常（说明向量库真的在工作）")
    else:
        print(f"{FAIL} 未检索到结果")
        success = False

    # ---------- 6. C：创建 ----------
    print_step(6, "C - 创建记录  POST /api/diagnoses")
    payload = {
        "raw_log": (
            "pymysql.err.OperationalError: (2003, \"Can't connect to MySQL server "
            "on 'demo-mysql' ([Errno 111] Connection refused)\")"
        ),
        "source": "manual",
        "service": "smoke-test-svc",
    }
    r = requests.post(f"{base}/api/diagnoses", json=payload, timeout=60)
    if r.status_code != 200:
        print(f"{FAIL} 创建失败：{r.status_code} {r.text[:200]}")
        return 1
    created = r.json()["data"]
    did = created["id"]
    print(f"{PASS} 创建成功，记录 ID = {did}")
    print(f"      命中关键字：{created['matched_keyword']}")
    print(f"      严重程度：{created['severity']}")
    print(f"      根因分析：{created['root_cause'][:80]}...")
    print(f"      处置建议（共 {len(created['suggestions'])} 条）：")
    for i, s in enumerate(created["suggestions"][:3], 1):
        print(f"        {i}. {s}")
    print(f"      RAG 参考来源（共 {len(created['used_references'])} 条）：")
    for ref in created["used_references"][:2]:
        print(f"        - {ref['source']} · {ref['title']}（相似度 {ref['score']}）")

    # ---------- 7. R：查询列表 ----------
    print_step(7, "R - 查询列表  GET /api/diagnoses")
    r = requests.get(f"{base}/api/diagnoses", params={"limit": 3}, timeout=10)
    lst = r.json()["data"]
    print(f"      总记录数：{lst['total']}，本次返回 {lst['count']} 条")
    for item in lst["items"]:
        print(f"      #{item['id']} [{item['severity']}] {item['service']} - {item['root_cause'][:40]}...")
    print(f"{PASS} 列表查询正常")

    # ---------- 8. R：查询单条 ----------
    print_step(8, f"R - 查询单条  GET /api/diagnoses/{did}")
    r = requests.get(f"{base}/api/diagnoses/{did}", timeout=10)
    one = r.json()["data"]
    print(f"      服务：{one['service']}   状态：{one['status']}")
    print(f"{PASS} 单条查询正常")

    # ---------- 9. U：更新 ----------
    print_step(9, f"U - 更新记录  PUT /api/diagnoses/{did}")
    r = requests.put(
        f"{base}/api/diagnoses/{did}",
        json={"status": "resolved"},
        timeout=10,
    )
    updated = r.json()["data"]
    print(f"      更新前状态：new  ->  更新后状态：{updated['status']}")
    print(f"{PASS} 更新成功")

    # ---------- 10. D：删除 ----------
    print_step(10, f"D - 删除记录  DELETE /api/diagnoses/{did}")
    r = requests.delete(f"{base}/api/diagnoses/{did}", timeout=10)
    print(f"      {r.json()['message']}")

    # 确认真的删掉了
    r = requests.get(f"{base}/api/diagnoses/{did}", timeout=10)
    if r.status_code == 404:
        print(f"{PASS} 已确认删除（再次查询返回 404）")
    else:
        print(f"{FAIL} 删除未生效")
        success = False

    # ---------- 11. 扫描 ----------
    print_step(11, "立即扫描  POST /api/scan")
    r = requests.post(f"{base}/api/scan", timeout=60)
    scan = r.json()["data"]
    print(f"      检查服务数：{scan['checked']}")
    print(f"      发现异常数：{scan['hit']}")
    print(f"      入库记录数：{scan['saved']}")
    print(f"{PASS} 扫描接口正常")

    # ---------- 总结 ----------
    print("\n" + "=" * 60)
    if success:
        print("全部接口验证通过！")
        print(f"\n现在打开浏览器访问：")
        print(f"  接口文档（可交互式测试）：{base}/docs")
        print(f"  前端诊断面板：            {base}/")
    else:
        print("存在失败项，请检查上面的输出。")
    print("=" * 60 + "\n")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
