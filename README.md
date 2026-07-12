# koubo-shorts

口播 → 竖屏短视频 自动化流水线。开源工具、零 API 费用、全本地运行。

处理流程:去口水/去静音 → 语音转字幕 → 烧字幕 → 竖屏化(9:16)

## 用法
```bash
bash install.sh          # 首次:装 ffmpeg + faster-whisper
# 把原片拖进 input/
python3 make_shorts.py   # 成片出现在 output/
```

详见 `使用说明.md`。依赖:ffmpeg + faster-whisper(本地) + Python3。视频不上传任何第三方。
