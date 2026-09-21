# 中间件常见错误排查手册（Elasticsearch / Kafka / RabbitMQ）

## Elasticsearch 磁盘水位触发只读

**现象**：
`high disk watermark [90%] exceeded`，
`flood stage disk watermark [95%] exceeded ... all indices on this node will be marked read-only`，
写入报 `blocked by: [FORBIDDEN/12/index read-only / allow delete (api)]`。

**常见原因**：
1. 磁盘剩余空间不足，触发 ES 的保护机制。
2. 旧索引未清理，数据持续增长。
3. 磁盘水位线配置过低。

**排查步骤**：
- `GET _cat/allocation?v` 查看各节点磁盘使用
- `GET _cluster/settings` 查看水位线配置
- `GET _cat/indices?v&s=store.size:desc` 找占用大的索引
- 系统层 `df -h` 确认真实磁盘

**解决方案**：
- 清理磁盘：删除旧索引 `DELETE logs-2026.01*`，或用 ILM 自动滚动删除
- 磁盘恢复后**必须手动解除只读**：
  `PUT _all/_settings {"index.blocks.read_only_allow_delete": null}`
- 调整水位线（临时）：
  `PUT _cluster/settings {"transient":{"cluster.routing.allocation.disk.threshold_enabled":true,"cluster.routing.allocation.disk.watermark.low":"85%","cluster.routing.allocation.disk.watermark.high":"90%","cluster.routing.allocation.disk.watermark.flood_stage":"95%"}}`
- 长期方案：配置 ILM 生命周期策略 + 扩容磁盘

## Elasticsearch 熔断 CircuitBreakingException

**现象**：
`[parent] Data too large, data for [<http_request>] would be [11.6gb], which is larger than the limit of [11.3gb]`，
`CircuitBreakingException`，ES 进程可能直接退出。

**常见原因**：
1. 单个聚合/查询涉及的数据量过大（如对高基数字段做 terms 聚合）。
2. 并发大查询叠加，堆内存不够。
3. 堆内存配置不当（超过 31GB 会失去指针压缩，反而更耗内存）。

**排查步骤**：
- `GET _nodes/stats/breaker` 查看各断路器当前使用和上限
- `GET _tasks?detailed=true&actions=*search` 找出正在跑的重查询
- `GET _cat/thread_pool/search?v` 看搜索队列是否堆积
- 检查慢查询日志

**解决方案**：
- 优化查询：避免高基数字段的大范围 terms 聚合，加 query 条件缩小范围
- 分页改 `search_after` 或 `scroll`，避免深分页 `from=10000`
- 堆内存配置：不超过物理内存 50%，且不超过 31GB
- 调大 parent 断路器（谨慎，只是缓解）：`indices.breaker.total.limit`
- 加查询限流和超时

## Elasticsearch GC 频繁与 OOM

**现象**：
`[gc][2143] overhead, spent [12.4s] collecting in the last [13.1s]`，
最终 `java.lang.OutOfMemoryError: Java heap space`。

**常见原因**：
1. 堆内存不足或配置不合理。
2. 大量聚合请求、深分页、大批量写入。
3. Fielddata 或 segment 占用过高。

**排查步骤**：
- `GET _nodes/stats/jvm` 看 heap 使用率和 GC 次数
- `GET _cat/fielddata?v` 查看 fielddata 占用
- `GET _nodes/hot_threads` 找热点线程
- `jstat -gcutil <pid> 1000` 观察

**解决方案**：
- 增加堆内存（不超过 31GB，且留出同样大小给 Lucene 用堆外内存）
- 避免对 text 字段做聚合（改用 keyword）
- 限制 fielddata 大小：`indices.fielddata.cache.size: 20%`
- 定期 force merge 只读索引，减少 segment
- 控制单次批量写入的文档数和体积

## Elasticsearch 分片分配失败

**现象**：
`received shard failed for shard id`，`failed to create shard`，
`Caused by: java.nio.file.FileSystemException: ... No space left on device`，
集群健康变为 yellow / red。

**排查步骤**：
- `GET _cluster/health?pretty` 看状态
- `GET _cat/shards?v&h=index,shard,prirep,state,unassigned.reason` 找未分配分片
- `GET _cluster/allocation/explain?pretty` 看 ES 给出的具体原因（这个最有用）

**解决方案**：
- 磁盘满：清理或扩容
- 分片数过多：`_cluster/settings` 调 `cluster.routing.allocation.total_shards_per_node`，或减分片重建索引
- 节点离线：恢复节点
- 手动重试分配：`POST _cluster/reroute?retry_failed=true`

## Elasticsearch 主节点选举 quorum 不足

**现象**：
`master not discovered or elected yet, an election requires at least 3 nodes`，
集群完全不可用。

**常见原因**：
1. 配置的主节点数量与实际不符。
2. 网络分区导致节点间无法通信。
3. `discovery.seed_hosts` 或 `initial_master_nodes` 配置错误。

**排查步骤**：
- `GET _cat/nodes?v` 看哪些节点在线
- 检查各节点 `elasticsearch.yml` 的 discovery 配置是否一致
- 检查 9300 端口连通性和防火墙
- `GET _cluster/state` 看集群状态

**解决方案**：
- 确保 `initial_master_nodes` 列出的节点数 ≥ quorum（一般为 master 候选数/2 + 1）
- 生产环境 master 候选节点建议 3 或 5 个（奇数）
- 修复网络分区
- 恢复后检查是否有脑裂产生的数据不一致

## Kafka 消费积压与频繁 rebalance

**现象**：消费 lag 持续增长，日志出现 `Attempt to heartbeat failed since group is rebalancing`、
`Commit cannot be completed since the group has rebalanced`。

**常见原因**：
1. 消费者处理速度跟不上生产速度。
2. `max.poll.interval.ms` 设置过小，处理耗时超过它就被踢出组，触发 rebalance。
3. 消费者实例频繁上下线（如容器重启、GC 停顿）。

**排查步骤**：
- `kafka-consumer-groups.sh --describe --group <group>` 看 LAG
- 检查消费者处理单条消息的耗时
- 观察是否有消费者频繁重启

**解决方案**：
- 调大 `max.poll.interval.ms`（如 300000），减小 `max.poll.records` 降低单次处理量
- 增加消费者实例或分区数（注意：消费者数不要超过分区数）
- 优化消费逻辑，或把耗时处理丢到线程池异步做
- 使用静态成员 `group.instance.id` 减少不必要的 rebalance

## RabbitMQ 队列堆积与内存告警

**现象**：管理界面队列 Ready 数持续增长，
日志出现 `memory alarm`、`disk alarm`，生产者被限流（blocked）。

**常见原因**：
1. 消费者处理慢或挂掉。
2. 没有设置队列最大长度，消息无限堆积。
3. 内存/磁盘达到水位触发流控。

**排查步骤**：
- `rabbitmqctl list_queues name messages consumers` 查看堆积
- 管理界面看是否有消费者，unacked 数量是否异常
- `rabbitmqctl status` 看 memory 和 disk 水位

**解决方案**：
- 扩容消费者，或排查消费者为何变慢（可能是下游数据库慢）
- 设置 `x-max-length` 限制队列长度，配合死信队列
- 设置消息 TTL 避免无限堆积
- 调大内存水位 `vm_memory_high_watermark`，但这只是缓解
- 确认消费者有正确的 ack（不要自动 ack 后处理失败导致消息丢失）
