#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按文件夹合成:SD 卡上每个分类文件夹 → 一条完整竖屏成片。

每条片段:有人声 → 剪掉说话空档 + 字幕;没人声 → 画面原样保留。
统一竖屏 + 轻美颜,按时间顺序拼接,输出 output/<文件夹名>.mp4。
用法:
  python3 make_folder_videos.py            # 处理所有文件夹
  python3 make_folder_videos.py 麦当劳AI办公 国庆烟花夜   # 只处理指定文件夹
"""

import sys
import shutil
from pathlib import Path

import make_shorts as ms

CARD = Path("/Volumes/OsmoAction/DCIM/DJI_001")
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
WORK_DIR = ROOT / ".work_folders"

FOLDERS = [
    "麦当劳AI办公", "健身房训练", "小企业博览会", "Harborview收据AI演示",
    "麦当劳街头觅食", "柯基咖啡厅客户会面", "活动夜与柯基", "深夜部署OpenDeploy",
    "公寓派对与天台夜景", "Dogpatch老船厂探访", "国庆烟花夜", "绿墙酒吧朋友夜聚",
    "青旅日常", "印度餐厅吃播", "列治文逛街探店",
]

# 拼接前统一的规格(所有中间片段编码参数一致,拼接才能无损秒拼)
NORM_FPS = "30"
NORM_AR = "48000"


def list_clips(folder: Path):
    """按文件名(时间)排序列出片段;某编号只有 LRF 没 MP4 时用 LRF 顶上。"""
    mp4s = {f.name[:29]: f for f in folder.iterdir()
            if f.suffix.upper() == ".MP4" and not f.name.startswith("._")}
    lrfs = {f.name[:29]: f for f in folder.iterdir()
            if f.suffix.upper() == ".LRF" and not f.name.startswith("._")}
    clips = dict(lrfs)
    clips.update(mp4s)  # MP4 优先
    return [clips[k] for k in sorted(clips)]


def reframe_burn_normalized(src, srt_name, dst, workdir):
    """竖屏化(+美颜+字幕),并统一 fps/音频规格,供无损拼接。"""
    style = (
        f"FontName={ms.CONFIG['font_name']},"
        f"FontSize={ms.CONFIG['font_size']},"
        f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline=2,Shadow=0,"
        f"Alignment=2,MarginV={ms.CONFIG['subtitle_margin_v']}"
    )
    base = ms.build_video_filter()
    if srt_name is None:
        fc = f"{base};[vid]null[out]"
    else:
        fc = f"{base};[vid]subtitles={srt_name}:force_style='{style}'[out]"
    cmd = [
        "ffmpeg", "-y", "-i", str(Path(src).resolve()),
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        "-r", NORM_FPS,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", NORM_AR, "-ac", "2",
        str(Path(dst).resolve()),
    ]
    ms.run(cmd, cwd=str(workdir))


def process_clip(clip: Path, job: Path, idx: int):
    """单条片段 → 处理好的中间竖屏片段。返回中间文件路径,失败/太短返回 None。"""
    try:
        dur = ms.get_duration(clip)
    except Exception:
        print(f"    ⚠️ 读不了时长,跳过 {clip.name}")
        return None
    if dur < 0.5:
        print(f"    ⚠️ 太短({dur:.1f}s),跳过 {clip.name}")
        return None

    cdir = job / f"c{idx:03d}"
    cdir.mkdir(parents=True, exist_ok=True)

    words = ms.step_transcribe_words(clip)
    trimmed = cdir / "trimmed.mp4"
    if words:
        keep = ms.build_keep_from_words(words, dur)
        cut_total = dur - sum(e - s for s, e in keep)
        print(f"    说话段 {len(keep)} 段,剪掉空档 {cut_total:.1f}s / {dur:.1f}s")
        ms.step_cut(clip, keep, trimmed)
        lines = ms.words_to_lines(words, keep)
    else:
        print(f"    无人声,画面保留 {dur:.1f}s")
        shutil.copy(str(clip), str(trimmed))
        lines = []

    srt_name = None
    if lines:
        ms.write_srt(lines, cdir / "sub.srt")
        srt_name = "sub.srt"

    part = cdir / "part.mp4"
    reframe_burn_normalized(trimmed, srt_name, part, cdir)
    trimmed.unlink(missing_ok=True)
    return part


def process_folder(name: str):
    folder = CARD / name
    if not folder.is_dir():
        print(f"❌ 找不到文件夹:{name}")
        return None
    clips = list_clips(folder)
    if not clips:
        print(f"❌ {name} 里没有视频")
        return None

    print(f"\n=== 文件夹:{name}({len(clips)} 条片段)===")
    job = WORK_DIR / name
    if job.exists():
        shutil.rmtree(job)
    job.mkdir(parents=True)

    parts = []
    for i, clip in enumerate(clips):
        print(f"  [{i+1}/{len(clips)}] {clip.name}")
        try:
            p = process_clip(clip, job, i)
            if p:
                parts.append(p)
        except Exception as e:
            print(f"    ❌ 片段失败,跳过:{e}")

    if not parts:
        print(f"❌ {name} 没有可用片段")
        return None

    # 无损拼接
    lst = job / "concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
    merged = job / "merged.mp4"
    ms.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", str(merged)])

    # 开头 5 秒烧标题(文件夹名,顶部居中黑底白字)
    out = OUTPUT_DIR / f"{name}.mp4"
    ass = job / "title.ass"
    with open(ass, "w", encoding="utf-8") as f:
        f.write(
            "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, "
            "Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
            "Style: Title,PingFang SC,84,&H00FFFFFF,&H70000000,1,3,14,0,8,40,40,260\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n"
            f"Dialogue: 0,0:00:00.00,0:00:05.00,Title,{name}\n"
        )
    ms.run(["ffmpeg", "-y", "-i", str(merged), "-vf", "ass=title.ass",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart", str(out)],
           cwd=str(job))
    shutil.rmtree(job)
    print(f"✅ 成片:output/{out.name}")
    return out


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    WORK_DIR.mkdir(exist_ok=True)
    targets = sys.argv[1:] or FOLDERS
    ok, fail = [], []
    for name in targets:
        try:
            r = process_folder(name)
            (ok if r else fail).append(name)
        except Exception as e:
            print(f"❌ {name} 失败:{e}")
            fail.append(name)
    print("\n========== 全部完成 ==========")
    print(f"成功 {len(ok)} 个文件夹,失败 {len(fail)} 个")
    if fail:
        print("失败:", ", ".join(fail))


if __name__ == "__main__":
    main()
