FROM python:3.11-slim

# 安装依赖后立即清理，全部在同一层完成，避免体积叠加
RUN pip install --no-cache-dir flask gunicorn \
 && find /usr/local/lib/python3.11 -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /usr/local/lib/python3.11 -name '*.pyc' -delete \
 # pip / setuptools / wheel 运行时不需要（应用已装好，容器内不会再用 pip）
 && rm -rf /usr/local/lib/python3.11/site-packages/pip* \
           /usr/local/lib/python3.11/site-packages/setuptools* \
           /usr/local/lib/python3.11/site-packages/wheel* \
           /usr/local/lib/python3.11/site-packages/pkg_resources/* \
           /usr/local/lib/python3.11/site-packages/_distutils_hack \
           /usr/local/lib/python3.11/site-packages/distutils-precedence.pth \
 # 应用用不到的标准库：GUI、包管理引导、2to3、虚拟环境、旧 distutils、单元测试
           /usr/local/lib/python3.11/idlelib \
           /usr/local/lib/python3.11/tkinter \
           /usr/local/lib/python3.11/turtledemo \
           /usr/local/lib/python3.11/ensurepip \
           /usr/local/lib/python3.11/lib2to3 \
           /usr/local/lib/python3.11/venv \
           /usr/local/lib/python3.11/distutils \
           /usr/local/lib/python3.11/unittest \
 # 第三方包的测试用例
 && find /usr/local/lib/python3.11/site-packages -type d -name tests -prune -exec rm -rf {} + \
 && rm -rf /usr/share/doc/* /var/cache/* /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .
COPY templates templates
ENV MUSIC_ROOT=/music
EXPOSE 8321

# 构建期自检：确认精简后关键模块仍可导入，避免运行时才发现问题
RUN python -c "import flask, gunicorn, hashlib, difflib, struct, json, pathlib, shutil, threading, collections, re, os; print('deps ok')"

# 单 worker + 4 线程：扫描是内存密集型，多 worker 会成倍占用内存（Pi 上易 OOM）
# --timeout 0 允许长扫描不被 gunicorn 掐断（扫描自身有看门狗与取消机制）
CMD ["gunicorn", "--bind", "0.0.0.0:8321", "--workers", "1", "--threads", "4", "--timeout", "0", "--graceful-timeout", "10", "app:app"]
