#!/usr/bin/env bash
# =============================================================================
# setup_voice.sh — ナレーション合成（VOICEVOX）の実行環境を用意する
#
# 日次タスクのコンテナは毎回まっさらなので、レンダリング前に必ずこれを走らせる。
# 冪等。既に置いてあれば何もしない。
#
#   bash engine/setup_voice.sh
#
# なぜこの構成なのか:
#   この環境は外部通信が許可リストで絞られていて、pypi と GitHub しか通らない。
#   VOICEVOX は本体もモデルも GitHub から取れるので、通信の壁を越えずに済む。
#   （edge-tts や各社のクラウドTTSは 403 で到達できない。2026-09-01 実測）
#
# クレジット表記「VOICEVOX:ずんだもん」は利用条件。動画末尾に焼き込んでいる。
# render_with_voice.py 側の drawtext を消さないこと。
# =============================================================================
set -euo pipefail

D="${GUDE_VOICE_DIR:-/opt/gude-voice}"
VC_VER="0.17.0"
ORT_VER="1.23.2"

mkdir -p "$D/lib" "$D/models" "$D/openjtalk_dic"

# --- Python 側 ---------------------------------------------------------------
if ! python3 -c "import voicevox_core" 2>/dev/null; then
  echo "[voice] installing voicevox_core ${VC_VER}"
  WHL="voicevox_core-${VC_VER}-cp310-abi3-manylinux_2_34_x86_64.whl"
  curl -sSL --max-time 300 -o "/tmp/$WHL" \
    "https://github.com/VOICEVOX/voicevox_core/releases/download/${VC_VER}/${WHL}"
  pip install "/tmp/$WHL" --break-system-packages -q
fi

# 辞書は pyopenjtalk のものを流用する（同じ open_jtalk 1.11）
if ! python3 -c "import pyopenjtalk" 2>/dev/null; then
  echo "[voice] installing pyopenjtalk (辞書のため)"
  pip install pyopenjtalk --break-system-packages -q
fi

# --- ONNX Runtime ------------------------------------------------------------
if [ ! -f "$D/lib/libvoicevox_onnxruntime.so.${ORT_VER}" ]; then
  echo "[voice] fetching onnxruntime ${ORT_VER}"
  curl -sSL --max-time 300 -o /tmp/ort.tgz \
    "https://github.com/VOICEVOX/onnxruntime-builder/releases/download/voicevox_onnxruntime-${ORT_VER}/voicevox_onnxruntime-linux-x64-${ORT_VER}.tgz"
  tar xzf /tmp/ort.tgz -C /tmp
  cp "/tmp/voicevox_onnxruntime-linux-x64-${ORT_VER}/lib/libvoicevox_onnxruntime.so.${ORT_VER}" "$D/lib/"
fi

# --- 音声モデル（0.vvm に ずんだもん が入っている） --------------------------
if [ ! -f "$D/models/0.vvm" ]; then
  echo "[voice] fetching voice model"
  curl -sSL --max-time 300 -o "$D/models/0.vvm" \
    "https://raw.githubusercontent.com/VOICEVOX/voicevox_vvm/main/vvms/0.vvm"
fi

# --- Open JTalk 辞書 ---------------------------------------------------------
if [ ! -f "$D/openjtalk_dic/sys.dic" ]; then
  echo "[voice] staging open_jtalk dictionary"
  SRC=$(python3 -c "
import pyopenjtalk
p = pyopenjtalk.OPEN_JTALK_DICT_DIR
print(p.decode() if isinstance(p, bytes) else p)")
  cp -r "$SRC/." "$D/openjtalk_dic/"
fi

echo "[voice] ready: $D"
