# -*- coding: utf-8 -*-
"""@gude_aiai アニメーション動画レンダラ
scene.html を Chromium で1フレームずつ描画し、ffmpeg で mp4 にする。
テキストのスライドではなく、キャラクター・モーション・テロップを持つ動画を作る。

使い方:
    python3 render_anim.py script.json out.mp4
"""
import json, os, subprocess, sys, shutil, math
from playwright.sync_api import sync_playwright

W, H, FPS = 720, 1280, 30
HERE = os.path.dirname(os.path.abspath(__file__))
SCENE = "file://" + os.path.join(HERE, "scene.html")
CHROME = "/opt/pw-browsers/chromium"


def render_frames(beats, dur, frame_dir):
    os.makedirs(frame_dir, exist_ok=True)
    n = int(dur * FPS)
    with sync_playwright() as p:
        launch = {"args": ["--force-color-profile=srgb", "--font-render-hinting=none",
                           "--disable-lcd-text", "--hide-scrollbars"]}
        if os.path.exists(CHROME):
            launch["executable_path"] = CHROME
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        page.add_init_script(
            "window.__BEATS__=%s;window.__DUR__=%f;" % (json.dumps(beats, ensure_ascii=False), dur))
        page.goto(SCENE)
        page.wait_for_function("typeof window.seek === 'function'")
        page.evaluate("window.seek(0)")
        page.wait_for_timeout(400)  # フォント読み込み待ち
        for i in range(n):
            page.evaluate("window.seek(%f)" % (i / FPS))
            page.screenshot(path=os.path.join(frame_dir, "f%05d.png" % i))
            if i % 60 == 0:
                print("  frame %d/%d" % (i, n), flush=True)
        browser.close()
    return n


def build_bgm(dur, path):
    """numpy でゼロから合成した、著作権フリーのローファイ風パッド。"""
    import numpy as np
    sr = 44100
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    # 静かなコード(Fmaj9 相当)
    freqs = [174.61, 220.00, 261.63, 329.63]
    pad = sum(np.sin(2 * np.pi * f * t) / (i + 2.2) for i, f in enumerate(freqs))
    # ゆっくりした揺れ
    pad *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.09 * t)
    # 柔らかいパルス(90BPM)
    beat = (np.sin(2 * np.pi * 1.5 * t) > 0.985).astype(float)
    env = np.exp(-np.linspace(0, 26, int(sr * 0.16)))
    pulse = np.convolve(beat, env)[: len(t)] * np.sin(2 * np.pi * 88 * t) * 0.35
    mix = pad * 0.16 + pulse * 0.16
    # 前後のフェード
    fade = int(sr * 0.8)
    mix[:fade] *= np.linspace(0, 1, fade)
    mix[-fade:] *= np.linspace(1, 0, fade)
    mix = np.clip(mix, -1, 1)
    st = np.stack([mix, mix], axis=1)
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def main():
    script = json.load(open(sys.argv[1], encoding="utf-8"))
    out = sys.argv[2]
    beats, dur = script["beats"], script["duration"]

    fd = os.path.join(HERE, "_frames")
    shutil.rmtree(fd, ignore_errors=True)
    print("rendering frames...")
    n = render_frames(beats, dur, fd)

    wav = os.path.join(HERE, "_bgm.wav")
    build_bgm(dur, wav)

    cmd = ["ffmpeg", "-y",
           "-framerate", str(FPS), "-i", os.path.join(fd, "f%05d.png"),
           "-i", wav,
           "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
           "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest",
           "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:]); sys.exit(1)
    shutil.rmtree(fd, ignore_errors=True)
    mb = os.path.getsize(out) / 1024 / 1024
    print("OK %s  %.2f MB  %d frames  %.1fs" % (out, mb, n, dur))
    if mb > 9.5:
        print("WARNING: 10MB上限に近い。crf を上げるか尺を詰めること。")


if __name__ == "__main__":
    main()
