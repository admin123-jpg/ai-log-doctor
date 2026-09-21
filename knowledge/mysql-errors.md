# MySQL 常见错误排查手册

## Too many connections 连接数耗尽

**现象**：应用报 `Too many connections`，MySQL 日志出现 `Connection pool exhausted`，新连接被拒绝。

**常见原因**：
1. 应用没有正确关闭数据库连接，连接泄漏。
2. 应用连接池配置过大，超过了 MySQL 的 max_connections。
3. 存在慢查询，导致连接长时间被占用不释放。

**排查步骤**：
- `SHOW VARIABLES LIKE 'max_connections';` 查看最大连接数（默认 151）
- `SHOW STATUS LIKE 'Threads_connected';` 查看当前连接数
- `SHOW PROCESSLIST;` 查看哪些连接在执行什么，是否有大量 Sleep 状态

**解决方案**：
- 临时提高：`SET GLOBAL max_connections = 500;`（重启失效）
- 永久生效：在 my.cnf 的 `[mysqld]` 加 `max_connections = 500`
- 根本解决：修复应用里未关闭连接的代码，使用连接池并配置合理上限。
- 设置 `wait_timeout`（如 300 秒）自动回收空闲连接。

## Access denied for user 认证失败

**现象**：`ERROR 1045 (28000): Access denied for user 'root'@'172.18.0.3' (using password: YES)`

**常见原因**：
1. 用户名或密码错误。
2. 该用户没有被授权从当前主机（IP）访问，MySQL 的用户是 `'用户名'@'主机名'` 组合。
3. 环境变量传递有问题，密码带了特殊字符未转义。

**排查步骤**：
- 在 MySQL 里执行 `SELECT user, host FROM mysql.user;` 确认允许访问的 host
- 用 `docker exec -it <mysql容器> mysql -uroot -p` 手动验证密码

**解决方案**：
- 授权远程访问：`GRANT ALL PRIVILEGES ON *.* TO 'user'@'%' IDENTIFIED BY 'password'; FLUSH PRIVILEGES;`
- 检查 docker-compose.yml 里 MYSQL_PASSWORD 等环境变量是否拼写正确。

## Can't create/write to file 数据目录权限问题

**现象**：`Failed to start mysqld: Can't create/write to file '/var/lib/mysql/ibdata1' (OS errno 13 - Permission denied)`，MySQL 反复重启。

**常见原因**：
1. 挂载的宿主机数据目录属主不是 mysql 用户（容器内 mysql 的 UID 通常是 999）。
2. 目录权限过严。

**排查步骤**：
- `ls -ln /path/to/mysql/data` 查看属主 UID
- `docker logs <mysql容器>` 看完整错误

**解决方案**：
- 宿主机执行 `chown -R 999:999 <数据目录>`（999 是官方镜像 mysql 用户的 UID）
- 或者 `chmod -R 777 <数据目录>`（仅测试环境使用）

## No space left on device 磁盘满

**现象**：`InnoDB: Operating system error number 28`，`Error number 28 means 'No space left on device'`，写入失败。

**常见原因**：
1. 数据盘写满。
2. binlog 或慢日志长期未清理。
3. 大表或临时文件占满空间。

**排查步骤**：
- `df -h` 查看磁盘
- `du -sh /var/lib/mysql/*` 找大文件
- `SHOW BINARY LOGS;` 查看 binlog 占用

**解决方案**：
- 清理 binlog：`PURGE BINARY LOGS BEFORE '2026-09-01';`
- 设置自动过期：`SET GLOBAL binlog_expire_logs_seconds = 604800;`（保留 7 天）
- 清理无用数据和日志，或扩容磁盘。

## Slow query 慢查询

**现象**：日志出现 `Slow query` 并标注 duration，例如 `duration: 12.453s, rows_examined: 3845213`，接口响应变慢。

**常见原因**：
1. 缺少合适的索引，导致全表扫描（rows_examined 远大于返回行数）。
2. SQL 写法问题：`SELECT *`、在 WHERE 里用函数、隐式类型转换。
3. 表数据量增长后统计信息过期。

**排查步骤**：
- 开启慢日志：`SET GLOBAL slow_query_log = 'ON'; SET GLOBAL long_query_time = 1;`
- 用 `EXPLAIN SELECT ...` 分析执行计划，重点看 type（ALL 表示全表扫描）和 rows
- `SHOW INDEX FROM <表名>;` 查看现有索引

**解决方案**：
- 给 WHERE / JOIN / ORDER BY 涉及的列加索引。
- 避免 `SELECT *`，只查需要的列。
- 大表分页改用基于主键的游标分页，避免 `LIMIT 1000000, 20`。

## Deadlock 死锁

**现象**：`Deadlock found when trying to get lock; try restarting transaction`（错误码 1213）。

**常见原因**：多个事务以不同顺序锁定同一批资源，形成循环等待。

**排查步骤**：`SHOW ENGINE INNODB STATUS;` 查看 LATEST DETECTED DEADLOCK 段。

**解决方案**：
- 统一业务里操作表的顺序。
- 缩短事务，不要在事务里做耗时操作（如调用外部接口）。
- 应用层加重试机制，遇到 1213 自动重试。
