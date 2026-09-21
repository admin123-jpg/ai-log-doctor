# Nginx 常见错误排查手册

## 502 Bad Gateway

**现象**：访问返回 502，Nginx 错误日志出现 `connect() failed (111: Connection refused) while connecting to upstream`。

**常见原因**：
1. 后端服务（如 Flask、Java 应用）没有启动或已崩溃。
2. upstream 配置的 IP 或端口写错。
3. 后端服务监听在 `127.0.0.1`，Nginx 容器访问不到。

**排查步骤**：
1. 先看 Nginx 错误日志：`docker logs <nginx容器> | grep error`
2. 确认后端容器是否存活：`docker ps`
3. 在 Nginx 容器里手动测试后端：`docker exec -it <nginx容器> sh` 然后 `wget -qO- http://<后端>:<端口>`

**解决方案**：
- 启动或重启后端服务。
- 检查 nginx.conf 里 `proxy_pass` 的地址是否正确，容器间通信要用服务名而不是 localhost。
- 后端应用监听地址改为 `0.0.0.0`。

## 504 Gateway Timeout

**现象**：访问返回 504，日志出现 `upstream timed out (110: Connection timed out) while reading response header from upstream`。

**常见原因**：
1. 后端处理太慢，超过了 Nginx 的超时时间。
2. 后端出现死锁或慢查询。
3. 后端线程池/连接池耗尽。

**解决方案**：
- 在 nginx.conf 的 location 里调大超时：
  `proxy_connect_timeout 60s; proxy_send_timeout 60s; proxy_read_timeout 60s;`
- 根本解决要优化后端性能：加索引、加缓存、异步化耗时任务。
- 检查后端数据库连接池是否被打满。

## 403 Forbidden 权限拒绝

**现象**：访问返回 403，日志出现 `open() ... failed (13: Permission denied)` 或 `access forbidden by rule`。

**常见原因**：
1. Nginx 工作进程用户（通常是 nginx 或 www-data）对文件/目录没有读权限。
2. 目录没有可执行权限（目录需要 x 权限才能进入）。
3. 配置了 `deny` 规则或 `autoindex off` 且没有索引文件。

**排查步骤**：
- `docker exec -it <nginx容器> sh -c "ps aux | grep nginx"` 看 worker 进程的用户
- `docker exec -it <nginx容器> ls -l /usr/share/nginx/html` 看文件权限

**解决方案**：
- `chmod -R 755 <目录>`、`chown -R nginx:nginx <目录>`
- 检查 nginx.conf 里的 `user` 指令与实际文件属主是否匹配。

## bind() to 0.0.0.0:80 failed 端口占用

**现象**：Nginx 启动失败，日志出现 `bind() to 0.0.0.0:80 failed (98: Address already in use)`，随后 `still could not bind()`。

**常见原因**：
1. 宿主机 80 端口已被其他程序占用（如 IIS、Apache、另一个 Nginx）。
2. 重复启动了同一个容器。

**排查步骤**：
- Windows：`netstat -ano | findstr :80`
- Linux：`ss -tulnp | grep :80`

**解决方案**：
- 停掉占用 80 端口的程序。
- 或修改端口映射，例如 `-p 8080:80`。

## upstream sent too big header 请求头过大

**现象**：502 错误，日志出现 `upstream sent too big header while reading response header from upstream`。

**常见原因**：后端返回的 Cookie 或 Header 太大，超过了 Nginx 默认的缓冲区大小（默认 4k/8k）。

**解决方案**：在 nginx.conf 的 http 或 server 块增加：
```
proxy_buffer_size 128k;
proxy_buffers 4 256k;
proxy_busy_buffers_size 256k;
```

## 静态资源 404

**现象**：页面能打开，但 CSS/JS/图片全部 404。

**常见原因**：
1. `root` 或 `alias` 路径配置错误。
2. 忘记把静态文件挂载进容器。
3. `try_files` 规则把静态请求也转发给了后端。

**解决方案**：
- 检查 docker-compose.yml 的 volumes 挂载路径是否正确。
- 用 `location ~* \.(css|js|png|jpg)$` 单独处理静态资源，并配 `root` 指向正确目录。
