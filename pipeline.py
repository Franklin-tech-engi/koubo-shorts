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

    # === 转录后端 ===
    # 配了 TRANSCRIBE_API_BASE + TRANSCRIBE_API_KEY 就走云端 API（OpenAI 兼容，如 gptnb
    # 的 whisper-large-v3-turbo）：中文更准，且不占本地内存，小机器也不会因加载模型而崩。
    # 没配则退回上面的本地 faster-whisper。
    "transcribe_api_base": os.environ.get("TRANSCRIBE_API_BASE", ""),
    "transcribe_api_key": os.environ.get("TRANSCRIBE_API_KEY", ""),
    "transcribe_api_model": os.environ.get("TRANSCRIBE_API_MODEL", "whisper-large-v3-turbo"),

    # === ffmpeg 资源控制（部署到小机器的关键）===
    # ffmpeg 默认吃满所有核 + medium 预设编码很重，真实 1080p 素材会把 2 核小机器
    # 的 CPU 打满、健康探针超时导致 pod 被杀。下面三项在 Dockerfile 里收紧。
    "ffmpeg_threads": int(os.environ.get("FFMPEG_THREADS", "0") or "0"),  # 0=不限；服务器设 1
    "x264_preset": os.environ.get("X264_PRESET", "medium"),               # 服务器用 veryfast 更省
    "gblur_sigma": os.environ.get("GBLUR_SIGMA", "20"),                   # 模糊背景开销；服务器降到 12

    # 竖屏画面处理方式：
    #   "blur_pad" —— 画面完整居中，上下用模糊背景填充（不会裁掉人，推荐）
    #   "crop"     —— 居中裁剪成竖屏（画面更满，但可能裁掉两边）
    "reframe_mode": "blur_pad",

    # === 按「说话」剪辑（代替按音量剪静音，街头噪音大也能剪准）===
    "max_pause": 0.8,     # 说话间隔超过这个秒数就剪掉
    "pause_head": 0.25,   # 每段说话前保留秒数
    "pause_tail": 0.35,   # 每段说话后保留秒数

    # === 美颜（加强版：磨皮更明显、提亮提饱和，仍自然）===
    "beauty": True,
    "beauty_filter": os.environ.get(
        "BEAUTY_FILTER",
        "bilateral=sigmaS=6:sigmaR=0.14,eq=brightness=0.04:saturation=1.10:contrast=1.03",
    ),

    # 输出分辨率（竖屏 9:16）
    "width": 1080,
    "height": 1920,

    # === 顶部标题（上传时填；空则不加）===
    "title": "",
    "title_font_size": int(os.environ.get("TITLE_FONT_SIZE", "62")),
    "title_font_file": os.environ.get("TITLE_FONT_FILE", ""),  # 空则自动探测 CJK 字体
    "title_max_chars": 13,   # 每行标题最多字数，超了自动换行
    "title_y": int(os.environ.get("TITLE_Y", "170")),  # 标题距顶部像素

    # 字幕样式（白字 + 黑描边，底部居中）
    "font_name": os.environ.get("SUBTITLE_FONT", "PingFang SC"),
    "font_size": 15,
    "subtitle_margin_v": 45,

    # === 字幕关键词标黄（用 LLM 挑词，整句白字、关键词黄色）===
    "highlight_keywords": os.environ.get("HIGHLIGHT_KEYWORDS", "1") == "1",
    "llm_api_base": os.environ.get("TRANSCRIBE_API_BASE", ""),   # 复用 gptnb
    "llm_api_key": os.environ.get("TRANSCRIBE_API_KEY", ""),
    "llm_model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),

    # 每行字幕最多多少字（12 字在 1080 宽下不会贴边）
    "max_chars_per_line": 12,
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


def _thread_args(cfg):
    """返回限制 ffmpeg 线程的参数（放在 ffmpeg 之后、输入之前）。0 表示不限。"""
    n = int(cfg.get("ffmpeg_threads", 0) or 0)
    if n > 0:
        return ["-threads", str(n), "-filter_threads", str(n),
                "-filter_complex_threads", str(n)]
    return []


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


