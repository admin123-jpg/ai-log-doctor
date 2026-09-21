#!/bin/bash
# ============================================================
# 安装 Tomcat 到 /tomcat（安装包解压方式，不走 yum）
#   JDK  -> /java   （Tomcat 运行前置）
#   应用 -> /tomcat/webapps/ops-app  （故意写会抛异常的页面）
# 说明：这里所有报错都是 JVM / Tomcat 真实抛出来的，
#       不是往日志文件里手写文本。
# ============================================================
set -e
JDK_PKG=/pkg/jdk-8u202-linux-x64.tar.gz
TC_PKG=/pkg/apache-tomcat-9.0.122.tar.gz

echo "=== [1/6] 解压 JDK 到 /java ==="
rm -rf /java && mkdir -p /java
tar -xzf "$JDK_PKG" -C /java --strip-components=1
/java/bin/java -version 2>&1 | sed 's/^/    /'
echo "    javac: $(/java/bin/javac -version 2>&1)"

echo ""
echo "=== [2/6] 解压 Tomcat 到 /tomcat ==="
rm -rf /tomcat && mkdir -p /tomcat
tar -xzf "$TC_PKG" -C /tomcat --strip-components=1
echo "    CATALINA_HOME = /tomcat"
ls /tomcat | sed 's/^/      /'

echo ""
echo "=== [3/6] 写 setenv.sh（固定 JAVA_HOME + 小堆内存）==="
# 堆故意设小：这样“内存溢出”是真实可触发的，而不是永远等不到
cat > /tomcat/bin/setenv.sh <<'SH'
export JAVA_HOME=/java
export JRE_HOME=/java/jre
export CATALINA_HOME=/tomcat
export CATALINA_BASE=/tomcat
export JAVA_OPTS="-Xms64m -Xmx128m -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tomcat/logs"
export LANG=en_US.UTF-8
SH
chmod +x /tomcat/bin/setenv.sh
cat /tomcat/bin/setenv.sh | sed 's/^/    /'

echo ""
echo "=== [4/6] 开启访问日志（产生真实的 404 / 500 记录）==="
/opt/python311/bin/python3.11 - <<'PY'
import re, pathlib
p = pathlib.Path("/tomcat/conf/server.xml")
s = p.read_text(encoding="utf-8", errors="replace")
if "AccessLogValve" in s and 'directory="logs"' in s and '<!--' not in s.split("AccessLogValve")[0][-200:]:
    print("    已有 AccessLogValve，跳过")
else:
    # 去掉 AccessLogValve 所在注释块
    s = re.sub(r"<!--\s*(<Valve className=\"org\.apache\.catalina\.valves\.AccessLogValve\".*?/>)\s*-->",
               r"\1", s, flags=re.S)
    # 若仍是注释状态（多行注释覆盖），则直接在 Host 后插入一个 Valve
    if "<Valve className=\"org.apache.catalina.valves.AccessLogValve\"" not in s:
        valve = ('\n            <Valve className="org.apache.catalina.valves.AccessLogValve"'
                 ' directory="logs" prefix="localhost_access_log" suffix=".txt"'
                 ' pattern="%h %l %u %t &quot;%r&quot; %s %b %D" resolveHosts="false" />\n')
        s = re.sub(r'(<Host name="localhost"[^>]*>)', r'\1' + valve, s, count=1)
    p.write_text(s, encoding="utf-8")
    print("    已写入 AccessLogValve")
PY
grep -n "AccessLogValve" /tomcat/conf/server.xml | sed 's/^/    /'

echo ""
echo "=== [5/6] 部署会真实报错的应用 ops-app ==="
APP=/tomcat/webapps/ops-app
rm -rf "$APP" && mkdir -p "$APP"

cat > "$APP/index.jsp" <<'JSP'
<%@ page contentType="text/html;charset=UTF-8" %>
<html><head><title>ops-app</title></head><body style="font-family:monospace;padding:24px">
<h3>ops-app（被测应用 · 故意包含会报错的接口）</h3>
<ul>
  <li><a href="normal.jsp">normal.jsp</a> —— 正常页</li>
  <li><a href="npe.jsp">npe.jsp</a> —— 空指针</li>
  <li><a href="throw.jsp">throw.jsp</a> —— 业务异常</li>
  <li><a href="conn.jsp">conn.jsp</a> —— 连接失败</li>
  <li><a href="sql.jsp">sql.jsp</a> —— 驱动缺失</li>
  <li><a href="oom.jsp">oom.jsp</a> —— 内存溢出</li>
  <li><a href="slow.jsp">slow.jsp</a> —— 慢响应（触发 nginx 504）</li>
</ul></body></html>
JSP

cat > "$APP/normal.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
OK normal, server time = <%= new java.util.Date() %>
JSP

cat > "$APP/npe.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
<%
    String s = null;
    if (s.length() > 0) { out.println("never"); }
%>
JSP

cat > "$APP/throw.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
<%
    throw new IllegalStateException("订单服务异常：库存扣减失败，事务已回滚 (traceId=demo-" + System.currentTimeMillis() + ")");
%>
JSP

cat > "$APP/conn.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
<%@ page import="java.net.*, java.io.*" %>
<%
    Socket k = new Socket();
    k.connect(new InetSocketAddress("127.0.0.1", 9999), 2000);
    k.close();
%>
JSP

cat > "$APP/sql.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
<%
    Class.forName("com.mysql.cj.jdbc.Driver");  // 该驱动未打进应用，必然抛异常
    out.println("driver loaded");
%>
JSP

cat > "$APP/oom.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
<%@ page import="java.util.*" %>
<%
    @SuppressWarnings("unchecked")
    List<byte[]> hold = (List<byte[]>) application.getAttribute("_hold");
    if (hold == null) { hold = new ArrayList<byte[]>(); application.setAttribute("_hold", hold); }
    for (int i = 0; i < 300; i++) { hold.add(new byte[1024 * 1024]); }
    out.println("allocated blocks = " + hold.size());
%>
JSP

cat > "$APP/slow.jsp" <<'JSP'
<%@ page contentType="text/plain;charset=UTF-8" %>
<%
    Thread.sleep(15000);   // 故意慢 15 秒
    out.println("slow done");
%>
JSP

echo "    已部署："
ls "$APP" | sed 's/^/      /'

echo ""
echo "=== [6/6] 启动 Tomcat ==="
/tomcat/bin/shutdown.sh 2>/dev/null || true
sleep 2
/tomcat/bin/startup.sh 2>&1 | sed 's/^/    /'

echo "    等待启动..."
for i in $(seq 1 30); do
    if ss -tlnp 2>/dev/null | grep -q ':8080 '; then
        echo "    Tomcat 已监听 8080 ✓（等待 ${i}s）"
        break
    fi
    sleep 1
done
ss -tlnp 2>/dev/null | grep ':8080 ' | sed 's/^/    /' || echo "    ✗ 未监听 8080"

echo ""
echo "=== 冒烟验证 ==="
curl -s -o /dev/null -w "    /            HTTP %{http_code}\n" http://127.0.0.1:8080/
curl -s -o /dev/null -w "    index.jsp    HTTP %{http_code}\n" http://127.0.0.1:8080/ops-app/index.jsp
curl -s -o /dev/null -w "    normal.jsp   HTTP %{http_code}\n" http://127.0.0.1:8080/ops-app/normal.jsp
echo "    日志目录："
ls -l /tomcat/logs/ | sed 's/^/      /'
