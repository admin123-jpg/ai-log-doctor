"""
大模型调用模块
=================================
作用：把「异常日志 + RAG 检索到的参考资料」组装成 Prompt，发给大模型，
     拿到结构化的诊断结论（根因 + 建议 + 严重程度）。

两种模式：
  1. 真实模式：配置了 LLM_API_KEY，调用 OpenAI 兼容接口（DeepSeek/通义等）
  2. Mock 模式：没配 Key 时，用规则库匹配生成结论 —— 保证流程能完整跑通

为什么要做 Mock 模式？
  新手最常见的问题是「申请 Key 要钱、要绑卡、要等」，如果没 Key 就什么都看不到，
  很容易半途而废。Mock 模式让你先把整条链路跑通，看到效果了再接真实模型。
"""

import json
import re
from typing import Optional

import requests

from .config import Config


# ============================ Prompt 模板 ============================

SYSTEM_PROMPT = """你是一名资深运维工程师（SRE），擅长 Docker、Nginx、MySQL、CI/CD 故障排查。
你的任务是根据异常日志和参考资料，快速定位根因并给出可执行的排查建议。

要求：
1. 优先依据【参考资料】作答，不要编造资料里没有的信息。
2. 根因分析要具体，说明「是什么导致的」，不要说空话。
3. 建议必须是可执行的命令或操作，每条一行，不要写成长篇大论。
4. 只输出 JSON，不要有任何其他文字。"""

USER_PROMPT_TEMPLATE = """服务名称：{service}
日志来源：{source}

【异常日志】
{log}

【参考资料】
{references}

请分析并严格按下面的 JSON 格式输出：
{{
  "root_cause": "一句话说明根本原因",
  "suggestions": ["建议1（含具体命令）", "建议2", "建议3"],
  "severity": "high 或 medium 或 low",
  "confidence": 0.85
}}"""


def build_prompt(log: str, references: list, source: str, service: str) -> tuple:
    """组装 system / user 两段 Prompt，返回 (system, user)"""
    if references:
        ref_text = "\n\n".join(
            f"[{i}] 来源：{r['source']} · {r['title']}（相似度 {r['score']}）\n{r['text']}"
            for i, r in enumerate(references, start=1)
        )
    else:
        ref_text = "（本次没有检索到相关资料，请基于通用运维经验作答）"

    user = USER_PROMPT_TEMPLATE.format(
        service=service,
        source=source,
        log=log[:3000],
        references=ref_text[:4000],
    )
    return SYSTEM_PROMPT, user


# ============================ JSON 解析 ============================

def extract_json(text: str) -> Optional[dict]:
    """
    从大模型返回文本里提取 JSON。
    模型经常会外包一层 ```json ... ```，或者前后加废话，所以需要容错处理。
    """
    if not text:
        return None

    # 情况1：带 markdown 代码围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        # 情况2：裸 JSON，取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ============================ Mock 规则库 ============================