def _transcribe_via_api(src, cfg):
    """走云端 OpenAI 兼容转录 API（whisper-large-v3-turbo），返回带时间戳的词列表。
    先抽成 16k 单声道 mp3（小、快，规避云端 25MB 文件上限），再上传识别。"""
    import tempfile
    import requests

    fd, audio = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             "-b:a", "64k", audio])
        base = cfg["transcribe_api_base"].rstrip("/")
        data = {
            "model": cfg["transcribe_api_model"],
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        if cfg.get("language"):
            data["language"] = cfg["language"]
        with open(audio, "rb") as f:
            resp = requests.post(
                f"{base}/audio/transcriptions",
                headers={"Authorization": "Bearer " + cfg["transcribe_api_key"]},
                data=data,
                files={"file": (os.path.basename(audio), f, "audio/mpeg")},
                timeout=300,
            )
        resp.raise_for_status()
        js = resp.json()
        words = []
        for w in js.get("words", []) or []:
            txt = (w.get("word") or "").strip()
            if txt and w.get("start") is not None and w.get("end") is not None:
                words.append((float(w["start"]), float(w["end"]), w["word"]))
        if not words:  # 没词级时间戳就退回按句
            for seg in js.get("segments", []) or []:
                t = (seg.get("text") or "").strip()
                if t and seg.get("start") is not None and seg.get("end") is not None:
                    words.append((float(seg["start"]), float(seg["end"]), t))
        return words
    finally:
        try:
            os.unlink(audio)
        except Exception:
            pass


def step_transcribe_words(src, cfg):
    """识别原片，返回带时间戳的词列表 [(start,end,word), ...]。
    配了云端转录 API 就走 API（更准、不占内存），否则用本地 faster-whisper。"""
    if cfg.get("transcribe_api_base") and cfg.get("transcribe_api_key"):
        return _transcribe_via_api(src, cfg)

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


def step_cut(src, keep, dst, cfg):
    """按保留片段剪辑（音画同步）。"""
    total = get_duration(src)
    if len(keep) == 1 and keep[0][0] <= 0.01 and keep[0][1] >= total - 0.01:
        shutil.copy(str(src), str(dst))
        return
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep)
    vf = f"select='{expr}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{expr}',asetpts=N/SR/TB"
    cmd = [
        "ffmpeg", "-y", *_thread_args(cfg), "-i", str(src),
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


# 常见 CJK 字体路径（drawtext 需要真实字体文件路径，字幕的 subtitles/ass 走 fontconfig 不需要）
_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


def _find_cjk_fontfile(cfg):
    """找一个可用的中文字体文件路径（给标题 drawtext 用）。"""
    if cfg.get("title_font_file") and os.path.exists(cfg["title_font_file"]):
        return cfg["title_font_file"]
    for p in _CJK_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _wrap_cjk(text, max_chars):
    """按字数把标题折成多行。"""
    text = text.strip()
    lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    return lines[:2]  # 标题最多两行


def _extract_keywords(lines, cfg):
    """调 LLM 给每条字幕挑 1-2 个关键词，返回和 lines 等长的关键词列表。失败返回全空。"""
    if not (cfg.get("highlight_keywords") and cfg.get("llm_api_base") and cfg.get("llm_api_key")):
        return [[] for _ in lines]
    if not lines:
        return []
    import json as _json
    import requests
    texts = [t for _, _, t in lines]
    prompt = (
        "下面是一条口播视频的逐句字幕（JSON数组）。给每一句挑出 1-2 个最重要、最该被强调的"
        "关键词（必须是句子里原样出现的连续片段，通常是名词/动词/数字/专有名词，别挑虚词）。"
        "严格只返回一个 JSON 数组，长度和输入相同，每一项是该句关键词组成的数组（没有合适的就空数组）。"
        "不要任何解释、不要代码块标记。\n输入：" + _json.dumps(texts, ensure_ascii=False)
    )
    try:
        base = cfg["llm_api_base"].rstrip("/")
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": "Bearer " + cfg["llm_api_key"],
                     "Content-Type": "application/json"},
            json={"model": cfg["llm_model"], "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        content = content.replace("```json", "").replace("```", "").strip()
        kws = _json.loads(content)
        # 对齐长度 + 只保留确实出现在句子里的关键词
        out = []
        for i, (_, _, text) in enumerate(lines):
            row = kws[i] if i < len(kws) and isinstance(kws[i], list) else []
            out.append([k for k in row if k and k in text][:2])
        return out
    except Exception as e:
        print("  关键词提取失败，退回纯白字幕：", e)
        return [[] for _ in lines]


def secs_to_ass_time(t):
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ass_highlight(text, keywords, hi_color):
    """把关键词用黄色 override 包起来（ASS 内联颜色）。"""
    reset = r"{\c&H00FFFFFF&}"
    hi = r"{\c" + hi_color + r"&}" if not hi_color.startswith("&H") else r"{\c" + hi_color + r"}"
    out = text
    for kw in keywords:
        if kw and kw in out:
            out = out.replace(kw, hi + kw + reset, 1)
    return out


def write_ass(lines, keywords, path, cfg):
    """生成 ASS 字幕：整句白字黑边、关键词黄色。"""
    W, H = cfg["width"], cfg["height"]
    fs = int(round(cfg["font_size"] * (H / 288.0)))  # 与旧 SRT 观感对齐
    mv = int(round(cfg["subtitle_margin_v"] * (H / 288.0)))
    font = cfg["font_name"]
    hi_color = "&H0000FFFF"  # 黄（ASS 是 BGR）
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Def,{font},{fs},&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(2, fs // 12)},0,2,60,60,{mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for i, (start, end, text) in enumerate(lines):
            if end <= start:
                end = start + 0.5
            kws = keywords[i] if i < len(keywords) else []
            body = _ass_highlight(text, kws, hi_color)
            f.write(f"Dialogue: 0,{secs_to_ass_time(start)},{secs_to_ass_time(end)},"
                    f"Def,,0,0,0,,{body}\n")


def build_video_filter(cfg):
    """返回 ffmpeg 的画面处理滤镜串（不含字幕，字幕单独拼接）。"""
    W, H = cfg["width"], cfg["height"]
    beauty = f"{cfg['beauty_filter']}," if cfg.get("beauty") else ""
    if cfg["reframe_mode"] == "crop":
        vf = (f"{beauty}scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H}[vid]")
    else:  # blur_pad
        sigma = cfg.get("gblur_sigma", "20")
        vf = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma={sigma}[bgb];"
            f"[fg]{beauty}scale={W}:{H}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vid]"
        )
    return vf


def step_reframe_and_burn(src, sub_name, dst, workdir, cfg):
    """竖屏化(+美颜) + 烧字幕(+顶部标题)。
    sub_name 为 workdir 下的字幕文件名(.ass 或 .srt)，None 表示无字幕。"""
    base = build_video_filter(cfg)
    prefix = "[0:v]" if cfg["reframe_mode"] == "crop" else ""
    chain = f"{prefix}{base}"   # 产出 [vid]
    label = "vid"

    # 1) 烧字幕：.ass 走 ass 滤镜（支持关键词内联黄色），.srt 走 subtitles 滤镜
    if sub_name:
        if sub_name.lower().endswith(".ass"):
            chain += f";[{label}]ass={sub_name}[subbed]"
        else:
            style = (f"FontName={cfg['font_name']},FontSize={cfg['font_size']},"
                     f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                     f"BorderStyle=1,Outline=2,Shadow=0,Alignment=2,"
                     f"MarginV={cfg['subtitle_margin_v']}")
            chain += f";[{label}]subtitles={sub_name}:force_style='{style}'[subbed]"
        label = "subbed"

    # 2) 顶部标题：drawtext（每行独立、居中，写到 workdir 的 txt 里规避转义）
    title = (cfg.get("title") or "").strip()
    if title:
        fontfile = _find_cjk_fontfile(cfg)
        if fontfile:
            fs = cfg["title_font_size"]
            for k, tl in enumerate(_wrap_cjk(title, cfg["title_max_chars"])):
                (Path(workdir) / f"title_{k}.txt").write_text(tl, encoding="utf-8")
                y = cfg["title_y"] + k * int(fs * 1.3)
                dt = (f"drawtext=fontfile='{fontfile}':textfile=title_{k}.txt:"
                      f"fontsize={fs}:fontcolor=white:"
                      f"borderw={max(3, fs // 14)}:bordercolor=black@0.9:"
                      f"shadowx=2:shadowy=2:shadowcolor=black@0.5:"
                      f"x=(w-text_w)/2:y={y}")
                chain += f";[{label}]{dt}[t{k}]"
                label = f"t{k}"
        else:
            print("  未找到中文字体文件，跳过标题")

    chain += f";[{label}]null[out]"

    cmd = [
        "ffmpeg", "-y", *_thread_args(cfg), "-i", str(Path(src).resolve()),
        "-filter_complex", chain,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", cfg.get("x264_preset", "medium"), "-crf", "20",
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
        step_cut(src_path, keep, trimmed, cfg)
        lines = words_to_lines(words, keep, cfg)
    else:
        _noop("  [2/3] 没识别到人声，画面原样保留 ...")
        progress("剪掉空档", 45)
        keep = [(0.0, duration)]
        shutil.copy(str(src_path), str(trimmed))
        lines = []

    # 字幕：默认走 ASS（支持关键词标黄）；关了高亮或没配 LLM 就退回纯白 ASS
    sub_name = None
    if lines:
        keywords = _extract_keywords(lines, cfg)
        n_hi = sum(len(k) for k in keywords)
        write_ass(lines, keywords, job / "sub.ass", cfg)
        sub_name = "sub.ass"
        _noop(f"        生成 {len(lines)} 条字幕（关键词标黄 {n_hi} 个）")

    _noop("  [3/3] 竖屏化 + 美颜 + 烧字幕" + ("+标题" if (cfg.get('title') or '').strip() else "") + " ...")
    progress("竖屏化+烧字幕", 70)
    out_path = out_dir / f"{name}{cfg['output_suffix']}.mp4"
    step_reframe_and_burn(trimmed, sub_name, out_path, job, cfg)

    progress("完成", 100)
    _noop(f"  ✅ 完成：{out_path}")

    # 清理该任务的中间文件（成片已在 out_dir，不受影响）
    shutil.rmtree(job, ignore_errors=True)
    return out_path
