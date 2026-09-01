# -*- coding: utf-8 -*-
"""
render_with_voice.py — ナレーション付きで動画を書き出す入口

    bash setup_voice.sh                                    # 初回だけ。冪等
    python3 render_with_voice.py script.json out.mp4 [--seed N] [--no-bgm] [--no-voice]

render_anim.py は bundle.py の自動生成物で「直接編集しないこと」と書かれているため、
そちらには手を入れず、ライブラリとして読み込んでこのファイルで組み立て直している。
絵作り（look / shots / frames / BGM）は render_anim.py のものをそのまま使う。

このファイルがやっていること:

  1. 各ビートを読み上げ、**テロップの時刻を読み上げの実尺から決め直す**
     （台本の `t` は捨てる。声と字がズレるのが一番みっともない）
  2. 声が乗っている区間だけ BGM を下げる
  3. 末尾に「VOICEVOX:ずんだもん」を焼き込む（**利用条件。消さないこと**）
  4. 山場（surprise）の無い台本を exit 2 で落とす

OOM 対策（本番は 1CPU / RAM 985MB）:

  - 合成器は 278MB 常駐するので **subprocess に分離**し、描画前にメモリを手放す
  - エンコードは **映像だけ → 音声をmux** の2段。一発で通すと毎回殺されていた
  - 一時ファイルは pid 付き。並行実行と手動リカバリのため

声の合成に失敗しても、止めずに無音へ落として書き出す
（`運用制約_守るべき前提` §5「ルーティンを止めない」）。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave

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


def _load_voice(vdir, beats, dur):
    """サブプロセスが吐いた wav を、決まった時刻に並べて1本にする。"""
    buf = np.zeros(int(dur * VE.SR) + VE.SR, dtype=np.float32)
    for i, b in enumerate(beats):
        path = os.path.join(vdir, "beat_%02d.wav" % i)
        if not os.path.exists(path):
            continue
        with wave.open(path, "rb") as w:
            x = np.frombuffer(w.readframes(w.getnframes()),
                              dtype=np.int16).astype(np.float32) / 32768.0
        j = int(b["t"] * VE.SR)
        buf[j:j + len(x)] += x
    pk = float(np.abs(buf).max())
    if pk > 0:
        buf *= 0.92 / pk
    return buf[:int(dur * VE.SR)]


def check_climax(look, flags):
    """山場（surprise）が無い台本は落とす。上流タスクのパッチと同じ判定。"""
    faces = [sh.get("face") for sh in look["shots"]]
    print("  face=%s" % "/".join(str(f) for f in faces))
    if "surprise" not in faces and "--allow-flat" not in flags:
        print("NG: surprise が無い。山場が立っていない。"
              "「だけ」は flat に上書きされるので山場では使わないこと。"
              "または該当ビートに face=surprise を明示する。")
        sys.exit(2)


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
    #
    # 合成は **必ず別プロセス**。合成器が 278MB 常駐したまま Chromium を起動すると
    # RAM 985MB のサンドボックスでは OOM で落ちる。
    voice = None
    if "--no-voice" not in flags:
        vdir = os.path.join(HERE, "_voice_%d" % os.getpid())
        try:
            print("synthesizing narration (subprocess)...")
            r = subprocess.run([sys.executable, os.path.join(HERE, "voice_engine.py"),
                                args[0], vdir],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-500:])
            tm = json.load(open(os.path.join(vdir, "timing.json"), encoding="utf-8"))
            dur = tm["duration"]
            beats = [dict(b, t=t) for b, t in zip(beats, tm["t"])]
            voice = _load_voice(vdir, beats, dur)
            print("  %d beats / %.1fs（台本の t は読み上げ尺で上書き）" % (len(beats), dur))
        except Exception as e:
            print("WARNING: ナレーション合成に失敗。無音で続行する: %r" % (e,))
            voice = None
        finally:
            shutil.rmtree(vdir, ignore_errors=True)

    mood = R.infer_mood(script)
    arc = R.infer_arc(script, mood)
    look = R.build_look(R.BRAND, mood, arc, seed, len(beats), beats=beats)
    look["handle"] = script.get("handle", "@gude_aiai")
    print("look: %s  seed=%d" % (look["label"], seed))
    check_climax(look, flags)

    fd = os.path.join(HERE, "_frames_%d" % os.getpid())
    shutil.rmtree(fd, ignore_errors=True)
    print("rendering frames...")
    n_frames = R.render_frames(beats, dur, look, fd)

    with_bgm = "--no-bgm" not in flags
    wav = os.path.join(HERE, "_mix_%d.wav" % os.getpid())
    has_audio = with_bgm or voice is not None
    if has_audio:
        VE.write_wav(wav, build_audio(dur, mood, seed, beats, voice, with_bgm))

    # --- エンコードは2段に分ける（一発で通すと OOM で殺される） ------------
    vonly = os.path.join(HERE, "_v_%d.mp4" % os.getpid())
    vcmd = ["ffmpeg", "-y", "-loglevel", "error",
            "-framerate", str(R.FPS), "-i", os.path.join(fd, "f%05d.png")]
    if voice is not None:
        vcmd += ["-vf", _credit_filter(dur)]
    vcmd += ["-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
             "-preset", "faster", "-crf", "25", "-pix_fmt", "yuv420p", "-threads", "1",
             "-x264-params",
             "rc-lookahead=10:sync-lookahead=0:threads=1:sliced-threads=0",
             "-an", vonly]
    r = subprocess.run(vcmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        print("HINT: フレームは %s に残っている。エンコードだけ回し直せる。" % fd)
        sys.exit(1)

    if has_audio:
        mcmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", vonly, "-i", wav,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-movflags", "+faststart", out]
    else:
        mcmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", vonly,
                "-c:v", "copy", "-movflags", "+faststart", out]
    r = subprocess.run(mcmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:])
        sys.exit(1)
    os.remove(vonly)

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
