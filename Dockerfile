FROM python:3.11-slim
RUN pip install --no-cache-dir flask
WORKDIR /app
COPY app.py .
COPY templates templates
ENV MUSIC_ROOT=/music
EXPOSE 8321
CMD ["python", "app.py"]
