# -*- coding: utf-8 -*-
"""
voice_engine.py — 台本の各ビートを読み上げ、尺から逆算してビート時刻を決める

これまでの動画は無音（BGMのみ）だった。無音の動画は数秒で離脱されるため、
ナレーションを最優先の改善として入れる。

設計の要点:

  1. **ビート時刻は台本に書かれた `t` ではなく、読み上げの実尺から決める。**
     テロップと声がズレるのが一番みっともないので、声を先に作って絵を合わせる。
  2. 合成は VOICEVOX（ずんだもん・ノーマル）。完全にオフラインで動く。
     モデルと実行時ライブラリは GitHub から取得する（setup_voice.sh 参照）。
  3. **クレジット表記「VOICEVOX:ずんだもん」は必須。** 動画末尾に焼き込む。
     これは無料利用の条件であり、外すことはできない。

使い方:

    from voice_engine import VoiceEngine, layout_beats, build_voice_track
    ve = VoiceEngine()
    clips = ve.synth_beats(beats)              # 各ビートの音声と実尺
    beats, dur = layout_beats(beats, clips)    # t を書き換え、総尺を返す
    track = build_voice_track(clips, beats, dur)
"""

import os
import wave
import numpy as np

SR = 44100          # 最終出力（BGMと同じ）
VV_SR = 48000       # VOICEVOX の出力。24000 の倍数でないと受け付けない

# VOICEVOX ずんだもん・ノーマル
STYLE_ID = 3
CREDIT = "VOICEVOX:ずんだもん"

# 既定の間の取り方（秒）
LEAD_IN = 0.35      # 頭の余白。いきなり喉り出さない
BEAT_GAP = 0.42     # ビートとビートのあいだ
TAIL = 1.60         # 末尾の余白。クレジットを出す時間でもある

VOICE_DIR = os.environ.get("GUDE_VOICE_DIR", "/opt/gude-voice")


def _p(*a):
    return os.path.join(VOICE_DIR, *a)


class VoiceEngine:
    """VOICEVOX を1度だけ初期化して使い回す。"""

    def __init__(self, style_id=STYLE_ID, speed=1.0, pitch=0.0, intonation=1.12):
        from voicevox_core.blocking import (
            Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile)
        ort = Onnxruntime.load_once(filename=_p("lib", "libvoicevox_onnxruntime.so.1.23.2"))
        oj = OpenJtalk(_p("openjtalk_dic"))
        self.syn = Synthesizer(ort, oj)
        for f in sorted(os.listdir(_p("models"))):
            if f.endswith(".vvm"):
                with VoiceModelFile.open(_p("models", f)) as m:
                    self.syn.load_voice_model(m)
        self.style_id = style_id
        self.speed = speed
        self.pitch = pitch
        self.intonation = intonation

    def say(self, text):
        """1行を読み上げて (float32 mono @SR, 秒) を返す。"""
        q = self.syn.create_audio_query(text, self.style_id)
        q.speed_scale = self.speed
        q.pitch_scale = self.pitch
        # 拑揚はやや強めにする。淡々と読むと雑学は頭に入らない
        q.intonation_scale = self.intonation
        q.pre_phoneme_length = 0.06
        q.post_phoneme_length = 0.16
        q.output_sampling_rate = VV_SR
        q.output_stereo = False
        raw = self.syn.synthesis(q, self.style_id)
        x = _resample(_wav_bytes_to_mono(raw), VV_SR, SR)
        return x, len(x) / SR

    def synth_beats(self, beats):
        """各ビートの telop を読み上げる。telop が空のビートは無音扱い。"""
        clips = []
        for b in beats:
            text = (b.get("say") or b.get("telop") or "").strip()
            if not text:
                clips.append((np.zeros(int(SR * 0.9), dtype=np.float32), 0.9))
                continue
            clips.append(self.say(_read_aloud(text)))
        return clips


def _read_aloud(t):
    """テロップをそのまま読ませると不自然になる箇所を直す。"""
    t = t.replace("…", "、").replace("——", "、").replace("—", "、")
    t = t.replace("％", "パーセント").replace("%", "パーセント")
    t = t.replace("〜", "から").replace("～", "から")
    # 行末の体言止めは、読み上げでは間が抜けるので句点を足す
    if t and t[-1] not in "。！？!?、":
        t += "。"
    return t


def _resample(x, src, dst):
    """VOICEVOX の 48kHz を BGM と同じ 44.1kHz に落とす。"""
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    t = np.linspace(0.0, len(x) - 1.0, n)
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


def _wav_bytes_to_mono(raw):
    import io
    with wave.open(io.BytesIO(raw), "rb") as w:
        n, ch, sw = w.getnframes(), w.getnchannels(), w.getsampwidth()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data


def layout_beats(beats, clips, lead=LEAD_IN, gap=BEAT_GAP, tail=TAIL):
    """読み上げの実尺からビート時刻を決め直し、総尺を返す。

    台本に書かれた `t` は捨てる。声が先、絵が後。
    """
    t = lead
    out = []
    for b, (_, sec) in zip(beats, clips):
        nb = dict(b)
        nb["t"] = round(t, 3)
        out.append(nb)
        t += sec + gap
    total = round(t - gap + tail, 2)
    return out, total


def build_voice_track(clips, beats, duration, sr=SR):
    """各ビートの音声を、決まった時刻に置いた1本のトラックにする。"""
    buf = np.zeros(int(duration * sr) + sr, dtype=np.float32)
    for b, (x, _) in zip(beats, clips):
        i = int(b["t"] * sr)
        buf[i:i + len(x)] += x
    peak = float(np.abs(buf).max())
    if peak > 0:
        buf *= 0.92 / peak
    return buf[:int(duration * sr)]


def duck(bgm, voice, floor=0.34, attack=0.06, release=0.45, sr=SR):
    """声が乗っている区間だけ BGM を下げる。単純なエンベロープ追従。"""
    n = min(len(bgm), len(voice))
    env = np.abs(voice[:n])
    win = max(1, int(0.02 * sr))
    env = np.convolve(env, np.ones(win) / win, mode="same")
    active = (env > 0.02).astype(np.float32)
    a = np.exp(-1.0 / max(1, attack * sr))
    r = np.exp(-1.0 / max(1, release * sr))
    g = np.ones(n, dtype=np.float32)
    cur = 1.0
    for i in range(n):
        tgt = floor if active[i] > 0.5 else 1.0
        k = a if tgt < cur else r
        cur = tgt + (cur - tgt) * k
        g[i] = cur
    out = bgm.copy()
    if out.ndim == 2:
        out[:n] *= g[:, None]
    else:
        out[:n] *= g
    return out


def write_wav(path, x, sr=SR):
    y = np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0)
    ch = 2 if (y.ndim == 2 and y.shape[1] == 2) else 1
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())
