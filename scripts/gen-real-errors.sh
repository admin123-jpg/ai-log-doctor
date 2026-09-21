#!/bin/bash
# ============================================================
# 在 nginx / tomcat / mysql 上制造「真实的」错误日志
#
# 原理：不往日志文件里写一个字符。
#       全部通过发起真实请求、触发真实故障，让服务自己记录。
#       - nginx   : 访问不存在的上游 / 缺失文件 / 超大请求体 / 慢上游
#       - tomcat  : 访问会抛异常的 JSP，由 JVM 真实抛出异常栈
#       - mysql   : 错误口令登录、真实死锁、连接中断
# ============================================================
NGX=/nginx/usr/sbin/nginx
NGX_CONF=/nginx/conf/nginx.conf
STAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "############################################################"
echo "# 真实错误生成  $STAMP"
echo "############################################################"

echo ""
echo "========== 一、nginx 真实错误 =========="
echo ""

echo "[1] 访问一个没有服务监听的 upstream（9999 端口）"
echo "    -> 预期 nginx 自己写出 connect() failed ... Connection refused，返回 502"
for i in 1 2 3; do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/api/health)
    printf "    第%d次: HTTP %s\n" "$i" "$code"
done

echo ""
echo "[2] 请求一个不存在的静态文件"
echo "    -> 预期 error.log 出现 open() failed (2: No such file or directory)，返回 404"
for f in report-2026.pdf data.csv missing.json; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/files/$f")
    printf "    /files/%-16s HTTP %s\n" "$f" "$code"
done

echo ""
echo "[3] 访问一个没有 index 的目录"
echo "    -> 预期 error.log 出现 directory index of ... is forbidden，返回 403"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/secret/)
printf "    /secret/            HTTP %s\n" "$code"

echo ""
echo "[4] 上传超过 client_max_body_size(1m) 的请求体"
echo "    -> 预期 error.log 出现 client intended to send too large body，返回 413"
dd if=/dev/zero of=/tmp/big.bin bs=1M count=2 2>/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST --data-binary @/tmp/big.bin http://127.0.0.1/)
printf "    POST 2MB            HTTP %s\n" "$code"
rm -f /tmp/big.bin

echo ""
echo "[5] 通过 nginx 访问一个耗时 15 秒的接口（proxy_read_timeout=5s）"
echo "    -> 预期 nginx 写出 upstream timed out (110)，返回 504；同时 Tomcat 侧正常处理完"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 http://127.0.0.1/tomcat/ops-app/slow.jsp)
printf "    slow.jsp via nginx  HTTP %s\n" "$code"

echo ""
echo "[6] 访问一个 nginx 用 return 直接拒绝的路径"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/not-here/page)
printf "    /not-here/page      HTTP %s\n" "$code"

echo ""
echo "========== 二、Tomcat 真实错误（JVM 抛出的异常栈）=========="
echo ""
TC=http://127.0.0.1:8080/ops-app

echo "[1] 空指针 npe.jsp"
printf "    HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' $TC/npe.jsp)"

echo "[2] 业务异常 throw.jsp（自定义异常 + 消息）"
printf "    HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' $TC/throw.jsp)"

echo "[3] 连接失败 conn.jsp（连 9999 端口）"
printf "    HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' $TC/conn.jsp)"

echo "[4] 驱动缺失 sql.jsp（ClassNotFoundException）"
printf "    HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' $TC/sql.jsp)"

echo "[5] 页面不存在（真实 404）"
printf "    HTTP %s\n" "$(curl -s -o /dev/null -w '%{http_code}' $TC/does-not-exist.jsp)"

echo "[6] 内存溢出 oom.jsp（-Xmx128m，会真实 OOM）"
for i in 1 2 3; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 40 $TC/oom.jsp)
    printf "    第%d次: HTTP %s\n" "$i" "$code"
done

echo ""
echo "========== 三、MySQL 真实错误（现有 5.7.44 实例）=========="
echo ""
MYSQL=/usr/bin/mysql

echo "[1] 用错误口令登录（真实 Access denied，会记入 mysqld 错误日志）"
for u in baduser hacker sa; do
    $MYSQL -h 127.0.0.1 -P 3306 -u "$u" -p'wrong_password_123' -e "SELECT 1" >/dev/null 2>&1
    printf "    %-8s 登录尝试完成\n" "$u"
done

echo "[2] 建立独立测试库并制造真实死锁"
$MYSQL -uroot <<'SQL' >/dev/null 2>&1 || echo "    (root 免密不可用，跳过死锁)"
CREATE DATABASE IF NOT EXISTS ops_doctor_test DEFAULT CHARSET utf8mb4;
USE ops_doctor_test;
CREATE TABLE IF NOT EXISTS stock (
  id INT PRIMARY KEY,
  qty INT NOT NULL
) ENGINE=InnoDB;
INSERT IGNORE INTO stock VALUES (1, 100), (2, 200);
SQL

# 用两个连接交叉加锁，制造真实死锁
$MYSQL -uroot -e "USE ops_doctor_test; BEGIN; SELECT * FROM stock WHERE id=1 FOR UPDATE; SELECT SLEEP(3); SELECT * FROM stock WHERE id=2 FOR UPDATE; COMMIT;" >/dev/null 2>&1 &
P1=$!
sleep 1
$MYSQL -uroot -e "USE ops_doctor_test; BEGIN; SELECT * FROM stock WHERE id=2 FOR UPDATE; SELECT SLEEP(3); SELECT * FROM stock WHERE id=1 FOR UPDATE; COMMIT;" 2>&1 | head -3 | sed 's/^/    /'
wait $P1 2>/dev/null
echo "    死锁测试完成"

echo "[3] 制造异常连接中断（Aborted connection）"
for i in 1 2 3 4 5; do
    timeout 1 $MYSQL -uroot -e "SELECT SLEEP(30)" >/dev/null 2>&1
done
echo "    5 次连接被强制中断"

echo "[4] 访问不存在的库 / 表"
$MYSQL -uroot -e "USE no_such_database;" 2>&1 | head -1 | sed 's/^/    /'
$MYSQL -uroot -e "USE ops_doctor_test; SELECT * FROM no_such_table;" 2>&1 | head -1 | sed 's/^/    /'

echo ""
echo "========== 四、生成结果统计 =========="
echo ""
for f in /nginx/logs/error.log /nginx/logs/access.log; do
    echo "  $(printf '%-28s' $f) $([ -f $f ] && wc -l < $f || echo 0) 行"
done
for f in /tomcat/logs/catalina.out /tomcat/logs/localhost_access_log*.txt; do
    [ -f $f ] && echo "  $(printf '%-28s' $f) $(wc -l < $f) 行"
done
echo "  $(printf '%-28s' /var/log/mysqld.log) $(wc -l < /var/log/mysqld.log) 行"

echo ""
echo "========== 五、抽样验证：日志里确实是真实报错 =========="
echo ""
echo "--- nginx error.log 最后 6 行 ---"
tail -6 /nginx/logs/error.log 2>/dev/null | cut -c1-150 | sed 's/^/  /'

echo ""
echo "--- tomcat catalina.out 里的异常类型统计 ---"
grep -oE '(java|javax|org)\.[a-zA-Z.]*(Exception|Error)' /tomcat/logs/catalina.out 2>/dev/null \
  | sort | uniq -c | sort -rn | head -8 | sed 's/^/  /'
