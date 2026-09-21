#!/bin/bash
# ============================================================
# 采集本机真实中间件日志 -> /opt/logs
#
# 采集对象（全部由服务自己写出，无人工伪造）：
#   nginx   : /nginx/logs/          （RPM 解压安装，端口 80）
#   tomcat  : /tomcat/logs/         （tarball 解压安装，端口 8080）
#   mysql   : /var/log/mysqld.log   （系统已有实例，端口 3306）
#   系统层  : journalctl / dmesg
#
# 三个设计要点：
#   1. 按「错误类型」拆成独立文件，而不是把不同故障混在一个文件里。
#      原因：程序切分日志时会取上下文，混在一起会把无关错误一起卷进来，
#            导致结论被污染（这个问题在之前用真实日志时已经踩过）。
#   2. 访问日志只保留 4xx/5xx —— 正常请求对诊断没有价值。
#   3. 文件名带中间件名，程序据此自动推断 service。
#
# 用法： bash collect-all-logs.sh [每文件最多行数，默认 800]
# ============================================================
set -u
MAX=${1:-800}
OUT=/opt/logs
mkdir -p "$OUT"
rm -f "$OUT"/*.log "$OUT"/*.out "$OUT"/*.txt 2>/dev/null

echo "========================================================"
echo "  采集真实日志  $(date '+%Y-%m-%d %H:%M:%S')   上限 ${MAX} 行/文件"
echo "========================================================"

# ---------------------------------------------------------------
echo ""
echo "[1/4] nginx  （/nginx/logs/）"
if [ -s /nginx/logs/error.log ]; then
    tail -n "$MAX" /nginx/logs/error.log > "$OUT/nginx-error.log"
    printf "      nginx-error.log        %5s 行\n" "$(wc -l < "$OUT/nginx-error.log")"
fi
if [ -s /nginx/logs/access.log ]; then
    awk '$9 ~ /^[45][0-9][0-9]$/' /nginx/logs/access.log | tail -n "$MAX" > "$OUT/nginx-access.log"
    n=$(wc -l < "$OUT/nginx-access.log")
    if [ "$n" -gt 0 ]; then
        printf "      nginx-access.log       %5s 行（仅 4xx/5xx）\n" "$n"
    else
        rm -f "$OUT/nginx-access.log"
    fi
fi

# ---------------------------------------------------------------
echo ""
echo "[2/4] tomcat （/tomcat/logs/）"
# ① 容器级日志：JVM 异常栈、启动信息
if [ -s /tomcat/logs/catalina.out ]; then
    tail -n "$MAX" /tomcat/logs/catalina.out > "$OUT/tomcat-catalina.out"
    printf "      tomcat-catalina.out    %5s 行\n" "$(wc -l < "$OUT/tomcat-catalina.out")"
fi
# ② 应用级异常栈（juli 的 localhost 日志，只留异常段落）
LOC=$(ls /tomcat/logs/localhost.[0-9]*.log 2>/dev/null | tail -1)
if [ -n "${LOC:-}" ] && [ -s "$LOC" ]; then
    grep -nE 'SEVERE|Exception|Error|Caused by' "$LOC" >/dev/null 2>&1 && \
        tail -n "$MAX" "$LOC" > "$OUT/tomcat-app.log"
    printf "      tomcat-app.log         %5s 行\n" "$(wc -l < "$OUT/tomcat-app.log")"
fi
# ③ 访问日志：真实的 404 / 500
ACC=$(ls /tomcat/logs/localhost_access_log*.txt 2>/dev/null | tail -1)
if [ -n "${ACC:-}" ] && [ -s "$ACC" ]; then
    awk '$9 ~ /^[45][0-9][0-9]$/' "$ACC" | tail -n "$MAX" > "$OUT/tomcat-access.log"
    n=$(wc -l < "$OUT/tomcat-access.log")
    if [ "$n" -gt 0 ]; then
        printf "      tomcat-access.log      %5s 行（仅 4xx/5xx）\n" "$n"
    else
        rm -f "$OUT/tomcat-access.log"
    fi
fi

# ---------------------------------------------------------------
echo ""
echo "[3/4] mysql （/var/log/mysqld.log）"
if [ -s /var/log/mysqld.log ]; then
    # 按级别拆开：ERROR 和 Warning 是两类不同的故障，混在一起会互相干扰
    grep '\[ERROR\]'   /var/log/mysqld.log | tail -n "$MAX" > "$OUT/mysql-error.log"
    grep '\[Warning\]' /var/log/mysqld.log | tail -n "$MAX" > "$OUT/mysql-warning.log"
    printf "      mysql-error.log        %5s 行\n" "$(wc -l < "$OUT/mysql-error.log")"
    printf "      mysql-warning.log      %5s 行\n" "$(wc -l < "$OUT/mysql-warning.log")"
fi

# ---------------------------------------------------------------
echo ""
echo "[4/4] 系统层"
if command -v journalctl >/dev/null 2>&1; then
    journalctl -p err -n "$MAX" --no-pager 2>/dev/null | grep -vE '^-- |^$' > "$OUT/systemd-error.log"
    printf "      systemd-error.log      %5s 行\n" "$(wc -l < "$OUT/systemd-error.log")"
fi
dmesg 2>/dev/null | tail -n 300 > "$OUT/kernel-dmesg.log"
printf "      kernel-dmesg.log       %5s 行\n" "$(wc -l < "$OUT/kernel-dmesg.log")"

# ---------------------------------------------------------------
echo ""
echo "=================== 采集结果 ==================="
printf "  %-24s %6s %8s %8s\n" "文件" "行数" "大小" "错误词"
printf "  %-24s %6s %8s %8s\n" "------------------------" "------" "--------" "--------"
for f in "$OUT"/*; do
    [ -f "$f" ] || continue
    n=$(grep -ciE 'error|exception|failed|refused|timeout|denied|forbidden|out of memory|panic|fatal|abort' "$f" 2>/dev/null || echo 0)
    printf "  %-24s %6s %8s %8s\n" "$(basename "$f")" "$(wc -l < "$f")" "$(du -h "$f" | cut -f1)" "$n"
done
echo ""
echo "  合计 $(cat "$OUT"/* 2>/dev/null | wc -l) 行 / $(du -sh "$OUT" | cut -f1)"
echo ""
echo "  按中间件分组："
for svc in nginx tomcat mysql systemd kernel; do
    cnt=$(ls "$OUT"/${svc}* 2>/dev/null | wc -l)
    [ "$cnt" -gt 0 ] && printf "    %-10s %d 个文件\n" "$svc" "$cnt"
done
