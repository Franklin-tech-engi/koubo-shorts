#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口播 → 竖屏短视频 自动化流水线
把原片放进 input/ 文件夹，运行本脚本，成片自动出现在 output/。

流程：语音识别定位说话段(faster-whisper) → 只保留说话段(剪掉停顿/撩头发等空档)
      → 字幕时间轴重映射 → 竖屏化 + 轻美颜 + 烧字幕(ffmpeg)
全部开源，零 API 费用。
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# ========= 可调配置（改这里就行）=========
CONFIG = {
    # 字幕语言：中文口播用 "zh"；英文用 "en"；留空 "" 让它自动识别
    "language": "zh",

    # 识别模型大小：tiny / base / small / medium / large-v3
    "whisper_model": "small",

    # 竖屏画面处理方式：
    #   "blur_pad" —— 画面完整居中，上下用模糊背景填充（不会裁掉人，推荐）
    #   "crop"     —— 居中裁剪成竖屏（画面更满，但可能裁掉两边）
    "reframe_mode": "blur_pad",

    # === 按「说话」剪辑（代替按音量剪静音，街头噪音大也能剪准）===
    # 说话间隔超过这个秒数就剪掉（撩头发、停顿看镜头这类空档会被剪掉）
    "max_pause": 0.8,
    # 每段说话前后各保留多少秒，衔接更自然
    "pause_head": 0.25,
    "pause_tail": 0.35,

    # === 轻美颜 ===
    "beauty": True,
    # 双边滤波磨皮(保边缘) + 轻微提亮提饱和；想更强把 sigmaR 调到 0.12
    "beauty_filter": "bilateral=sigmaS=3:sigmaR=0.08,eq=brightness=0.02:saturation=1.05",

    # 输出分辨率（竖屏 9:16）
    "width": 1080,
    "height": 1920,

    # 字幕样式（简单清晰：白字 + 黑描边，底部居中）
    "font_name": "PingFang SC",   # Mac 中文字体；英文可用 "Arial"
    "font_size": 15,
    "subtitle_margin_v": 45,      # 字幕离底部的距离（越大越靠上）

    # 每行字幕最多多少字（中文按字数，太长会自动换行/断句）
    "max_chars_per_line": 16,
    "max_seconds_per_line": 4.0,

    # 输出文件名后缀
    "output_suffix": "_竖屏成片",
}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv"}

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
WORK_DIR = ROOT / ".work"


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


def step_transcribe_words(src):
    """用 faster-whisper 识别原片，返回带时间戳的词列表 [(start,end,word), ...]。"""
    from faster_whisper import WhisperModel

    model = WhisperModel(CONFIG["whisper_model"], device="cpu", compute_type="int8")
    lang = CONFIG["language"] or None
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


def build_keep_from_words(words, duration):
    """根据说话时间轴算保留片段：说话间隔 > max_pause 的空档剪掉。"""
    head = CONFIG["pause_head"]
    tail = CONFIG["pause_tail"]
    max_pause = CONFIG["max_pause"]

    # 先把词合并成连续说话段（相邻词间隔 <= max_pause 视为同一段）
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

    # 前后加呼吸空间并裁剪到片长内，再合并重叠
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


def words_to_lines(words, keep):
    """把（已剪辑保留的）词按短行切成字幕 [(start,end,text), ...]，时间为新时间轴。"""
    max_chars = CONFIG["max_chars_per_line"]
    max_dur = CONFIG["max_seconds_per_line"]

    # 只保留落在 keep 区间内的词，并映射到新时间轴
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


def build_video_filter():
    """返回 ffmpeg 的 -filter_complex 字符串（不含字幕，字幕单独拼接）。"""
    W, H = CONFIG["width"], CONFIG["height"]
    beauty = f"{CONFIG['beauty_filter']}," if CONFIG.get("beauty") else ""
    if CONFIG["reframe_mode"] == "crop":
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


def step_reframe_and_burn(src, srt_name, dst, workdir):
    """竖屏化(+轻美颜)并烧字幕。srt_name 为 workdir 下的相对文件名。
    srt_name 传 None 时表示没有字幕，只做画面处理。"""
    style = (
        f"FontName={CONFIG['font_name']},"
        f"FontSize={CONFIG['font_size']},"
        f"PrimaryColour=&H00FFFFFF,"      # 白字
        f"OutlineColour=&H00000000,"      # 黑描边
        f"BorderStyle=1,Outline=2,Shadow=0,"
        f"Alignment=2,MarginV={CONFIG['subtitle_margin_v']}"
    )
    base = build_video_filter()
    prefix = "[0:v]" if CONFIG["reframe_mode"] == "crop" else ""
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
    # 在 workdir 内运行，subtitles 用相对文件名，规避特殊字符转义
    run(cmd, cwd=str(workdir))


def process_one(video_path):
    name = video_path.stem
    print(f"\n=== 处理：{video_path.name} ===")
    job = WORK_DIR / name
    if job.exists():
        shutil.rmtree(job)
    job.mkdir(parents=True)

    duration = get_duration(video_path)

    print("  [1/3] 语音识别定位说话段 ...")
    words = step_transcribe_words(video_path)

    trimmed = job / "trimmed.mp4"
    if words:
        keep = build_keep_from_words(words, duration)
        cut_total = duration - sum(e - s for s, e in keep)
        print(f"  [2/3] 剪掉不说话空档 {cut_total:.1f} 秒（共 {len(keep)} 段说话）...")
        step_cut(video_path, keep, trimmed)
        lines = words_to_lines(words, keep)
    else:
        print("  [2/3] 没识别到人声，画面原样保留 ...")
        keep = [(0.0, duration)]
        shutil.copy(str(video_path), str(trimmed))
        lines = []

    srt_path = job / "sub.srt"
    write_srt(lines, srt_path)
    print(f"        生成 {len(lines)} 条字幕")

    print("  [3/3] 竖屏化 + 轻美颜 + 烧字幕 ...")
    out_path = OUTPUT_DIR / f"{name}{CONFIG['output_suffix']}.mp4"
    srt_name = "sub.srt" if lines else None
    step_reframe_and_burn(trimmed, srt_name, out_path, job)

    print(f"  ✅ 完成：output/{out_path.name}")
    return out_path


def main():
    for d in (INPUT_DIR, OUTPUT_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)

    only = set(sys.argv[1:])  # 可选：只处理指定文件名（不含路径）
    videos = sorted(p for p in INPUT_DIR.iterdir()
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                    and (not only or p.name in only))
    if not videos:
        print("input/ 文件夹里没有视频。把原片拖进去再运行。")
        return

    print(f"共发现 {len(videos)} 条视频，开始批量处理...")
    ok, fail = [], []
    for v in videos:
        try:
            ok.append(process_one(v))
        except Exception as e:
            print(f"  ❌ {v.name} 处理失败：{e}")
            fail.append(v.name)

    print("\n========== 全部完成 ==========")
    print(f"成功 {len(ok)} 条，失败 {len(fail)} 条")
    if fail:
        print("失败列表：", ", ".join(fail))
    print(f"成片在：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
