#!/bin/bash
# ============================================================
# 安装 nginx 到 /nginx（安装包解压方式，不走 yum）
#
#   包来源：nginx.org 官方 el7 RPM
#   解压方式：rpm2cpio + cpio，把 RPM 内容铺到 /nginx 下
#
#   注意：nginx 1.23+ 依赖 PCRE2，而 CentOS 7 只自带 PCRE1，
#         所以额外解压一个 pcre2 包补上 libpcre2-8.so.0。
# ============================================================
set -e
PKG=/pkg/nginx-1.24.0-1.el7.ngx.x86_64.rpm
PCRE2=/pkg/pcre2-10.23-2.el7.x86_64.rpm
ROOT=/nginx

echo "=== [1/6] 解压 nginx RPM 到 $ROOT ==="
rm -rf "$ROOT"
mkdir -p "$ROOT"
cd "$ROOT"
rpm2cpio "$PKG" | cpio -idm --quiet
echo "  解开的内容："
find "$ROOT" -maxdepth 3 -type d | sed 's/^/    /'

echo ""
echo "=== [2/6] 补 PCRE2 运行库（CentOS 7 自带的是 PCRE1）==="
mkdir -p "$ROOT/lib"
cd "$ROOT/lib"
rpm2cpio "$PCRE2" | cpio -idm --quiet
# RPM 里路径是 usr/lib64/...，把它挪到 /nginx/lib
find "$ROOT/lib" -name 'libpcre2-8.so*' -exec cp -a {} "$ROOT/lib/" \;
rm -rf "$ROOT/lib/usr"
ls -l "$ROOT/lib/" | sed 's/^/    /'
# 注册到系统库搜索路径（只新增一个目录，可随时删除）
echo "$ROOT/lib" > /etc/ld.so.conf.d/nginx-local.conf
ldconfig
echo "  已注册 $ROOT/lib 到 /etc/ld.so.conf.d/nginx-local.conf"

echo ""
echo "=== [3/6] 检查二进制与动态库依赖 ==="
BIN="$ROOT/usr/sbin/nginx"
ldd "$BIN" | sed 's/^/    /' | grep -E 'not found|pcre2|pcre|ssl|crypto' || true
echo "  版本："
"$BIN" -v 2>&1 | sed 's/^/    /'

echo ""
echo "=== [4/6] 建立统一目录布局 ==="
mkdir -p "$ROOT/conf" "$ROOT/logs" "$ROOT/html" "$ROOT/temp/client_body" \
         "$ROOT/temp/proxy" "$ROOT/temp/fastcgi" "$ROOT/temp/uwsgi" "$ROOT/temp/scgi"
# nginx 启动早期会先按「编译期默认路径」写错误日志，
# 这时还没读我们的配置，所以这两个标准目录必须存在，否则会报警告
mkdir -p /var/log/nginx /var/cache/nginx
# 把 RPM 里的 mime.types 等配置拿过来
[ -d "$ROOT/etc/nginx" ] && cp -rn "$ROOT"/etc/nginx/* "$ROOT/conf/" 2>/dev/null || true
# 静态首页
cat > "$ROOT/html/index.html" <<'HTML'
<!DOCTYPE html><html><head><meta charset="utf-8"><title>nginx OK</title></head>
<body style="font-family:monospace;padding:40px">
<h2>nginx is running</h2>
<p>被监控的中间件之一，日志位于 /nginx/logs/</p>
</body></html>
HTML
# 用于制造「真实文件不存在」和「真实 403」的目录
mkdir -p "$ROOT/html/files" "$ROOT/html/secret"
echo "this file exists" > "$ROOT/html/files/ok.txt"
echo "  已创建目录布局与静态文件"

echo ""
echo "=== [5/6] 生成配置文件（含故意制造的故障 upstream）==="
cat > "$ROOT/conf/nginx.conf" <<'CONF'
# nginx 配置 —— 供 ai-log-doctor 采集日志
# 其中 /api/ 故意指向一个没有服务监听的端口，
# 访问它就能让 nginx 自己产生真实的 502 错误日志。

worker_processes  1;
daemon        on;
user          nobody;
pid           /nginx/logs/nginx.pid;
error_log     /nginx/logs/error.log  warn;

events {
    worker_connections  256;
}

http {
    include       mime.types;
    default_type  application/octet-stream;

    # 注意：这里刻意保留 $remote_user 字段，让日志与「标准 combined 格式」
    # 字段位一致（第 9 列才是状态码），后续过滤 4xx/5xx 才不会错位。
    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" "$http_user_agent" '
                      'rt=$request_time urt=$upstream_response_time';

    access_log  /nginx/logs/access.log  main;

    # ---- 用于制造真实错误的几项设置 ----
    client_max_body_size   1m;      # 超 1MB 的请求体 -> 真实 413
    client_body_timeout    10s;
    proxy_connect_timeout  3s;
    proxy_read_timeout     5s;      # 上游超 5s -> 真实 504
    proxy_send_timeout     5s;
    send_timeout           10s;

    client_body_temp_path  /nginx/temp/client_body;
    proxy_temp_path        /nginx/temp/proxy;
    fastcgi_temp_path      /nginx/temp/fastcgi;
    uwsgi_temp_path        /nginx/temp/uwsgi;
    scgi_temp_path         /nginx/temp/scgi;

    server {
        listen       80;
        server_name  localhost;
        root         /nginx/html;

        # ① 正常静态页
        location / {
            index  index.html;
        }

        # ② 故意配一个不存在的上游服务 -> 真实 502
        #    connect() failed (111: Connection refused) while connecting to upstream
        location /api/ {
            proxy_pass http://127.0.0.1:9999;
            proxy_next_upstream error timeout http_502;
        }

        # ③ 指向本地 Tomcat，配合慢接口 -> 真实 504
        #    upstream timed out (110: Connection timed out)
        location /tomcat/ {
            proxy_pass http://127.0.0.1:8080/;
        }

        # ④ 用 return 直接拒绝 -> 真实 404（记在访问日志）
        location /not-here/ {
            return 404;
        }

        # ⑤ 走真实文件查找 -> 文件不存在时 error.log 写 open() failed
        location /files/ {
            root /nginx/html;
        }

        # ⑥ 访问没有 index 的目录 -> 真实 403
        #    directory index of ... is forbidden
        location /secret/ {
            root /nginx/html;
        }

        # ⑦ nginx 自身状态
        location /nginx-status {
            stub_status on;
            access_log off;
        }
    }
}
CONF

echo ""
echo "=== [6/6] 语法检查并启动 ==="
"$BIN" -t -c "$ROOT/conf/nginx.conf" 2>&1 | sed 's/^/    /'
"$BIN" -c "$ROOT/conf/nginx.conf"
sleep 2

echo ""
echo "=== 结果 ==="
if ss -tlnp 2>/dev/null | grep -q ':80 '; then
    ss -tlnp | grep ':80 ' | sed 's/^/  /'
    echo "  nginx 已监听 80 端口 ✓"
else
    echo "  ✗ nginx 未监听，看错误日志："
    tail -20 "$ROOT/logs/error.log" 2>/dev/null | sed 's/^/    /'
fi
"$BIN" -v 2>&1 | sed 's/^/  版本: /'
echo "  日志目录: $ROOT/logs/"
ls -l "$ROOT/logs/" | sed 's/^/    /'
echo ""
echo "  冒烟测试:"
curl -s -o /dev/null -w "    /            HTTP %{http_code}\n" http://127.0.0.1/
curl -s -o /dev/null -w "    /nginx-status HTTP %{http_code}\n" http://127.0.0.1/nginx-status
