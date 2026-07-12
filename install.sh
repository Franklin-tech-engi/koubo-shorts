#!/bin/bash
# ============================================================
# 口播→竖屏短视频 自动化流水线 · Mac 一键安装
# 第一次使用时，双击运行本脚本，或在终端里执行： bash install.sh
# ============================================================
set -e

echo ""
echo "==== 开始安装口播剪辑流水线所需环境 ===="
echo ""

# 1) 检查 / 安装 Homebrew（Mac 的软件包管理器）
if ! command -v brew >/dev/null 2>&1; then
  echo "[1/4] 未检测到 Homebrew，正在安装（可能需要你输入 Mac 密码）..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # 让当前终端能立刻用上 brew
  if [ -d /opt/homebrew/bin ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
  if [ -d /usr/local/bin ]; then eval "$(/usr/local/bin/brew shellenv 2>/dev/null || true)"; fi
else
  echo "[1/4] Homebrew 已安装 ✅"
fi

# 2) 安装 ffmpeg（剪辑主力）
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[2/4] 正在安装 ffmpeg ..."
  brew install ffmpeg
else
  echo "[2/4] ffmpeg 已安装 ✅"
fi

# 3) 确认 python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "[3/4] 正在安装 python3 ..."
  brew install python
else
  echo "[3/4] python3 已安装 ✅"
fi

# 4) 安装 faster-whisper（语音转字幕）
echo "[4/4] 正在安装 faster-whisper（字幕识别）..."
python3 -m pip install --user --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install --user faster-whisper

echo ""
echo "==== 安装完成！ ===="
echo ""
echo "用法："
echo "  1. 把要处理的原片拖进  input/  文件夹"
echo "  2. 在这个文件夹里运行： python3 make_shorts.py"
echo "  3. 成片会出现在  output/  文件夹"
echo ""
echo "第一次运行会自动下载字幕模型（约几百 MB，只下这一次），请耐心等待。"
echo ""
