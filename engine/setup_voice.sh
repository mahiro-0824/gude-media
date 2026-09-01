#!/usr/bin/env bash
# =============================================================================
# setup_voice.sh — ナレーション合成（VOICEVOX）の実行環境を用意する
#
# 日次タスクのサンドボックスは毎回まっさらなので、レンダリング前に必ずこれを走らせる。
# 冪等。既に置いてあれば何もしない。所要は初回約1分、180MB。
#
#   bash setup_voice.sh
#
# なぜこの構成なのか:
#   この環境は外部通信が許可リストで絞られているが、GitHub は通る。
#   VOICEVOX は本体もモデルも辞書も GitHub から取れるので、完全にオフラインで回る。
#   （edge-tts や各社のクラウドTTSは 403 で到達できない。2026-09-01 実測）
#
# クレジット表記「VOICEVOX:ずんだもん」は利用条件。動画末尾に焼き込んでいる。
# render_with_voice.py 側の drawtext を消さないこと。
# =============================================================================
set -euo pipefail

# /tmp は tmpfs（RAM）なので使わない。180MB 置くとメモリが厄介になる
D="${GUDE_VOICE_DIR:-/var/tmp/gude-voice}"
VC_VER="0.17.0"
ORT_VER="1.23.2"

mkdir -p "$D/lib" "$D/models" "$D/openjtalk_dic"

# --- Python 側（wheel は abi3。py3.10～3.13 でそのまま入る） ------------------
if ! python3 -c "import voicevox_core" 2>/dev/null; then
  echo "[voice] installing voicevox_core ${VC_VER}"
  WHL="voicevox_core-${VC_VER}-cp310-abi3-manylinux_2_34_x86_64.whl"
  curl -sSL --max-time 300 -o "/var/tmp/$WHL" \
    "https://github.com/VOICEVOX/voicevox_core/releases/download/${VC_VER}/${WHL}"
  # ファイル名を変えると pip に弾かれるので、この名前のまま入れること
  pip install "/var/tmp/$WHL" -q 2>/dev/null || pip install "/var/tmp/$WHL" -q --break-system-packages
fi

# --- ONNX Runtime ------------------------------------------------------------
if [ ! -f "$D/lib/libvoicevox_onnxruntime.so.${ORT_VER}" ]; then
  echo "[voice] fetching onnxruntime ${ORT_VER}"
  curl -sSL --max-time 300 -o /var/tmp/ort.tgz \
    "https://github.com/VOICEVOX/onnxruntime-builder/releases/download/voicevox_onnxruntime-${ORT_VER}/voicevox_onnxruntime-linux-x64-${ORT_VER}.tgz"
  tar xzf /var/tmp/ort.tgz -C /var/tmp
  cp "/var/tmp/voicevox_onnxruntime-linux-x64-${ORT_VER}/lib/libvoicevox_onnxruntime.so.${ORT_VER}" "$D/lib/"
fi

# --- 音声モデル（0.vvm に ずんだもん が入っている） --------------------------
if [ ! -f "$D/models/0.vvm" ]; then
  echo "[voice] fetching voice model"
  curl -sSL --max-time 300 -o "$D/models/0.vvm" \
    "https://raw.githubusercontent.com/VOICEVOX/voicevox_vvm/main/vvms/0.vvm"
fi

# --- Open JTalk 辞書（GitHub から直接。pyopenjtalk は 3.13 で入らないことがある）
if [ ! -f "$D/openjtalk_dic/sys.dic" ]; then
  echo "[voice] fetching open_jtalk dictionary"
  curl -sSL --max-time 300 -o /var/tmp/ojt.tgz \
    "https://github.com/r9y9/open_jtalk/releases/download/v1.11.1/open_jtalk_dic_utf_8-1.11.tar.gz"
  tar xzf /var/tmp/ojt.tgz -C /var/tmp
  cp -r /var/tmp/open_jtalk_dic_utf_8-1.11/. "$D/openjtalk_dic/"
fi

echo "[voice] ready: $D"
