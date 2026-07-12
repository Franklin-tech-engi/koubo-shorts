#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harborview收据AI演示 精选版:选关键片段,压到 2 分钟内。"""
import shutil
from pathlib import Path
import make_shorts as ms
import make_folder_videos as mfv

NAME = "Harborview收据AI演示"
FOLDER = Path("/Volumes/OsmoAction/DCIM/DJI_001") / NAME
JOB = mfv.WORK_DIR / (NAME + "_2min")
OUT = mfv.OUTPUT_DIR / f"{NAME}.mp4"
MAX_SEC = 118  # 含5秒标题,总长压在2分钟内

# 精选片段(叙事顺序):场景→AI读收据→结果讲解→手机传流程→给Kevin便签→行走收尾
PICK_IDS = ["0021", "0023", "0024", "0027", "0036", "0040"]

def process_clip_tight(clip, job, idx):
    """只保留说话段:无人声片段直接丢弃。"""
    import shutil as _sh
    dur = ms.get_duration(clip)
    if dur < 0.5:
        return None
    cdir = job / f"c{idx:03d}"
    cdir.mkdir(parents=True, exist_ok=True)
    words = ms.step_transcribe_words(clip)
    if not words:
        print("    无人声,丢弃")
        return None
    keep = ms.build_keep_from_words(words, dur)
    trimmed = cdir / "trimmed.mp4"
    ms.step_cut(clip, keep, trimmed)
    lines = ms.words_to_lines(words, keep)
    srt_name = None
    if lines:
        ms.write_srt(lines, cdir / "sub.srt")
        srt_name = "sub.srt"
    part = cdir / "part.mp4"
    mfv.reframe_burn_normalized(trimmed, srt_name, part, cdir)
    return part

def main():
    ms.CONFIG["max_pause"] = 0.5
    ms.CONFIG["pause_head"] = 0.2
    ms.CONFIG["pause_tail"] = 0.3
    if JOB.exists():
        shutil.rmtree(JOB)
    JOB.mkdir(parents=True)
    clips = mfv.list_clips(FOLDER)
    chosen = [c for pid in PICK_IDS for c in clips if f"_{pid}_" in c.name]
    print(f"精选 {len(chosen)} 条片段")
    parts, total = [], 0.0
    for i, clip in enumerate(chosen):
        print(f"  [{i+1}/{len(chosen)}] {clip.name}")
        p = process_clip_tight(clip, JOB, i)
        if not p:
            continue
        d = ms.get_duration(p)
        if total >= MAX_SEC:
            print(f"    已达 {MAX_SEC}s 上限,丢弃后续")
            break
        if total + d > MAX_SEC:
            cut = MAX_SEC - total
            p2 = p.with_name("part_cut.mp4")
            ms.run(["ffmpeg", "-y", "-i", str(p), "-t", f"{cut:.2f}",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                    "-r", "30", str(p2)])
            p, d = p2, cut
        parts.append(p); total += d
        print(f"    累计 {total:.1f}s")
    lst = JOB / "concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
    merged = JOB / "merged.mp4"
    ms.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(merged)])
    ass = JOB / "title.ass"
    with open(ass, "w", encoding="utf-8") as f:
        f.write("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
                "Style: Title,PingFang SC,84,&H00FFFFFF,&H70000000,1,3,14,0,8,40,40,260\n\n"
                "[Events]\nFormat: Layer, Start, End, Style, Text\n"
                f"Dialogue: 0,0:00:00.00,0:00:05.00,Title,{NAME}\n")
    ms.run(["ffmpeg", "-y", "-i", str(merged), "-vf", "ass=title.ass",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart", str(OUT)], cwd=str(JOB))
    shutil.rmtree(JOB)
    print(f"✅ 成片:output/{OUT.name} 总长 {ms.get_duration(OUT):.1f}s")

if __name__ == "__main__":
    main()