# 格式：(正则, 根因, 建议列表, 严重程度)
MOCK_RULES = [
    (
        r"Connection refused|ECONNREFUSED|拒绝连接|Errno 111",
        "目标服务未启动或网络不通，导致连接被拒绝。常见于容器间使用了错误地址、"
        "目标容器尚未就绪、或两者不在同一 Docker 网络。",
        [
            "执行 docker ps -a 确认目标容器状态是否为 Up",
            "执行 docker network inspect 确认两个容器在同一个网络",
            "容器间通信要用服务名和容器内部端口，不要用 localhost 或宿主机映射端口",
            "在 docker-compose.yml 用 depends_on 声明依赖，并在应用里加重试",
        ],
        "high",
    ),
    (
        # 注意：这条必须放在通用 OOM 规则之前。
        # 因为 "OutOfMemoryError" 字符串里含有 "MemoryError"，
        # 如果让通用规则先匹配，JVM 堆溢出就会被误判成容器 OOMKilled。
        r"Java heap space|OutOfMemoryError|GC overhead limit exceeded|JvmGcMonitorService",
        "JVM 堆内存不足。可能是 -Xmx 设置过小，也可能存在内存泄漏，或一次性把超大数据集读进了内存。",
        [
            "看堆栈定位到具体类和方法，确认是哪段代码申请了大量内存",
            "加参数保留现场：-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp，再用 MAT 分析 dump",
            "调大堆内存：-Xms2g -Xmx2g（容器内建议不超过内存 limit 的 70%）",
            "大数据处理改分页或流式读取，避免一次性 SELECT * 全表加载",
        ],
        "high",
    ),
    (
        r"Out of memory|OOMKilled|OOM|MemoryError|内存溢出",
        "进程内存占用超过容器限制，被系统的 OOM Killer 强制杀掉（退出码 137）。",
        [
            "执行 docker inspect <容器> | grep -i oom 确认是否为 OOM",
            "在 docker-compose.yml 调大 deploy.resources.limits.memory",
            "优化代码：大结果集改分页查询，避免一次性 SELECT * 全表加载",
            "用 docker stats 持续观察内存增长曲线，排查内存泄漏",
        ],
        "high",
    ),
    (
        r"MISCONF Redis|Failed opening the RDB file|Error trying to save the DB",
        "Redis 的 RDB 持久化失败（目录无写权限或磁盘满），按 stop-writes-on-bgsave-error 策略主动拒绝了写入命令，以保护数据一致性。",
        [
            "检查 RDB 目录权限：ls -l /var/lib/redis，必要时 chown -R redis:redis",
            "检查磁盘：df -h，清理或扩容",
            "紧急恢复写入（临时）：redis-cli CONFIG SET stop-writes-on-bgsave-error no",
            "确认落盘路径：CONFIG GET dir 与 CONFIG GET dbfilename",
        ],
        "high",
    ),
    (
        r"Can't save in background: fork|fork: Cannot allocate memory",
        "Redis 做 RDB 快照需要 fork 子进程，但系统内存不足或 overcommit 策略拒绝了 fork 请求。",
        [
            "设置 sysctl vm.overcommit_memory=1 允许 fork",
            "给 Redis 容器预留更多内存，maxmemory 建议设为容器 limit 的 70%",
            "关闭 THP：echo never > /sys/kernel/mm/transparent_hugepage/enabled",
            "内存紧张时可降低 RDB 频率或改用 AOF",
        ],
        "high",
    ),
    (
        r"maxmemory policy|memory usage .* maxmemory|Evicting keys",
        "Redis 内存达到 maxmemory 上限。若策略为 noeviction 会直接拒绝写入，否则会按策略淘汰 key。",
        [
            "执行 INFO memory 查看 maxmemory_policy 和 used_memory",
            "缓存场景改为 allkeys-lru：CONFIG SET maxmemory-policy allkeys-lru",
            "用 redis-cli --bigkeys 找出大 key 并拆分",
            "调大 maxmemory（注意不要超过容器内存限制的 75%）",
        ],
        "high",
    ),
    (
        r"Connection with master lost|Unable to connect to MASTER|maxclients reached",
        "Redis 主从复制断开，或客户端连接数达到 maxclients 上限。",
        [
            "从库执行 INFO replication 查看 master_link_status",
            "测试主库连通性：redis-cli -h <master> -p 6379 ping",
            "检查 masterauth 与 requirepass 配置是否一致",
            "连接数打满时：客户端改用连接池，调大 maxclients 和系统 ulimit -n",
        ],
        "high",
    ),
    (
        r"flood stage disk watermark|disk watermark|read-only / allow delete",
        "Elasticsearch 磁盘达到洪水水位（默认 95%），为防止写满磁盘，已把该节点上的索引置为只读。",
        [
            "执行 GET _cat/allocation?v 确认磁盘使用情况",
            "清理旧索引：DELETE logs-2026.01*，或配置 ILM 自动滚动删除",
            "磁盘恢复后必须手动解除只读：PUT _all/_settings {\"index.blocks.read_only_allow_delete\": null}",
            "长期方案：扩容磁盘 + 配置 ILM 生命周期策略",
        ],
        "high",
    ),
    (
        r"CircuitBreakingException|Data too large",
        "Elasticsearch 触发了内存熔断保护：单个查询或聚合请求需要的内存超过断路器上限，通常是深分页或高基数字段聚合导致。",
        [
            "执行 GET _nodes/stats/breaker 查看各断路器占用",
            "执行 GET _tasks 找出正在跑的重查询并优化",
            "深分页改用 search_after 或 scroll，避免 from=10000",
            "避免对 text 类型字段做聚合，改用 keyword 字段",
        ],
        "high",
    ),
    (
        r"master not discovered|election requires at least|no longer in cluster",
        "Elasticsearch 主节点选举失败（quorum 不足或网络分区），集群处于不可用状态。",
        [
            "执行 GET _cat/nodes?v 确认哪些节点在线",
            "检查各节点 discovery.seed_hosts 和 initial_master_nodes 配置是否一致",
            "确认 9300 端口连通，排查网络分区和防火墙",
            "生产环境 master 候选节点建议设为奇数（3 或 5 个）",
        ],
        "high",
    ),
    (
        r"Connection pool exhausted|borrowConnection|HikariPool.*not available|Connection is not available",
        "应用数据库连接池耗尽，请求等待连接超时。通常是慢 SQL 长期占用连接、事务范围过大或连接未正确释放。",
        [
            "数据库侧执行 SHOW PROCESSLIST 排查慢查询和 Sleep 连接",
            "缩短事务范围，不要在事务里调用外部接口",
            "调大连接池 maximum-pool-size，并配置合理的 connection-timeout",
            "确认代码用 try-with-resources 或 finally 正确关闭连接",
        ],
        "high",
    ),
    (
        r"Too many open files",
        "进程打开的文件描述符达到系统 ulimit 上限，无法接受新连接或打开新文件。",
        [
            "执行 ulimit -n 查看当前限制，调大到 65535 并在 /etc/security/limits.conf 永久配置",
            "容器环境启动加 --ulimit nofile=65535:65535",
            "用 lsof -p <pid> | wc -l 看实际句柄数，排查未关闭的流和连接",
            "确认 HTTP 客户端复用了连接",
        ],
        "high",
    ),
    (
        r"NullPointerException",
        "代码对 null 对象调用了方法，通常是参数缺失、依赖服务返回空或缺少判空处理。",
        [
            "看堆栈最上面那行定位到具体类和行号",
            "对可能为 null 的对象加判空，或使用 Optional",
            "接口入口加参数校验（@Valid + @NotNull）",
            "配置全局异常处理器，返回规范错误码而不是 500 堆栈",
        ],
        "medium",
    ),
    (
        r"Deadlock found|Deadlock",
        "数据库事务死锁：多个事务以不同顺序锁定同一批资源形成循环等待，InnoDB 自动回滚了其中一个。",
        [
            "执行 SHOW ENGINE INNODB STATUS 查看 LATEST DETECTED DEADLOCK 段落",
            "统一业务里操作表和行的顺序",
            "缩短事务，不要在事务中做耗时操作",
            "应用层对错误码 1213 加重试机制",
        ],
        "high",
    ),
    (
        r"Slow query|rows_examined|duration: \d+\.\d+s",
        "存在慢查询，扫描行数（rows_examined）远大于返回行数，通常是缺少索引或 SQL 写法问题。",
        [
            "用 EXPLAIN 分析执行计划，重点看 type 是否为 ALL（全表扫描）和 rows",
            "给 WHERE / JOIN / ORDER BY 涉及的列加索引",
            "避免 SELECT *，只查需要的列",
            "大表分页改用基于主键的游标分页，避免 LIMIT 1000000,20",
        ],
        "medium",
    ),
    (
        r"no live upstreams",
        "Nginx 的 upstream 里所有后端节点都被标记为不可用，请求无法转发。",
        [
            "检查后端服务是否全部崩溃：docker ps -a",
            "查看后端日志确认崩溃原因",
            "检查 nginx 的 max_fails 和 fail_timeout 配置是否过于敏感",
            "确认健康检查路径是否正确返回 200",
        ],
        "high",
    ),
    (
        r"SSL_do_handshake\(\) failed|sslv3 alert certificate",
        "SSL/TLS 握手失败，通常是证书过期、证书链不完整或客户端不信任该证书。",
        [
            "用 openssl x509 -in cert.pem -noout -dates 检查证书有效期",
            "检查证书链是否完整（是否缺少中间证书）",
            "确认 Nginx 配置的 ssl_certificate 指向 fullchain 而非单独证书",
            "用 openssl s_client -connect host:443 验证握手",
        ],
        "medium",
    ),
    (
        r"limiting requests|limiting connections",
        "触发了 Nginx 限流（limit_req 或 limit_conn），请求被拒绝。",
        [
            "确认是正常流量增长还是异常刷量（看客户端 IP 分布）",
            "调整 limit_req_zone 的 rate 和 burst 参数",
            "对正常业务放白名单，对异常 IP 用 deny 封禁",
            "必要时在应用层做更精细的限流",
        ],
        "medium",
    ),
    (
        r"Timeout|timed out|timeout|超时",
        "上游服务响应超过设定的超时时间，可能是处理慢、死锁或连接池耗尽。",
        [
            "所有外部调用显式设置超时：requests.post(url, timeout=(5, 30))",
            "检查后端数据库是否存在慢查询，用 EXPLAIN 分析执行计划",
            "Nginx 侧调大 proxy_read_timeout，例如设为 60s",
            "耗时任务改异步处理，不要阻塞请求线程",
        ],
        "medium",
    ),
    (
        r"Permission denied|Access denied|errno 13|权限不足",
        "运行进程的用户对目标文件或目录没有读写权限，或属主 UID 与容器内用户不匹配。",
        [
            "执行 docker exec -it <容器> id 查看容器内用户 UID",
            "宿主机执行 chown -R <UID>:<GID> <目录> 修正属主",
            "MySQL 官方镜像的 UID 通常是 999，注意挂载目录属主要一致",
            "确认目录有执行(x)权限，否则无法进入",
        ],
        "high",
    ),
    (
        r"No space left on device|errno 28|磁盘满",
        "磁盘空间耗尽，写入操作失败。常见于容器日志无限制增长或 binlog 未清理。",
        [
            "执行 df -h 确认磁盘占用，docker system df 看 Docker 占用",
            "执行 docker system prune -a 清理无用镜像和容器",
            "配置日志轮转：max-size: 10m、max-file: \"3\"",
            "清理 MySQL binlog：PURGE BINARY LOGS BEFORE '2026-09-01'",
        ],
        "high",
    ),
    (
        r"Too many connections|Connection pool exhausted",
        "数据库连接数达到上限，新连接被拒绝。通常是连接泄漏或慢查询长期占用连接。",
        [
            "执行 SHOW PROCESSLIST 查看当前连接和状态",
            "临时提额：SET GLOBAL max_connections = 500",
            "检查应用是否用 try/finally 确保连接关闭，建议改用连接池",
            "设置 wait_timeout=300 自动回收空闲连接",
        ],
        "high",
    ),
    (
        r"502|Bad Gateway",
        "Nginx 作为反向代理无法连接到后端服务，后端可能已崩溃或地址配置错误。",
        [
            "执行 docker ps 确认后端容器是否在运行",
            "检查 nginx.conf 里 proxy_pass 的地址是否正确",
            "在 Nginx 容器里执行 wget -qO- http://<后端>:<端口> 测试连通",
            "执行 docker logs <后端容器> 查看后端自身错误",
        ],
        "medium",
    ),
    (
        r"504|upstream timed out",
        "Nginx 等待后端响应超时，后端处理时间超过了 proxy_read_timeout。",
        [
            "调整 nginx.conf：proxy_read_timeout 60s",
            "用 EXPLAIN 分析后端慢查询，给 WHERE 条件列加索引",
            "检查后端线程池/连接池是否被打满",
            "给耗时接口加缓存或改异步任务",
        ],
        "medium",
    ),
    (
        r"No matching distribution found|Could not find a version|pip install",
        "CI 环境安装 Python 依赖失败，通常是包需要编译、Python 版本不兼容或网络问题。",
        [
            "固定 Python 版本，例如 actions/setup-python 里 python-version: '3.11'",
            "给编译型依赖换纯 Python 替代方案（如向量库改用内存版）",
            "加参数重试：pip install --timeout 120 --retries 3 -r requirements.txt",
            "配置国内镜像源加速下载",
        ],
        "medium",
    ),
    (
        r"KeyError",
        "代码直接访问了字典里不存在的键，通常是请求参数缺失或上游返回结构变化。",
        [
            "把 data['key'] 改成 data.get('key')，避免抛异常",
            "在接口入口打印 request.json 确认实际参数结构",
            "给必填参数加校验，缺失时返回明确的 400 错误",
        ],
        "medium",
    ),
    (
        r"Traceback|Exception|Fatal",
        "应用抛出未捕获的异常导致请求失败。",
        [
            "查看完整 Traceback 定位到具体文件和行号",
            "不要用裸 except: pass，改成 logger.exception(e) 记录堆栈",
            "配置日志级别为 INFO 并输出到 stdout，确保 docker logs 能收集",
            "注册全局异常处理器，返回统一格式的错误响应",
        ],
        "high",
    ),
    (
        r"bind\(\) to .* failed|Address already in use|port is already allocated",
        "端口被其他进程或容器占用，导致绑定失败。",
        [
            "执行 netstat -ano | findstr :<端口>（Windows）或 ss -tulnp | grep :<端口>（Linux）",
            "停掉占用端口的进程，或修改 docker-compose 的端口映射",
        ],
        "high",
    ),
]


