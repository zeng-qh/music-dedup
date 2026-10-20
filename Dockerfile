FROM python:3.11-slim
RUN pip install --no-cache-dir flask gunicorn
WORKDIR /app
COPY app.py .
COPY templates templates
ENV MUSIC_ROOT=/music
EXPOSE 8321
# 单 worker + 4 线程：扫描是内存密集型，多 worker 会成倍占用内存（Pi 上易 OOM）
# --timeout 0 允许长扫描不被 gunicorn 掐断（扫描自身有看门狗与取消机制）
CMD ["gunicorn", "--bind", "0.0.0.0:8321", "--workers", "1", "--threads", "4", "--timeout", "0", "--graceful-timeout", "10", "app:app"]
