#!/bin/sh
# ==========================================================
# AI 容器日志医生 —— 启动 / 停止 / 重启脚本
#
# 用法：
#   sh exec.sh start      启动服务
#   sh exec.sh stop       停止服务
#   sh exec.sh restart    重启服务
#   sh exec.sh status     查看状态（含接口健康检查）
#   sh exec.sh logs       实时查看日志
#
# 设计说明（为什么要「双模式」）：
#   在服务器上，服务由 systemd 管理（开机自启、崩溃自动重启），
#   所以脚本优先调用 systemctl。
#   如果目标机器没有 systemd（比如容器里、或者 Mac），
#   就自动降级为 nohup + PID 文件的方式管理进程。
#   两条路径的命令完全一样，用起来没有区别。
# ==========================================================

set -u

# ---------- 基本路径（自动定位到脚本所在目录，所以放在哪都能跑）----------
APP_DIR=$(cd "$(dirname "$0")" && pwd)
SERVICE_NAME="ai-log-doctor"
PID_FILE="$APP_DIR/data/app.pid"
LOG_FILE="$APP_DIR/data/app.log"

# ---------- 从 .env 读取端口配置（读不到就用默认值）----------
read_env() {
    _key="$1"
    _default="$2"
    _val=$(grep -E "^${_key}=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- \
           | tr -d '"' | tr -d "'" | tr -d ' \r')
    if [ -n "$_val" ]; then
        printf '%s' "$_val"
    else
        printf '%s' "$_default"
    fi
}

API_HOST=$(read_env API_HOST "127.0.0.1")
API_PORT=$(read_env API_PORT "8000")

# 监听 127.0.0.1 时，健康检查也用 127.0.0.1（0.0.0.0 不是可连接的地址）
CHECK_HOST="$API_HOST"
[ "$CHECK_HOST" = "0.0.0.0" ] && CHECK_HOST="127.0.0.1"

# ---------- 找 Python 解释器（兼容 Linux 和 Windows 两种目录结构）----------
if [ -x "$APP_DIR/venv/bin/python" ]; then
    PYTHON="$APP_DIR/venv/bin/python"
elif [ -x "$APP_DIR/venv/Scripts/python.exe" ]; then
    PYTHON="$APP_DIR/venv/Scripts/python.exe"
else
    PYTHON=""
fi

# ---------- 判断用哪种管理方式 ----------
USE_SYSTEMD="no"
if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\.service"; then
        USE_SYSTEMD="yes"
    fi
fi

# ---------- 取进程 PID（用于降级模式）----------
get_pid() {
    if [ -f "$PID_FILE" ]; then
        _p=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null; then
            printf '%s' "$_p"
            return 0
        fi
        rm -f "$PID_FILE" 2>/dev/null
    fi
    return 1
}

# ---------- 等待服务就绪（最多等 N 秒）----------
wait_ready() {
    _i=0
    _max=$1
    while [ "$_i" -lt "$_max" ]; do
        if command -v curl >/dev/null 2>&1; then
            if curl -s -o /dev/null --max-time 2 "http://${CHECK_HOST}:${API_PORT}/api/health" 2>/dev/null; then
                return 0
            fi
        else
            # 没有 curl 就用 python 探测端口
            [ -n "$PYTHON" ] && "$PYTHON" -c "
import socket,sys
s=socket.socket(); s.settimeout(1)
try: s.connect(('$CHECK_HOST', $API_PORT))
except Exception: sys.exit(1)
finally: s.close()
" 2>/dev/null && return 0
        fi
        sleep 1
        _i=$((_i + 1))
    done
    return 1
}

# ============================ start ============================
do_start() {
    echo "[start] 启动 $SERVICE_NAME ..."

    if [ "$USE_SYSTEMD" = "yes" ]; then
        if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
            echo "        服务已在运行，无需启动"
            do_status
            return 0
        fi
        systemctl start "$SERVICE_NAME" || { echo "        启动失败！用 journalctl -u $SERVICE_NAME -n 50 查看原因"; return 1; }
    else
        echo "        （未检测到 systemd 服务，使用 nohup + PID 文件方式）"
        if get_pid >/dev/null; then
            echo "        服务已在运行（PID $(get_pid)），无需启动"
            return 0
        fi
        if [ -z "$PYTHON" ]; then
            echo "        错误：找不到虚拟环境里的 Python。请先执行："
            echo "          python -m venv venv && venv/bin/pip install -r requirements.txt"
            return 1
        fi
        mkdir -p "$APP_DIR/data"
        cd "$APP_DIR" || return 1
        nohup "$PYTHON" -m src.main serve --host "$API_HOST" --port "$API_PORT" \
              >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "        已启动，PID $(cat "$PID_FILE")，日志：$LOG_FILE"
    fi

    printf "        等待服务就绪"
    if wait_ready 20; then
        echo " ... 就绪 ✓"
        echo ""
        echo "        前端面板：http://<本机IP>:${API_PORT}/"
        echo "        接口文档：http://<本机IP>:${API_PORT}/docs"
    else
        echo " ... 超时 ✗"
        echo "        服务可能启动失败，查看日志："
        if [ "$USE_SYSTEMD" = "yes" ]; then
            echo "          journalctl -u $SERVICE_NAME -n 50"
        else
            echo "          tail -50 $LOG_FILE"
        fi
        return 1
    fi
    return 0
}

# ============================ stop ============================
do_stop() {
    echo "[stop] 停止 $SERVICE_NAME ..."

    if [ "$USE_SYSTEMD" = "yes" ]; then
        if ! systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
            echo "        服务本来就没在运行"
            return 0
        fi
        systemctl stop "$SERVICE_NAME" && echo "        已停止 ✓"
        return 0
    fi

    PID=$(get_pid) || { echo "        没有正在运行的进程（无 PID 文件）"; return 0; }
    echo "        正在停止 PID $PID ..."
    kill "$PID" 2>/dev/null

    _i=0
    while [ "$_i" -lt 10 ]; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 1
        _i=$((_i + 1))
    done

    if kill -0 "$PID" 2>/dev/null; then
        echo "        进程未响应，强制终止"
        kill -9 "$PID" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    echo "        已停止 ✓"
    return 0
}

# ============================ status ============================
do_status() {
    echo "=========== $SERVICE_NAME 状态 ==========="

    if [ "$USE_SYSTEMD" = "yes" ]; then
        printf "管理方式    : systemd\n"
        printf "运行状态    : %s\n" "$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)"
        printf "开机自启    : %s\n" "$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null)"
    else
        printf "管理方式    : nohup + PID 文件\n"
        if PID=$(get_pid); then
            printf "运行状态    : running (PID %s)\n" "$PID"
        else
            printf "运行状态    : stopped\n"
        fi
    fi

    printf "监听地址    : %s:%s\n" "$API_HOST" "$API_PORT"
    [ -n "$PYTHON" ] && printf "Python      : %s\n" "$("$PYTHON" -V 2>&1)"

    # 接口健康检查
    if command -v curl >/dev/null 2>&1; then
        _code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                "http://${CHECK_HOST}:${API_PORT}/api/health" 2>/dev/null)
        if [ "$_code" = "200" ]; then
            printf "健康检查    : HTTP 200 ✓ 服务正常\n"
        else
            printf "健康检查    : 失败（HTTP %s）✗\n" "${_code:-无响应}"
        fi
    fi
    echo "=========================================="
    return 0
}

