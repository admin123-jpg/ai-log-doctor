# Redis 常见错误排查手册

## MISCONF Redis RDB 持久化失败导致写入被禁

**现象**：写入报 `MISCONF Redis is configured to save RDB snapshots, but it's currently unable to persist to disk`，
日志出现 `Failed opening the RDB file dump.rdb`、`Error trying to save the DB, can't exit`。

**常见原因**：
1. RDB 文件所在目录没有写权限（`Permission denied`）。
2. 磁盘空间不足。
3. 上一次 bgsave 失败后，Redis 按 `stop-writes-on-bgsave-error yes` 的配置主动拒绝写入，保护数据一致性。

**排查步骤**：
- 看完整日志确认是权限还是空间问题：`grep -i "rdb\|save" redis.log`
- 检查目录权限：`ls -l /var/lib/redis`
- 检查磁盘：`df -h`
- Redis 内执行 `CONFIG GET dir` 和 `CONFIG GET dbfilename` 确认落盘路径

**解决方案**：
- 权限问题：`chown -R redis:redis /var/lib/redis && chmod 755 /var/lib/redis`
- 磁盘问题：清理磁盘或扩容
- 紧急恢复写能力（临时）：`CONFIG SET stop-writes-on-bgsave-error no`
- 根治：确保 RDB 目录可写，并监控 bgsave 是否成功

## Can't save in background fork 失败

**现象**：`Can't save in background: fork: Cannot allocate memory`。

**常见原因**：
1. Redis 做 RDB 快照需要 fork 子进程，fork 需要足够的内存（Copy-On-Write 机制下，理论上最坏需要接近父进程的内存）。
2. 系统开启了 `vm.overcommit_memory=0`（启发式），在内存紧张时拒绝 fork。
3. 容器内存限制设置过小，Redis 实际占用已接近上限。

**排查步骤**：
- `INFO memory` 查看 `used_memory`、`maxmemory`、`mem_fragmentation_ratio`
- `cat /proc/sys/vm/overcommit_memory`（0=启发式，1=总是允许，2=禁止超分）
- `docker stats` 看容器内存使用是否接近 limit
- `grep -i fork redis.log`

**解决方案**：
- 设置 `sysctl vm.overcommit_memory=1`，允许 fork：`echo 1 > /proc/sys/vm/overcommit_memory`
- 给 Redis 容器预留 1.5 倍内存（Redis 官方建议 maxmemory 设为容器限制的 70% 左右）
- 关闭 THP（Transparent Huge Pages）：`echo never > /sys/kernel/mm/transparent_hugepage/enabled`
- 如果不需要强持久化，可改用 AOF 或降低 RDB 频率

## maxmemory 达到上限触发淘汰或拒绝写入

**现象**：`maxmemory policy 'noeviction' reached, command SET rejected`，
或 `WARNING memory usage 4021.55 MB, maxmemory 4096.00 MB`，`Evicting keys`。

**常见原因**：
1. 内存达到 maxmemory 上限。
2. 淘汰策略为 `noeviction`，达到上限后直接拒绝写命令。
3. 存在 bigkey 或大量过期 key 未回收。

**排查步骤**：
- `INFO memory` 看 `maxmemory_policy` 和 `used_memory`
- `redis-cli --bigkeys` 找出大 key
- `INFO stats` 看 `evicted_keys` 是否在持续增长
- `MEMORY USAGE <key>` 查看指定 key 占用

**解决方案**：
- 根据业务改淘汰策略：`CONFIG SET maxmemory-policy allkeys-lru`（缓存场景）或 `volatile-lru`
- 调大 `maxmemory`（注意不要超过容器限制的 70-75%）
- 拆分 bigkey，给 key 设置合理 TTL
- 排查是否有内存泄漏（key 只增不减）

## 主从复制断开 Connection with master lost

**现象**：`Unable to connect to MASTER: Connection refused`、`Connection with master lost`、
`MASTER <-> REPLICA sync started` 反复出现。

**常见原因**：
1. 主库未启动、崩溃或重启。
2. 网络不通（防火墙、安全组、容器网络）。
3. 主库设置了 `requirepass`，从库没配 `masterauth`。
4. `repl-backlog-size` 太小，从库断开太久导致全量同步失败循环。

**排查步骤**：
- 从库执行 `INFO replication` 看 `master_link_status`（up/down）
- 从库机器上测试连通：`redis-cli -h <master> -p 6379 ping`
- 主库日志看是否限制了从库连接
- 检查 `masterauth` / `requirepass` 配置是否一致

**解决方案**：
- 确保主库存活且端口可达
- 从库配置 `masterauth <密码>`
- 调大 `repl-backlog-size`（如 256mb）和 `repl-backlog-ttl`（如 3600），减少全量同步
- 全量同步开销大，可设置 `repl-diskless-sync yes` 走网络直传

## maxclients 连接数耗尽

**现象**：`maxclients reached, 10000 client connections`、`Error accepting a client connection: err: Too many open files`。

**常见原因**：
1. 客户端连接泄漏（用完没释放，没用连接池）。
2. `maxclients` 设置过低。
3. 系统 ulimit（文件描述符）限制低于 maxclients。

**排查步骤**：
- `INFO clients` 看 `connected_clients`
- `CLIENT LIST` 看连接的 `idle` 时间，idle 很大的可能是泄漏
- `ulimit -n` 查看文件描述符限制
- `CONFIG GET maxclients`

**解决方案**：
- 客户端改用连接池（如 JedisPool、Lettuce 连接池），避免频繁建连
- 调大 `maxclients`，同时把系统 `ulimit -n` 调到更大（至少 maxclients + 32）
- 设置 `timeout` 参数（如 300）自动断开空闲连接
- 排查慢查询导致连接长期占用

## 慢查询导致延迟升高

**现象**：应用响应变慢，Redis 未报错，但 `SLOWLOG` 里有耗时命令。

**常见原因**：
1. 使用了 `KEYS *`、`HGETALL`、大集合 `SMEMBERS` 等 O(N) 命令。
2. 单次操作 bigkey。
3. 大量 key 同时过期导致阻塞。

**排查步骤**：
- `SLOWLOG GET 20` 查看慢命令
- `redis-cli --bigkeys` 找大 key
- `INFO commandstats` 看各命令调用次数和耗时

**解决方案**：
- 禁用 `KEYS`，改用 `SCAN` 增量遍历
- 大集合拆分，或用 `HSCAN` / `SSCAN` 分批取
- 给过期时间加随机抖动，避免同时过期
- 配置 `slowlog-log-slower-than` 持续监控
