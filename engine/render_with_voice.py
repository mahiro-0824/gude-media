# -*- coding: utf-8 -*-
"""
render_with_voice.py — ナレーション付きで動画を書き出す入口

    bash engine/setup_voice.sh          # 初回だけ。冪等
    python3 engine/render_with_voice.py script.json out.mp4 [--seed N] [--no-bgm]

render_anim.py は bundle.py の自動生成物で「直接編集しないこと」と書かれているため、
そちらには手を入れず、ライブラリとして読み込んでこのファイルで組み立て直している。
絵作り（look / shots / frames / BGM）は render_anim.py のものをそのまま使う。

このファイルが足しているのは3つだけ:

  1. 各ビートを読み上げ、**テロップの時刻を読み上げの実尺から決め直す**
     （台本に書かれた `t` は捨てる。声と字がズレるのが一番みっともない）
  2. 声が乗っている区間だけ BGM を下げる
  3. 末尾に「VOICEVOX:ずんだもん」を焼き込む

3 は VOICEVOX の利用条件。**消さないこと。**

`--no-voice` を付けると従来どおり無音（BGMのみ）で書き出す。
声の合成に失敗したときも、止めずに無音へ落として書き出す
（`運用制約_守るべき前提` §5「ルーティンを止めない」）。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import render_anim as R          # noqa: E402
import voice_engine as VE        # noqa: E402

CREDIT_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"


def _credit_filter(dur):
    """末尾2秒だけクレジットを出す drawtext フィルタ。"""
    txt = VE.CREDIT.replace(":", r"\:")
    return ("drawtext=fontfile=%s:text='%s':fontcolor=0x7A7670:fontsize=20:"
            "x=(w-text_w)/2:y=h-56:alpha='if(lt(t,%.2f),0,0.85)'"
            % (CREDIT_FONT, txt, max(0.0, dur - 2.0)))


def build_audio(dur, mood, seed, beats, voice, with_bgm=True):
    """声と BGM を1本のステレオトラックにまとめる。"""
    n = int(dur * VE.SR)
    mix = np.zeros((n, 2), dtype=np.float32)

    if with_bgm:
        bgm = np.asarray(R.render_bgm(dur, mood, seed,
                                      beats_t=[b["t"] for b in beats]),
                         dtype=np.float32)
        if bgm.ndim == 1:
            bgm = np.stack([bgm, bgm], axis=1)
        if len(bgm) < n:
            bgm = np.pad(bgm, ((0, n - len(bgm)), (0, 0)))
        bgm = bgm[:n]
        if voice is not None:
            bgm = VE.duck(bgm, voice)
        mix += bgm * (0.42 if voice is not None else 1.0)

    if voice is not None:
        v = voice[:n]
        mix[:len(v), 0] += v
        mix[:len(v), 1] += v

    peak = float(np.abs(mix).max())
    if peak > 0:
        mix *= min(1.0, 0.95 / peak)
    return mix


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    script = json.load(open(args[0], encoding="utf-8"))
    out = args[1]
    beats, dur = script["beats"], script["duration"]

    seed = script.get("seed")
    for f in flags:
        if f.startswith("--seed"):
            seed = int(f.split("=")[1]) if "=" in f else None
    if seed is None:
        seed = int(hashlib.md5(os.path.basename(out).encode()).hexdigest()[:8], 16)

    # --- 声を先に作り、絵の時刻をそれに合わせる ------------------------
    voice = None
    if "--no-voice" not in flags:
        try:
            print("synthesizing narration...")
            ve = VE.VoiceEngine()
            clips = ve.synth_beats(beats)
            beats, dur = VE.layout_beats(beats, clips)
            voice = VE.build_voice_track(clips, beats, dur)
            print("  %d beats / %.1fs（台本の t は読み上げ尺で上書き）" % (len(beats), dur))
        except Exception as e:
            # 声が作れなくても投稿は止めない
            print("WARNING: ナレーション合成に失敗。無音で続行する: %r" % (e,))
            voice = None

    mood = R.infer_mood(script)
    arc = R.infer_arc(script, mood)
    look = R.build_look(R.BRAND, mood, arc, seed, len(beats), beats=beats)
    look["handle"] = script.get("handle", "@gude_aiai")
    print("look: %s  seed=%d" % (look["label"], seed))

    fd = os.path.join(HERE, "_frames")
    shutil.rmtree(fd, ignore_errors=True)
    print("rendering frames...")
    n_frames = R.render_frames(beats, dur, look, fd)

    with_bgm = "--no-bgm" not in flags
    wav = os.path.join(HERE, "_mix.wav")
    has_audio = with_bgm or voice is not None
    if has_audio:
        VE.write_wav(wav, build_audio(dur, mood, seed, beats, voice, with_bgm))

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-framerate", str(R.FPS), "-i", os.path.join(fd, "f%05d.png")]
    if has_audio:
        cmd += ["-i", wav]
    if voice is not None:
        cmd += ["-vf", _credit_filter(dur)]
    cmd += ["-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
            "-preset", "faster", "-crf", "26", "-pix_fmt", "yuv420p", "-threads", "2"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += ["-movflags", "+faststart", out]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        sys.exit(1)
    shutil.rmtree(fd, ignore_errors=True)

    mb = os.path.getsize(out) / 1024 / 1024
    print("OK %s  %.2f MB  %d frames  %.1fs  声=%s"
          % (out, mb, n_frames, dur, "あり" if voice is not None else "なし"))
    if mb > 9.5:
        print("WARNING: 10MB上限に近い。crf を上げるか尺を詰めること。")
    if dur > 18.0:
        print("WARNING: %.1f秒。台本が長い。1投稿1事実に絞って15秒以内に収めること。" % dur)


if __name__ == "__main__":
    main()