# ============================ backup ============================
do_backup() {
    DB="$APP_DIR/data/doctor.db"
    if [ ! -f "$DB" ]; then
        echo "[backup] 数据库不存在：$DB"
        return 1
    fi
    STAMP=$(date +%Y%m%d-%H%M%S)
    TARGET="$APP_DIR/data/doctor.db.bak-$STAMP"
    cp "$DB" "$TARGET" || { echo "[backup] 备份失败"; return 1; }
    echo "[backup] 已备份到：$TARGET"
    echo "         大小：$(ls -lh "$TARGET" | awk '{print $5}')"
    echo ""
    echo "         提示：'生成演示数据' 会清空整张表，操作前先跑一次 backup"
    return 0
}

# ============================ logs ============================
do_logs() {
    if [ "$USE_SYSTEMD" = "yes" ]; then
        echo "（Ctrl+C 退出）"
        journalctl -u "$SERVICE_NAME" -f
    else
        [ -f "$LOG_FILE" ] || { echo "日志文件不存在：$LOG_FILE"; return 1; }
        echo "（Ctrl+C 退出）"
        tail -f "$LOG_FILE"
    fi
}

# ============================ 入口 ============================
case "${1:-}" in
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        echo ""
        do_start
        ;;
    status)
        do_status
        ;;
    logs)
        do_logs
        ;;
    backup)
        do_backup
        ;;
    *)
        echo "AI 容器日志医生 —— 服务管理脚本"
        echo ""
        echo "用法： sh $0 {start|stop|restart|status|logs|backup}"
        echo ""
        echo "  start     启动服务（并等待健康检查通过）"
        echo "  stop      停止服务"
        echo "  restart   重启服务"
        echo "  status    查看运行状态和接口健康检查"
        echo "  logs      实时查看日志"
        echo "  backup    备份数据库（生成演示数据前务必执行）"
        echo ""
        echo "项目目录：$APP_DIR"
        exit 1
        ;;
esac
