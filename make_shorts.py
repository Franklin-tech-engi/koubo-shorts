#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口播 → 竖屏短视频 自动化流水线（本地批量版）

用法：
    把原片放进 input/ 文件夹，运行本脚本，成片自动出现在 output/。
        python3 make_shorts.py            # 处理 input/ 里所有视频
        python3 make_shorts.py a.mp4 b.mp4 # 只处理指定文件名

核心逻辑在 pipeline.py（Web 版也复用它）。想改参数（字体/美颜/分辨率等）
去 pipeline.py 里的 DEFAULT_CONFIG。
"""

import sys
from pathlib import Path

from pipeline import VIDEO_EXTS, process_video

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
WORK_DIR = ROOT / ".work"


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
        print(f"\n=== 处理：{v.name} ===")
        try:
            out = process_video(v, OUTPUT_DIR, WORK_DIR)
            ok.append(out)
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
