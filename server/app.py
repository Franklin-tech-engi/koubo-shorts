#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口播剪辑 Web 版后端（FastAPI）

给不懂技术的人用：打开网页 → 拖视频进去 → 排队 → 下载竖屏成片。
为控制服务器成本，采用「单 worker 串行处理 + 排队」：同一时间只处理一条，
人多时后面的排队等待（前端会显示排到第几位）。

环境变量：
  DATA_DIR       数据目录（放上传/成片/临时文件），默认 ./data —— 部署时指向持久化卷
  MAX_UPLOAD_MB  单个文件大小上限(MB)，默认 500
  RETAIN_HOURS   成片/上传保留小时数，超时自动清理，默认 6
  WHISPER_MODEL  识别模型(tiny/base/small/...)，默认 small
  SUBTITLE_FONT  字幕字体名，Linux 部署设为 "Noto Sans CJK SC"
"""

import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import VIDEO_EXTS, process_video  # noqa: E402

# ---------- 配置 ----------
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
WORK_DIR = DATA_DIR / "work"
for d in (UPLOAD_DIR, OUTPUT_DIR, WORK_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
RETAIN_HOURS = float(os.environ.get("RETAIN_HOURS", "6"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------- 任务状态（内存） ----------
# job_id -> dict(status, stage, pct, filename, out_path, error, created, submit_seq)
JOBS = {}
JOBS_LOCK = threading.Lock()
WORK_QUEUE = queue.Queue()
_SUBMIT_SEQ = 0  # 提交序号，用于算排队位置

app = FastAPI(title="口播剪辑")


def _now():
    return time.time()


def _set(job_id, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def _queue_position(job_id):
    """算这个任务前面还有几个没处理完（排队中或正在处理），返回 0 表示轮到它/已在处理。"""
    with JOBS_LOCK:
        me = JOBS.get(job_id)
        if not me or me["status"] not in ("queued", "processing"):
            return 0
        my_seq = me["submit_seq"]
        ahead = sum(
            1 for j in JOBS.values()
            if j["status"] in ("queued", "processing") and j["submit_seq"] < my_seq
        )
        return ahead


def worker_loop():
    """单 worker：从队列取任务，串行处理。"""
    while True:
        job_id = WORK_QUEUE.get()
        try:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job or job["status"] == "canceled":
                continue
            src = Path(job["src_path"])
            if not src.exists():
                _set(job_id, status="error", error="上传文件丢失")
                continue

            _set(job_id, status="processing", stage="开始", pct=5)

            def progress(stage, pct):
                _set(job_id, stage=stage, pct=pct)

            out_path = process_video(src, OUTPUT_DIR, WORK_DIR, progress=progress)
            _set(job_id, status="done", stage="完成", pct=100,
                 out_path=str(out_path), out_name=out_path.name)
        except Exception as e:  # 处理失败不影响后续任务
            _set(job_id, status="error", error=str(e))
        finally:
            # 删掉上传的原片，省空间（成片保留到清理线程处理）
            try:
                p = Path(JOBS.get(job_id, {}).get("src_path", ""))
                if p.exists():
                    p.unlink()
            except Exception:
                pass
            WORK_QUEUE.task_done()


def cleanup_loop():
    """定期清理超过 RETAIN_HOURS 的成片/上传/内存任务，避免占满磁盘。"""
    while True:
        try:
            cutoff = _now() - RETAIN_HOURS * 3600
            # 清成片和残留上传文件
            for folder in (OUTPUT_DIR, UPLOAD_DIR):
                for f in folder.iterdir():
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
            # 清内存里的旧任务记录
            with JOBS_LOCK:
                stale = [jid for jid, j in JOBS.items() if j["created"] < cutoff]
                for jid in stale:
                    JOBS.pop(jid, None)
        except Exception as e:
            print("清理线程出错：", e)
        time.sleep(600)  # 每 10 分钟扫一次


@app.on_event("startup")
def _startup():
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, f"不支持的文件类型：{ext or '未知'}。请上传视频文件。")

    global _SUBMIT_SEQ
    job_id = uuid.uuid4().hex[:12]
    # 安全的落盘文件名：用 job_id + 原始后缀，原始名只用于展示
    safe_name = f"{job_id}{ext}"
    dst = UPLOAD_DIR / safe_name

    # 流式写盘并限制大小
    size = 0
    with open(dst, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(
                    413, f"文件太大，最大 {MAX_UPLOAD_MB}MB。请压缩或裁短后再上传。")
            out.write(chunk)

    if size == 0:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "空文件。")

    with JOBS_LOCK:
        _SUBMIT_SEQ += 1
        JOBS[job_id] = {
            "status": "queued",
            "stage": "排队中",
            "pct": 0,
            "filename": file.filename,
            "src_path": str(dst),
            "out_path": None,
            "out_name": None,
            "error": None,
            "created": _now(),
            "submit_seq": _SUBMIT_SEQ,
        }
    WORK_QUEUE.put(job_id)
    return {"job_id": job_id, "position": _queue_position(job_id)}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在或已过期")
        data = {
            "status": job["status"],
            "stage": job["stage"],
            "pct": job["pct"],
            "filename": job["filename"],
            "error": job["error"],
            "out_name": job["out_name"],
        }
    data["position"] = _queue_position(job_id)
    return data


@app.get("/api/download/{job_id}")
def download(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["out_path"]:
        raise HTTPException(404, "成片还没好或已过期")
    p = Path(job["out_path"])
    if not p.exists():
        raise HTTPException(404, "成片已被清理，请重新上传")
    return FileResponse(str(p), filename=job["out_name"], media_type="video/mp4")


# 兜底静态资源（如果以后加图片等）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
