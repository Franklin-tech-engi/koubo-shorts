# 口播剪辑 Web 版 · 部署镜像
FROM python:3.11-slim

# 系统依赖：ffmpeg(剪辑) + Noto CJK 中文字体(烧中文字幕必须) + libgomp(whisper 运行时)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app

# 先装依赖（利用缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 构建时预下载 whisper 模型，避免每次冷启动重新下载几百 MB
ARG WHISPER_MODEL=small
ENV WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

# 拷贝代码
COPY pipeline.py .
COPY server ./server

# 运行时配置
ENV SUBTITLE_FONT="Noto Sans CJK SC" \
    DATA_DIR=/data \
    PORT=8000
EXPOSE 8000

# /data 挂持久化卷（存上传/成片/临时文件）
VOLUME ["/data"]

CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
