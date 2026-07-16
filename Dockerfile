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

# 构建时预下载 whisper 模型，避免每次冷启动重新下载几百 MB。
# 部署默认用 base：内存约 500MB、速度快，适合免费小机器(2核4G)；本地可用 small。
ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

# 拷贝代码
COPY pipeline.py .
COPY server ./server

# 运行时配置
# WHISPER_CPU_THREADS=1 + OMP_NUM_THREADS=1：在 2 核机器上只用 1 核跑识别，
# 留 1 核给 Web 进程响应健康探针，避免 CPU 打满导致 pod 被平台重启。
ENV SUBTITLE_FONT="Noto Sans CJK SC" \
    DATA_DIR=/data \
    PORT=8000 \
    WHISPER_CPU_THREADS=1 \
    OMP_NUM_THREADS=1 \
    FFMPEG_THREADS=1 \
    X264_PRESET=veryfast \
    GBLUR_SIGMA=12
EXPOSE 8000

# /data 挂持久化卷（存上传/成片/临时文件）
VOLUME ["/data"]

CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
