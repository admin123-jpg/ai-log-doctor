FROM python:3.11-slim

WORKDIR /app

# PYTHONUNBUFFERED=1：让 print 和日志立刻输出，不被缓冲（否则 docker logs 看不到实时日志）
# PYTHONIOENCODING：避免中文日志乱码
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health')"

CMD ["python", "app.py"]
