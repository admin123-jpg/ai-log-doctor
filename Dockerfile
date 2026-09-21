# ==========================================================
# 主程序镜像
#
# 构建： docker build -t ai-log-doctor:latest .
# 运行： docker run -p 8000:8000 ai-log-doctor:latest
# ==========================================================

FROM python:3.11-slim

WORKDIR /app

# 环境变量说明：
#   PYTHONUNBUFFERED=1  日志不缓冲，docker logs 能看到实时输出
#   PYTHONIOENCODING    避免中文日志乱码
#   PYTHONDONTWRITEBYTECODE  不生成 .pyc 文件
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

# 先只复制依赖文件：利用 Docker 层缓存，
# 依赖不变时改代码不会重新 pip install（大幅加快构建速度）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再复制全部代码
COPY . .

# 数据目录（SQLite 和向量缓存会写在这里）
RUN mkdir -p /app/data

EXPOSE 8000

# 健康检查：容器编排系统（K8s / compose）靠它判断服务是否存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["python", "-m", "src.main", "serve", "--host", "0.0.0.0", "--port", "8000"]
