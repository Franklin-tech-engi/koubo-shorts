#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口播 → 竖屏短视频 核心流水线（可复用模块）

本地 CLI（make_shorts.py）和 Web 后端（server/app.py）都调用这里的 process_video()。
流程：语音识别定位说话段(faster-whisper) → 只保留说话段(剪掉停顿/撩头发等空档)
      → 字幕时间轴重映射 → 竖屏化 + 轻美颜 + 烧字幕(ffmpeg)
全部开源，零 API 费用。
"""

import os
import shutil
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv"}

# ========= 默认配置（改这里就行）=========
# 说明：字体默认取环境变量 SUBTITLE_FONT；本地 Mac 不设就用 PingFang SC，
#      服务器(Linux/Docker)里我们在 Dockerfile 装 Noto CJK 并设 SUBTITLE_FONT。
DEFAULT_CONFIG = {
    # 字幕语言：中文口播用 "zh"；英文用 "en"；留空 "" 让它自动识别
    "language": "zh",

    # 识别模型大小：tiny / base / small / medium / large-v3
    # 可用环境变量 WHISPER_MODEL 覆盖（服务器算力有限时用 base 更省更快；本地追求
    # 更高质量可设 small/medium）。默认 small 适合本地；部署镜像在 Dockerfile 里设为 base。
    "whisper_model": os.environ.get("WHISPER_MODEL", "small"),

    # ctranslate2 CPU 线程数：0 = 库默认（吃满所有核）。部署到 2 核小机器时设 1，
    # 给 Web 进程留核响应健康探针，避免 CPU 打满被平台重启。用环境变量 WHISPER_CPU_THREADS 覆盖。
    "cpu_threads": int(os.environ.get("WHISPER_CPU_THREADS", "0") or "0"),

    # 竖屏画面处理方式：
    #   "blur_pad" —— 画面完整居中，上下用模糊背景填充（不会裁掉人，推荐）
    #   "crop"     —— 居中裁剪成竖屏（画面更满，但可能裁掉两边）
    "reframe_mode": "blur_pad",

    # === 按「说话」剪辑（代替按音量剪静音，街头噪音大也能剪准）===
    "max_pause": 0.8,     # 说话间隔超过这个秒数就剪掉
    "pause_head": 0.25,   # 每段说话前保留秒数
    "pause_tail": 0.35,   # 每段说话后保留秒数

    # === 轻美颜 ===
    "beauty": True,
    "beauty_filter": "bilateral=sigmaS=3:sigmaR=0.08,eq=brightness=0.02:saturation=1.05",

    # 输出分辨率（竖屏 9:16）
    "width": 1080,
    "height": 1920,

    # 字幕样式（白字 + 黑描边，底部居中）
    "font_name": os.environ.get("SUBTITLE_FONT", "PingFang SC"),
    "font_size": 15,
    "subtitle_margin_v": 45,

    # 每行字幕最多多少字
    "max_chars_per_line": 16,
    "max_seconds_per_line": 4.0,

    # 输出文件名后缀
    "output_suffix": "_竖屏成片",
}


def make_config(overrides=None):
    """基于默认配置生成一份完整配置（可传字典覆盖个别项）。"""
    cfg = dict(DEFAULT_CONFIG)
    if overrides:
        cfg.update(overrides)
    return cfg


def run(cmd, **kw):
    """执行命令，失败时抛出并打印。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        print("命令失败：", " ".join(str(c) for c in cmd))
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise RuntimeError(f"命令返回码 {proc.returncode}")
    return proc


def secs_to_srt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def get_duration(src):
    proc = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(src)])
    return float(proc.stdout.strip())


def step_transcribe_words(src, cfg):
    """用 faster-whisper 识别原片，返回带时间戳的词列表 [(start,end,word), ...]。"""
    from faster_whisper import WhisperModel

    # cpu_threads：限制 ctranslate2 用几个线程。部署到小机器(如 2 核)时留 1 核给
    # Web 进程响应健康探针，避免 CPU 打满导致 pod 被平台判定不健康而重启。0 = 库默认。
    cpu_threads = int(cfg.get("cpu_threads", 0) or 0)
    model = WhisperModel(
        cfg["whisper_model"],
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        num_workers=1,
    )
    lang = cfg["language"] or None
    segments, _info = model.transcribe(
        str(src),
        language=lang,
        word_timestamps=True,
        vad_filter=True,
    )
    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                if w.word.strip():
                    words.append((w.start, w.end, w.word))
        else:
            text = seg.text.strip()
            if text:
                words.append((seg.start, seg.end, text))
    return words


def build_keep_from_words(words, duration, cfg):
    """根据说话时间轴算保留片段：说话间隔 > max_pause 的空档剪掉。"""
    head = cfg["pause_head"]
    tail = cfg["pause_tail"]
    max_pause = cfg["max_pause"]

    spans = []
    cur_s, cur_e = None, None
    for s, e, _w in words:
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s - cur_e <= max_pause:
            cur_e = max(cur_e, e)
        else:
            spans.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    if cur_s is not None:
        spans.append((cur_s, cur_e))

    keep = []
    for s, e in spans:
        ks, ke = max(0.0, s - head), min(duration, e + tail)
        if keep and ks <= keep[-1][1]:
            keep[-1] = (keep[-1][0], max(keep[-1][1], ke))
        else:
            keep.append((ks, ke))
    return [(s, e) for s, e in keep if e - s > 0.05]


def step_cut(src, keep, dst):
    """按保留片段剪辑（音画同步）。"""
    total = get_duration(src)
    if len(keep) == 1 and keep[0][0] <= 0.01 and keep[0][1] >= total - 0.01:
        shutil.copy(str(src), str(dst))
        return
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep)
    vf = f"select='{expr}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{expr}',asetpts=N/SR/TB"
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf, "-af", af,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        str(dst),
    ]
    run(cmd)


