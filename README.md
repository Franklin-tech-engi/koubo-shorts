# koubo-shorts

口播 → 竖屏短视频 自动化流水线。开源、零 API 费用。

**处理流程**:语音识别定位说话段 → 剪掉不说话的空档(停顿/撩头发等) → 自动生成字幕 → 竖屏化 9:16 + 轻美颜 + 烧字幕。

有两种用法:**本地命令行**(适合自己在电脑上批量跑)和 **Web 拖拽上传**(适合部署后发给不懂技术的人用)。

---

## 一、本地命令行(纯本地、不联网上传视频)

```bash
bash install.sh          # 首次:装 ffmpeg + faster-whisper(Mac)
# 把原片拖进 input/
python3 make_shorts.py            # 处理 input/ 里所有视频,成片出现在 output/
python3 make_shorts.py a.mp4      # 只处理指定文件
```

依赖:`ffmpeg` + `faster-whisper` + Python3。视频不上传任何第三方。
参数(字体/美颜/分辨率/字幕样式等)都在 `pipeline.py` 顶部的 `DEFAULT_CONFIG`。

## 二、Web 拖拽上传版(自己部署一份给别人用)

打开网页 → 拖视频进去 → 排队处理 → 下载竖屏成片。为控制服务器成本,采用**单 worker 串行处理 + 排队**:同一时间只处理一条,人多时排队。

**用 Docker 跑(推荐,自带 ffmpeg 和中文字体):**
```bash
docker build -t koubo-shorts .
docker run -p 8000:8000 -v $PWD/data:/data koubo-shorts
# 打开 http://localhost:8000
```

**本地直接跑(需自备 ffmpeg + 中文字体):**
```bash
pip install -r requirements.txt
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

**可调环境变量:**

| 变量 | 默认 | 说明 |
|------|------|------|
| `WHISPER_MODEL` | `small` | 识别模型 tiny/base/small/medium;算力紧张可用 base |
| `SUBTITLE_FONT` | `PingFang SC` | 字幕字体;Linux 部署设为 `Noto Sans CJK SC`(Docker 已自动设) |
| `MAX_UPLOAD_MB` | `500` | 单文件大小上限 |
| `RETAIN_HOURS` | `6` | 成片保留小时数,超时自动清理 |
| `DATA_DIR` | `./data` | 上传/成片/临时文件目录,部署时指向持久化卷 |

## 架构

```
pipeline.py          核心流水线(本地 CLI 和 Web 后端共用)
make_shorts.py       本地批量命令行入口
server/app.py        Web 后端(上传 / 排队 / 状态 / 下载 / 自动清理)
server/static/       拖拽上传前端页面
Dockerfile           部署镜像(ffmpeg + Noto CJK 字体 + 预下载模型)
```

MIT 许可,欢迎自取自改。
