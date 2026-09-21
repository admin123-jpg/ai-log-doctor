# Docker 常见错误排查手册

## 容器间 Connection refused 无法访问

**现象**：日志中出现 `Connection refused`、`[Errno 111]`、`ECONNREFUSED`，A 容器访问 B 容器失败。

**常见原因**：
1. 目标容器还没启动完成，或者已经崩溃退出。
2. 两个容器不在同一个 Docker 网络里。
3. 端口写错了：容器间通信要用「容器内部端口」，不是映射到宿主机的端口。
4. 应用监听地址写成了 `127.0.0.1`，导致只监听容器内部，外部访问不到，应该改成 `0.0.0.0`。

**排查命令**：
- `docker ps -a` 确认目标容器状态是否为 Up
- `docker network ls` 和 `docker network inspect <网络名>` 确认是否同网
- `docker logs <容器名>` 看目标容器自身有没有报错
- `docker exec -it <容器> sh` 进入容器后 `ping <目标容器名>` 测试连通性

**解决方案**：
- 在 docker-compose.yml 里把所有相关服务放到同一个自定义网络。
- 用 `depends_on` 声明启动顺序，并在应用里加重试机制（因为 depends_on 只保证启动，不保证就绪）。
- 应用监听地址改成 `0.0.0.0`。

## 容器 OOMKilled 内存溢出被杀

**现象**：`docker ps -a` 状态显示 `Exited (137)`，日志里出现 `Out of memory`、`OOM`、`MemoryError`。退出码 137 = 128 + 9，表示被 SIGKILL 杀掉。

**常见原因**：
1. 容器内存限制设置得太小。
2. 应用存在内存泄漏，运行时间越长占用越高。
3. 一次性加载了过大的数据到内存（比如大文件、大查询结果）。

**排查命令**：
- `docker inspect <容器名> | grep -i oom` 查看是否被 OOM Killer 杀掉
- `docker stats` 实时查看内存占用
- `docker logs <容器名>` 看崩溃前的最后输出

**解决方案**：
- 在 docker-compose.yml 里用 `deploy.resources.limits.memory` 调大内存限制。
- 优化代码：大数据改用分页 / 流式读取，不要一次性 `SELECT *` 全表。
- 给 JVM 类应用设置合理的堆内存参数（如 `-Xmx`）。

## Permission denied 权限不足

**现象**：日志出现 `Permission denied`、`(OS errno 13)`、`Access denied`。

**常见原因**：
1. 挂载的宿主机目录，容器内运行进程的用户没有读写权限。
2. 文件属主和容器内用户 UID 不匹配。
3. SELinux 开启了强制模式（Linux 宿主机）。

**排查命令**：
- `docker exec -it <容器> id` 查看容器内当前用户和 UID
- `ls -ln <宿主机目录>` 查看目录属主 UID
- `docker inspect <容器> | grep -A5 Mounts` 确认挂载配置

**解决方案**：
- 启动容器时加 `--user <UID>:<GID>` 指定用户。
- 或在宿主机执行 `chown -R 1000:1000 <目录>` 修改属主。
- 或在 docker-compose.yml 的 volume 后面加 `:z` / `:Z`（SELinux 场景）。

## No space left on device 磁盘空间不足

**现象**：`(OS errno 28)`、`No space left on device`，镜像构建或容器写入失败。

**常见原因**：
1. 磁盘真的满了。
2. Docker 的悬空镜像（dangling images）、停止的容器、未使用的卷占满空间。
3. 容器日志无限制增长，单个日志文件达到几十 GB。

**排查命令**：
- `df -h` 查看磁盘剩余空间
- `docker system df` 查看 Docker 各组件占用
- `du -sh /var/lib/docker/containers/*/*-json.log` 找超大的日志文件

**解决方案**：
- `docker system prune -a` 清理无用的镜像、容器、网络（注意会删除未运行的所有容器）。
- `docker volume prune` 清理未使用的卷。
- 配置日志轮转：在 docker-compose.yml 里加 `logging.driver: json-file` 和 `options.max-size: 10m`、`max-file: "3"`。

## port is already allocated 端口被占用

**现象**：启动容器报错 `Bind for 0.0.0.0:8080 failed: port is already allocated`，或 `Address already in use`。

**常见原因**：
1. 宿主机上已有其他进程占用了该端口。
2. 之前启动的同名容器没有清理干净。

**排查命令**：
- Windows：`netstat -ano | findstr :8080` 然后 `tasklist | findstr <PID>`
- Linux：`ss -tulnp | grep :8080` 或 `lsof -i:8080`
- `docker ps -a` 查看是否有占用端口的旧容器

**解决方案**：
- 停掉占用端口的进程或容器。
- 或者修改端口映射，比如把 `8080:80` 改成 `8081:80`。

## 容器启动后立即退出

**现象**：`docker ps -a` 显示状态为 `Exited (0)` 或 `Exited (1)`，容器刚启动就退出。

**常见原因**：
1. 容器的主进程执行完就结束了（比如只执行了 `echo hello`），容器没有前台进程守护。
2. 应用启动报错，进程崩溃。
3. 缺少必要的环境变量或配置文件。

**排查命令**：
- `docker logs <容器名>` 这是最关键的，看退出前打印了什么
- `docker inspect <容器> | grep -A10 State` 查看退出码和错误信息

**解决方案**：
- 确保容器有一个前台常驻进程，比如 `python app.py`、`nginx -g "daemon off;"`。
- 补上缺失的环境变量。
- 临时调试可以用 `docker run -it <镜像> sh` 进入容器手动排查。