def remap_time(t, keep):
    """把原片时间 t 映射到剪辑后时间轴。"""
    new_t = 0.0
    for s, e in keep:
        if t < s:
            break
        if t <= e:
            return new_t + (t - s)
        new_t += e - s
    return new_t


def words_to_lines(words, keep, cfg):
    """把（已剪辑保留的）词按短行切成字幕 [(start,end,text), ...]，时间为新时间轴。"""
    max_chars = cfg["max_chars_per_line"]
    max_dur = cfg["max_seconds_per_line"]

    kept_words = []
    for s, e, w in words:
        mid = (s + e) / 2
        if any(ks - 0.05 <= mid <= ke + 0.05 for ks, ke in keep):
            kept_words.append((remap_time(s, keep), remap_time(e, keep), w))

    lines = []
    cur, cur_start = [], None
    for s, e, w in kept_words:
        if cur_start is None:
            cur_start = s
        cur.append((s, e, w))
        text = "".join(x[2] for x in cur).strip()
        dur = e - cur_start
        end_punct = text[-1:] in "。！？!?…" if text else False
        if len(text.replace(" ", "")) >= max_chars or dur >= max_dur or end_punct:
            lines.append((cur_start, e, text))
            cur, cur_start = [], None
    if cur:
        text = "".join(x[2] for x in cur).strip()
        if text:
            lines.append((cur_start, cur[-1][1], text))
    return lines


def write_srt(lines, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(lines, 1):
            if end <= start:
                end = start + 0.5
            f.write(f"{i}\n{secs_to_srt_time(start)} --> {secs_to_srt_time(end)}\n{text}\n\n")


def build_video_filter(cfg):
    """返回 ffmpeg 的画面处理滤镜串（不含字幕，字幕单独拼接）。"""
    W, H = cfg["width"], cfg["height"]
    beauty = f"{cfg['beauty_filter']}," if cfg.get("beauty") else ""
    if cfg["reframe_mode"] == "crop":
        vf = (f"{beauty}scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H}[vid]")
    else:  # blur_pad
        vf = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma=20[bgb];"
            f"[fg]{beauty}scale={W}:{H}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vid]"
        )
    return vf


def step_reframe_and_burn(src, srt_name, dst, workdir, cfg):
    """竖屏化(+轻美颜)并烧字幕。srt_name 为 workdir 下的相对文件名，None 表示无字幕。"""
    style = (
        f"FontName={cfg['font_name']},"
        f"FontSize={cfg['font_size']},"
        f"PrimaryColour=&H00FFFFFF,"      # 白字
        f"OutlineColour=&H00000000,"      # 黑描边
        f"BorderStyle=1,Outline=2,Shadow=0,"
        f"Alignment=2,MarginV={cfg['subtitle_margin_v']}"
    )
    base = build_video_filter(cfg)
    prefix = "[0:v]" if cfg["reframe_mode"] == "crop" else ""
    if srt_name is None:
        fc = f"{prefix}{base};[vid]null[out]"
    else:
        fc = f"{prefix}{base};[vid]subtitles={srt_name}:force_style='{style}'[out]"

    cmd = [
        "ffmpeg", "-y", "-i", str(Path(src).resolve()),
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(Path(dst).resolve()),
    ]
    run(cmd, cwd=str(workdir))


def _noop(msg):
    print(msg)


def process_video(src_path, out_dir, work_dir, config=None, progress=None):
    """处理单条视频，返回成片路径。

    参数：
      src_path : 原片路径
      out_dir  : 成片输出目录
      work_dir : 该任务的临时工作目录（函数会在其下建以文件名命名的子目录）
      config   : 覆盖 DEFAULT_CONFIG 的字典（可选）
      progress : 进度回调 progress(stage:str, pct:int)（可选），用于 Web 端显示进度
    """
    cfg = make_config(config)
    progress = progress or (lambda stage, pct: None)

    src_path = Path(src_path)
    out_dir = Path(out_dir)
    work_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    name = src_path.stem
    job = work_dir / name
    if job.exists():
        shutil.rmtree(job)
    job.mkdir(parents=True)

    duration = get_duration(src_path)

    _noop(f"  [1/3] 语音识别定位说话段 ...")
    progress("识别语音", 10)
    words = step_transcribe_words(src_path, cfg)

    trimmed = job / "trimmed.mp4"
    if words:
        keep = build_keep_from_words(words, duration, cfg)
        cut_total = duration - sum(e - s for s, e in keep)
        _noop(f"  [2/3] 剪掉不说话空档 {cut_total:.1f} 秒（共 {len(keep)} 段说话）...")
        progress("剪掉空档", 45)
        step_cut(src_path, keep, trimmed)
        lines = words_to_lines(words, keep, cfg)
    else:
        _noop("  [2/3] 没识别到人声，画面原样保留 ...")
        progress("剪掉空档", 45)
        keep = [(0.0, duration)]
        shutil.copy(str(src_path), str(trimmed))
        lines = []

    srt_path = job / "sub.srt"
    write_srt(lines, srt_path)
    _noop(f"        生成 {len(lines)} 条字幕")

    _noop("  [3/3] 竖屏化 + 轻美颜 + 烧字幕 ...")
    progress("竖屏化+烧字幕", 70)
    out_path = out_dir / f"{name}{cfg['output_suffix']}.mp4"
    srt_name = "sub.srt" if lines else None
    step_reframe_and_burn(trimmed, srt_name, out_path, job, cfg)

    progress("完成", 100)
    _noop(f"  ✅ 完成：{out_path}")

    # 清理该任务的中间文件（成片已在 out_dir，不受影响）
    shutil.rmtree(job, ignore_errors=True)
    return out_path
