# Tomcat / Java 应用常见错误排查手册

## OutOfMemoryError: Java heap space 堆内存溢出

**现象**：日志出现 `java.lang.OutOfMemoryError: Java heap space`，
通常伴随 `Exception in thread "http-nio-8080-exec-xxx"`，应用无响应或自动重启。

**常见原因**：
1. 堆内存（`-Xmx`）设置过小，业务高峰期不够用。
2. 内存泄漏：静态集合只增不减、未关闭的连接/流、ThreadLocal 未清理。
3. 一次性处理超大数据（如全表导出、大文件读入内存）。

**排查步骤**：
- 看堆栈定位到具体类和方法（如 `ReportService.exportAllOrders:156`）
- 加 JVM 参数 `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp` 保留现场
- 用 MAT（Memory Analyzer Tool）或 `jvisualvm` 分析 dump 文件，找占用最大的对象
- `jmap -histo <pid> | head -20` 实时查看对象数量排行
- `jstat -gcutil <pid> 1000` 观察 GC 频率和各区占用

**解决方案**：
- 调大堆内存：`-Xms2g -Xmx2g`（建议 -Xms 和 -Xmx 设成一样，避免动态扩容抖动）
- 容器环境注意：JVM 堆不要超过容器 limit 的 70%（还要留给元空间、线程栈、堆外内存）
- 修复代码问题：大数据改分页/流式处理（`LIMIT` 分批），及时 close 资源
- 检查是否有静态 Map/List 无限制增长

## GC overhead limit exceeded GC 开销超限

**现象**：`java.lang.OutOfMemoryError: GC overhead limit exceeded`。

**常见原因**：
JVM 花在 GC 上的时间超过 98%，但每次只能回收不到 2% 的堆 —— 说明堆几乎满了且全是存活对象。

**排查步骤**：
- `jstat -gcutil <pid> 1000` 观察 FGC 次数是否暴涨
- 抓 heap dump 分析存活对象
- 检查是否有大量缓存对象未设置过期

**解决方案**：
- 调大堆内存
- 换用 G1 收集器：`-XX:+UseG1GC -XX:MaxGCPauseMillis=200`
- 排查内存泄漏（最常见是本地缓存无上限）
- 如果是缓存问题，改用 Caffeine/Redis 并设置最大容量和过期策略

## Servlet.service() threw exception 业务异常

**现象**：`SEVERE ... Servlet.service() for servlet [dispatcherServlet] threw exception`
伴随 `java.lang.NullPointerException` 等堆栈。

**常见原因**：
1. 空指针：对象未判空就调用方法（如 `user.getUsername()` 而 user 为 null）。
2. 参数校验缺失，上游传了意外的值。
3. 依赖的服务返回了 null 或异常结构。

**排查步骤**：
- 看堆栈最上面那行 `at com.xxx.Controller.method(Class.java:行号)`，直接定位代码行
- Caused by 段落通常会指出真正的原因
- 复现请求并打印入参

**解决方案**：
- 对可能为 null 的对象加判空或用 `Optional`
- 接口入口加参数校验（`@Valid` + `@NotNull`）
- 全局异常处理器统一兜底，返回规范错误码而不是 500 堆栈

## 数据库连接池耗尽

**现象**：
`Connection pool exhausted, waited 30000 ms for connection`、
`HikariPool-1 - Connection is not available, request timed out after 30000ms`、
`Unable to borrow connection from pool`。

**常见原因**：
1. 连接池最大连接数配置过小。
2. 慢 SQL 长期占用连接不释放。
3. 事务未正确提交/回滚，连接泄漏。
4. 数据库本身连接数打满（MySQL `Too many connections`）。

**排查步骤**：
- 看应用日志确认是哪个接口耗时
- 数据库侧 `SHOW PROCESSLIST` 看是否有大量 Sleep 或慢查询
- 检查连接池监控指标（active / idle / waiting）
- 确认事务边界是否正确（`@Transactional` 是否覆盖了耗时操作）

**解决方案**：
- 调大连接池（HikariCP `maximum-pool-size`，建议 = CPU核数 × 2 + 磁盘数）
- 优化慢 SQL，加索引
- 缩短事务范围，不要在事务里调外部接口
- 确保连接 finally 中关闭，或使用 try-with-resources
- 设置 `connection-timeout` 快速失败而非无限等待

## Too many open files 文件句柄耗尽

**现象**：`java.net.SocketException: Too many open files`，
或 `org.apache.tomcat.util.net.Acceptor.run Socket accept failed`。

**常见原因**：
1. 系统 ulimit 设置过低（默认 1024）。
2. 连接、文件流未关闭导致句柄泄漏。
3. 高并发场景下连接数超过句柄限制。

**排查步骤**：
- `ulimit -n` 查看当前限制
- `lsof -p <pid> | wc -l` 看进程实际打开的句柄数
- `lsof -p <pid> | awk '{print $NF}' | sort | uniq -c | sort -rn | head` 看哪类句柄最多

**解决方案**：
- 调大限制：`ulimit -n 65535`，并在 `/etc/security/limits.conf` 永久配置
- 容器环境：Docker 加 `--ulimit nofile=65535:65535`
- 修复代码中未关闭的流和连接（用 try-with-resources）
- 检查 HTTP 客户端是否复用连接

## 线程池满拒绝请求

**现象**：请求被拒绝，日志出现 `RejectedExecutionException` 或 `Task rejected`，
Tomcat 侧表现为 `All threads (200) are currently busy`。

**常见原因**：
1. 线程池核心/最大线程数配置过小。
2. 下游服务慢，线程被长时间占用。
3. 突发流量超过处理能力。

**排查步骤**：
- 开启 Tomcat 线程池监控：`jconsole` 或 Spring Boot Actuator
- `jstack <pid> > thread.txt` 抓线程栈，看线程卡在哪
- 分析线程状态：大量 `WAITING` 说明在等下游，`RUNNABLE` 集中在某方法说明有慢逻辑

**解决方案**：
- 调大 `server.tomcat.threads.max`（默认 200）
- 给下游调用设置超时，避免线程被无限占用
- 加限流和降级（Sentinel / Resilience4j）
- 异步化耗时任务

## Tomcat 启动失败端口被占用

**现象**：`Failed to initialize connector [Connector[HTTP/1.1-8080]]`，
`Address already in use` 或 `BindException`。

**常见原因**：
1. 端口已被另一个进程占用。
2. 上次 Tomcat 未正常关闭，端口处于 TIME_WAIT。

**排查步骤**：
- `netstat -tulnp | grep 8080` 或 `lsof -i:8080`
- Windows：`netstat -ano | findstr :8080`

**解决方案**：
- 停掉占用进程，或修改 `server.xml` 的 `<Connector port="8080">`
- 确认没有重复部署同一个应用