def mock_diagnose(log: str, source: str, service: str) -> dict:
    """Mock 模式：用规则库匹配日志，生成诊断结论"""
    for pattern, cause, suggestions, severity in MOCK_RULES:
        if re.search(pattern, log, re.I):
            return {
                "root_cause": cause,
                "suggestions": suggestions,
                "severity": severity,
                "confidence": 0.75,
                "mode": "mock",
            }

    # 兜底：没匹配到任何规则
    return {
        "root_cause": "日志中检测到异常关键字，但未匹配到已知故障模式，建议人工介入分析。",
        "suggestions": [
            "查看完整日志上下文，确认异常发生的时间点和触发操作",
            "执行 docker logs <容器名> 获取更多信息",
            "确认最近是否有配置变更或版本发布",
        ],
        "severity": "medium",
        "confidence": 0.4,
        "mode": "mock",
    }


# ============================ 真实调用 ============================

def call_llm(system: str, user: str) -> str:
    """
    调用 OpenAI 兼容的聊天接口。
    支持的厂商：DeepSeek、通义千问（兼容模式）、OpenAI、月之暗面等。
    """
    url = Config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {Config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": Config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,   # 温度越低输出越稳定，运维场景不需要创造性
        "max_tokens": 1200,
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ============================ 对外主入口 ============================

def diagnose(
    log: str,
    references: Optional[list] = None,
    source: str = "docker",
    service: str = "unknown",
) -> dict:
    """
    诊断主函数：输入日志和参考资料，输出诊断结论。

    返回结构：
    {
      "root_cause": "根因描述",
      "suggestions": ["建议1", "建议2"],
      "severity": "high|medium|low",
      "confidence": 0.85,
      "mode": "llm" | "mock" | "llm-fallback"
    }
    """
    references = references or []

    # Mock 模式：直接走规则库
    if Config.MOCK_MODE or not Config.LLM_API_KEY:
        return mock_diagnose(log, source, service)

    # 真实模式
    system, user = build_prompt(log, references, source, service)
    try:
        raw = call_llm(system, user)
        parsed = extract_json(raw)
        if not parsed:
            raise ValueError("模型返回的不是合法 JSON")
        return {
            "root_cause": parsed.get("root_cause", "（模型未给出根因）"),
            "suggestions": parsed.get("suggestions", []),
            "severity": parsed.get("severity", "medium"),
            "confidence": parsed.get("confidence", 0.8),
            "mode": "llm",
        }
    except Exception as err:
        # 调用失败不要中断主流程，降级到规则库，保证系统健壮
        print(f"[LLM] 调用失败（{err}），已降级为规则诊断")
        result = mock_diagnose(log, source, service)
        result["mode"] = "llm-fallback"
        return result
