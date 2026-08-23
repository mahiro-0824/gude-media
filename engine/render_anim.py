# -*- coding: utf-8 -*-
# =============================================================================
# このファイルは bundle.py が生成したものです。直接編集しないこと。
# 元は engine/look_engine.py + engine/bgm_engine.py + engine/render_anim.py。
# 直すときは元のファイルを直して bundle.py を実行し、これを push する。
# =============================================================================
import colorsys, math, wave, json, os, subprocess, sys, shutil, hashlib
import numpy as np
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- look_engine
# -*- coding: utf-8 -*-
"""
look_engine.py — 回の内容から「見た目」と「展開」を決める

同じ台本エンジンから作っても毎回おなじ絵になるのを防ぐ。
決めるのは3つ:

  1. パレット   … ムード（内容のトーン）で系統が決まり、seed で微妙に振れる
  2. カット割り … 回のタイプ（比較 / 手順 / 数字 / 物語 / どんでん返し）で
                  カメラ・転換・テロップ位置の並びが決まる
  3. 質感       … 粒子・走査線・ブラーの量

ブランドの芯（ぐで太郎＝生成り×朱、うだっち＝黒×ネオングリーン）は動かさない。
動かすのは副アクセント・背景の色味・図版の色・転換の付け方。
"""

import colorsys


# ---------------------------------------------------------------- 色ユーティリティ

def hsl(h, s, l):
    h = (h % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def mix(hex_a, hex_b, k):
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * k) for x, y in zip(a, b))


# ---------------------------------------------------------------- ブランドの芯

BRANDS = {
    # 生成り × 朱。明るい紙の上の絵本
    "gude": dict(
        base="#F4F1EC", ink="#1C1E21", sub="#7A7670",
        anchor_hue=14, anchor_sat=0.64, anchor_lit=0.53,   # 朱 #D6583A
        sec_base=176,       # 副アクセントの起点（青緑）
        warm_hue=40,        # このブランドの「暖色」の居場所（金）
        lit_boost=0.0,
        dark=False, w=720, h=1280,
    ),
    # 黒 × ネオングリーン。暗い画面の上のネオン
    "udacchi": dict(
        base="#0A0A0D", ink="#EEF5EE", sub="#8A928A",
        anchor_hue=132, anchor_sat=1.0, anchor_lit=0.61,   # (57,255,90)
        sec_base=192,       # シアン寄り
        warm_hue=45,        # 琥珀。黒地でネオングリーンと喧嘩しない
        lit_boost=0.11,     # 暗い地なので副アクセントは明るめに
        dark=True, w=1080, h=1920,
    ),
}


# ---------------------------------------------------------------- ムード → 色の性格
#
# sec_from : 副アクセントの基準（"sec" = ブランドの副色起点 / "warm" = 暖色の居場所）
# sec_off  : 基準からのずらし（度）
# tint     : 背景に副アクセントを混ぜる量
# grain / blur / motion : 質感と動きの量

MOOD_LOOK = {
    "calm":    dict(sec_from="sec",  sec_off=-10, sec_sat=0.34, sec_lit=0.46, tint=0.055,
                    grain=0.045, blur=52, motion=0.55, ja="静かな回"),
    "bright":  dict(sec_from="warm", sec_off=6,   sec_sat=0.74, sec_lit=0.55, tint=0.070,
                    grain=0.035, blur=44, motion=1.00, ja="前向きな回"),
    "tense":   dict(sec_from="sec",  sec_off=34,  sec_sat=0.46, sec_lit=0.33, tint=0.090,
                    grain=0.075, blur=60, motion=0.70, ja="引っかかる回"),
    "playful": dict(sec_from="sec",  sec_off=150, sec_sat=0.78, sec_lit=0.60, tint=0.060,
                    grain=0.030, blur=40, motion=1.25, ja="軽い回"),
    "warm":    dict(sec_from="warm", sec_off=-12, sec_sat=0.54, sec_lit=0.50, tint=0.080,
                    grain=0.050, blur=56, motion=0.60, ja="ふりかえる回"),
    "focus":   dict(sec_from="sec",  sec_off=12,  sec_sat=0.30, sec_lit=0.44, tint=0.040,
                    grain=0.040, blur=46, motion=0.80, ja="淡々と積む回"),
}


# ---------------------------------------------------------------- 回のタイプ → カット割り
#
# cam   : ゆっくりした寄り引き。hold / push / pull / punch / driftL / driftR / tilt
# trans : ビートの変わり目の転換。fade / wipeUp / wipeDown / wipeSide / flash / blocks
# telop : テロップの置き場所。bottom / mid / top
# style : テロップの見せ方。bar / plain / rule

ARCS = {
    # 発端→やってみた→つまずき→転換→拍子抜け→結果
    "story": dict(
        ja="物語（連載の1話）",
        cam=["push", "hold", "push", "punch", "pull", "hold"],
        trans=["fade", "wipeUp", "wipeUp", "flash", "wipeSide", "fade"],
        telop=["bottom", "bottom", "bottom", "mid", "bottom", "bottom"],
        style=["bar", "bar", "bar", "plain", "bar", "rule"],
        props=[None, None, "checklist", None, "ba", None],
    ),
    # AとBを並べて見せる回
    "compare": dict(
        ja="比較",
        cam=["push", "driftL", "driftR", "hold", "punch", "pull"],
        trans=["fade", "wipeSide", "wipeSide", "blocks", "flash", "fade"],
        telop=["bottom", "bottom", "bottom", "bottom", "mid", "bottom"],
        style=["bar", "bar", "bar", "bar", "plain", "rule"],
        props=[None, "cards", "bars", "ba", "cards", None],
    ),
    # 手順を積み上げる回
    "steps": dict(
        ja="手順",
        cam=["push", "push", "push", "push", "pull", "hold"],
        trans=["fade", "wipeUp", "wipeUp", "wipeUp", "blocks", "fade"],
        telop=["bottom", "bottom", "bottom", "bottom", "bottom", "mid"],
        style=["bar", "bar", "bar", "bar", "bar", "plain"],
        props=[None, "checklist", "checklist", "phone", "bars", None],
    ),
    # 数字がひとつ主役の回
    "number": dict(
        ja="数字",
        cam=["hold", "push", "punch", "hold", "pull", "push"],
        trans=["fade", "wipeUp", "flash", "fade", "wipeSide", "fade"],
        telop=["bottom", "bottom", "mid", "bottom", "bottom", "bottom"],
        style=["bar", "bar", "plain", "bar", "bar", "rule"],
        props=[None, None, "count", "calendar", "bars", None],
    ),
    # 思い込みが外れる回
    "reveal": dict(
        ja="どんでん返し",
        cam=["hold", "hold", "tilt", "punch", "pull", "hold"],
        trans=["fade", "fade", "blocks", "flash", "wipeDown", "fade"],
        telop=["bottom", "bottom", "bottom", "mid", "bottom", "bottom"],
        style=["bar", "bar", "bar", "plain", "bar", "rule"],
        props=[None, "cards", "flow", None, "bars", None],
    ),
}

# ムードから「その回に合いそうな型」を先に絞る（内容とズレた展開を選ばないため）
MOOD_ARCS = {
    "calm":    ["story", "reveal"],
    "bright":  ["number", "story", "compare"],
    "tense":   ["reveal", "story"],
    "playful": ["reveal", "compare"],
    "warm":    ["story"],
    "focus":   ["steps", "compare", "number"],
}

# 台本の言葉から回のタイプを当てる
ARC_KEYWORDS = {
    "compare": ["比べ", "より", "どっち", "違い", "一方", "対して", "AとB", "差"],
    "steps":   ["まず", "つぎに", "次に", "手順", "ステップ", "やり方", "設定", "1つ目", "順番"],
    "number":  ["円", "％", "%", "時間", "分", "倍", "件", "人", "回", "日"],
    "reveal":  ["実は", "じつは", "思い込", "勘違", "まさか", "拍子抜け", "だけだった", "いらな"],
}


def infer_arc(script, mood):
    if isinstance(script, dict) and script.get("arc") in ARCS:
        return script["arc"]
    text = ""
    for b in script.get("beats", []):
        text += (b.get("telop", "") or "") + (b.get("kicker", "") or "") + (b.get("key", "") or "")
    allowed = MOOD_ARCS.get(mood, list(ARCS))
    score = {a: 0 for a in ARCS}
    for arc, words in ARC_KEYWORDS.items():
        for w in words:
            score[arc] += text.count(w)
    for a in allowed:
        score[a] += 1                      # ムードに合う型を優先
    return max(score, key=lambda k: score[k])


# ---------------------------------------------------------------- 組み立て

def _rot(seq, k):
    if not seq:
        return seq
    k %= len(seq)
    return list(seq[k:]) + list(seq[:k])


# 同じ役割どうしの言い換え。並び順（＝話の起伏）は動かさず、
# 各ビートの「やり方」だけを seed で入れ替える。
# こうしないと、山場のカットが冒頭に来て内容と展開がズレる。
CAM_VARIANTS = {
    "hold":   ["hold", "push", "pull"],
    "push":   ["push", "hold", "driftR"],
    "pull":   ["pull", "hold", "driftL"],
    "punch":  ["punch", "tilt"],          # 山場は山場のまま
    "tilt":   ["tilt", "punch"],
    "driftL": ["driftL", "driftR"],
    "driftR": ["driftR", "driftL"],
}
TRANS_VARIANTS = {
    "fade":     ["fade", "wipeUp"],
    "wipeUp":   ["wipeUp", "wipeDown", "wipeSide"],
    "wipeDown": ["wipeDown", "wipeUp"],
    "wipeSide": ["wipeSide", "blocks"],
    "flash":    ["flash"],                # 転換の山も固定
    "blocks":   ["blocks", "wipeSide"],
}
STYLE_VARIANTS = {
    "bar":   ["bar", "rule"],
    "rule":  ["rule", "bar"],
    "plain": ["plain"],                   # 強調の見せ方は固定
}


# ---------------------------------------------------------------- 表情
#
# 回の型ごとに「どのビートでどんな顔をしているか」を持つ。
# 話の流れと顔が合っていないと、動いていても嘘っぽく見える。

FACE_ARCS = {
    "story":   ["neutral", "think", "confused", "surprise", "happy", "flat"],
    "compare": ["neutral", "think", "think", "neutral", "surprise", "happy"],
    "steps":   ["neutral", "neutral", "think", "neutral", "happy", "flat"],
    "number":  ["neutral", "think", "surprise", "neutral", "happy", "flat"],
    "reveal":  ["neutral", "think", "think", "surprise", "happy", "flat"],
}

# 本文にこの語があれば、型より本文を優先する
FACE_KEYWORDS = {
    "surprise": ["実は", "じつは", "まさか", "え、", "えっ", "!?", "！？", "そんな", "びっくり",
                 "衝撃", "なんと", "を超える", "届く"],
    "happy":    ["できた", "よかった", "うれし", "嬉し", "楽に", "解決", "軽く", "助か", "気持ちい"],
    "think":    ["なぜ", "どうして", "らしい", "かもしれ", "たぶん", "つまり", "理由", "考え", "調べ"],
    "confused": ["わからな", "分からな", "難し", "詰ま", "困", "むずかし", "できない", "折れなく",
                 "失敗", "間違"],
    "flat":     ["けどね", "だけ", "それだけ", "ただし", "現実には", "とはいえ", "まあ"],
}


def infer_face(text, fallback):
    if not text:
        return fallback
    best, best_n = fallback, 0
    for face, words in FACE_KEYWORDS.items():
        n = sum(text.count(w) for w in words)
        if n > best_n:
            best, best_n = face, n
    return best


def _variant(table, key, i, s):
    opts = table.get(key, [key])
    return opts[(s // (i + 3)) % len(opts)]


# ---------------------------------------------------------------- キャラの変化
#
# 同じ絵柄（線の太さ・省略の度合い）は保ったまま、頭の形・耳・小物・配色だけを
# seed（＝その日の日付）から引き直す。毎回ちがう子が出てくるが、
# アカウントとしての絵の作法は変わらない。

CHAR_HEADS = {
    "round":  "M150 32 C110 32 84 62 84 102 C84 142 112 168 150 168 C188 168 216 142 216 102 C216 62 190 32 150 32 Z",
    "egg":    "M150 28 C114 28 88 66 88 108 C88 145 114 168 150 168 C186 168 212 145 212 108 C212 66 186 28 150 28 Z",
    "wide":   "M150 38 C102 38 78 66 78 103 C78 143 108 166 150 166 C192 166 222 143 222 103 C222 66 198 38 150 38 Z",
    "square": "M150 32 C112 32 86 48 86 86 L86 116 C86 152 112 168 150 168 C188 168 214 152 214 116 L214 86 C214 48 188 32 150 32 Z",
}
CHAR_EARS = ["none", "cat", "round", "tuft", "antenna"]
CHAR_ACCS = ["none", "scarf", "glasses", "cap", "bowtie"]

# (頭の色, 体の色, ほおの色)
CHAR_PALETTES = [
    ("#FBF7F0", "#E9E3D8", "#F0B9A6"),
    ("#FFF4E6", "#F2DCBE", "#E9A48C"),
    ("#F3F6FA", "#DBE3EC", "#EFA9B8"),
    ("#FDF3F3", "#EFD9D9", "#E8908F"),
    ("#F2F7F2", "#DAE6DA", "#EDA98E"),
    ("#F7F2FA", "#E3D9EC", "#E7A2BE"),
]


def _draw(seq, seed, salt):
    """seed と用途名から、系列の中身をひとつ引く。用途ごとに独立して散る。"""
    h = int(hashlib.sha256(("%d/%s" % (seed, salt)).encode()).hexdigest()[:8], 16)
    return seq[h % len(seq)]


def build_char(seed):
    head = _draw(list(CHAR_HEADS.keys()), seed, "head")
    ear = _draw(CHAR_EARS, seed, "ear")
    acc = _draw(CHAR_ACCS, seed, "acc")
    pal = _draw(CHAR_PALETTES, seed, "pal")
    # 帽子と耳は同時に出すと潰し合うので、帽子が勝つ
    if acc == "cap":
        ear = "none"
    return dict(head=head, headD=CHAR_HEADS[head], ear=ear, acc=acc,
                headFill=pal[0], bodyFill=pal[1], cheek=pal[2])


def build_look(brand, mood, arc, seed, n_beats, beats=None):
    """ブランド・ムード・回タイプ・seed から、絵と展開の設計を返す。"""
    B = BRANDS[brand]
    M = MOOD_LOOK.get(mood, MOOD_LOOK["calm"])
    A = ARCS.get(arc, ARCS["story"])
    s = int(seed)

    # --- 色。主アクセントはブランドの芯から動かさない（±6度だけ）
    jitter = ((s % 13) - 6) * 1.0
    ah = B["anchor_hue"] + jitter
    accent = hsl(ah, B["anchor_sat"], B["anchor_lit"])
    sec_j = ((s // 7 % 11) - 5) * 1.6
    sec_base = B["sec_base"] if M["sec_from"] == "sec" else B["warm_hue"]
    sh = sec_base + M["sec_off"] + sec_j
    # アクセントと近すぎて「同じ色が2つ」に見えるのを防ぐ
    d = ((sh - ah + 180) % 360) - 180
    if abs(d) < 22:
        sh = ah + (22 if d >= 0 else -22)
    sec = hsl(sh, M["sec_sat"], M["sec_lit"] + B["lit_boost"])

    # --- 背景。ブランド既定色に副アクセントをごく薄く混ぜる
    tint_l = 0.26 if B["dark"] else 0.72
    tint_col = hsl(sh, 0.45, tint_l)
    bg = mix(B["base"], tint_col, M["tint"] * (1.0 + ((s // 3 % 5) - 2) * 0.06))

    # --- 背景に漂う塊の色（3つ）
    if B["dark"]:
        blobs = [hsl(ah, 0.9, 0.15), hsl(sh, 0.72, 0.14), hsl(sh + 40, 0.55, 0.11)]
    else:
        blobs = [hsl(ah, 0.55, 0.87), hsl(sh, 0.40, 0.88), hsl(sh + 34, 0.45, 0.90)]
    blobs = _rot(blobs, s // 5)

    rule = mix(bg, B["ink"], 0.16 if not B["dark"] else 0.22)
    white, black = "#FFFFFF", "#000000"
    if B["dark"]:
        track = mix(bg, white, 0.13)
        cardbg = mix(bg, white, 0.07)
        barbg = mix(bg, white, 0.06)
        barfg = B["ink"]
        kwbar = accent
    else:
        track = mix(bg, B["ink"], 0.11)
        cardbg = mix(bg, white, 0.62)
        barbg = mix(B["ink"], bg, 0.05)
        barfg = mix(white, bg, 0.35)
        kwbar = mix(accent, white, 0.45)

    # --- カット割り。並び順＝話の起伏なので動かさない。
    #     各ビートの「やり方」だけを seed で言い換える。
    def at(seq, i, fallback):
        return seq[i % len(seq)] if seq else fallback

    faces = FACE_ARCS.get(arc, FACE_ARCS["story"])
    beats = beats or []

    shots = []
    for i in range(n_beats):
        b = beats[i] if i < len(beats) else {}
        text = (b.get("telop", "") or "") + (b.get("key", "") or "") + (b.get("kicker", "") or "")
        face = b.get("face") or infer_face(text, at(faces, i, "neutral"))
        shots.append(dict(
            cam=_variant(CAM_VARIANTS, at(A["cam"], i, "hold"), i, s),
            trans=("fade" if i == 0
                   else _variant(TRANS_VARIANTS, at(A["trans"], i, "fade"), i, s)),
            telop=at(A["telop"], i, "bottom"),
            style=_variant(STYLE_VARIANTS, at(A["style"], i, "bar"), i, s),
            face=face,
        ))

    return dict(
        brand=brand, mood=mood, arc=arc, seed=s,
        w=B["w"], h=B["h"],
        vars={
            "--bg": bg, "--ink": B["ink"], "--sub": B["sub"],
            "--accent": accent, "--accent2": sec, "--rule": rule,
            "--blob1": blobs[0], "--blob2": blobs[1], "--blob3": blobs[2],
            "--track": track, "--cardbg": cardbg,
            "--barbg": barbg, "--barfg": barfg, "--kwbar": kwbar,
            "--accent-glow": accent + "8C", "--accent2-glow": sec + "8C",
        },
        grain=M["grain"], blur=M["blur"], motion=M["motion"],
        shots=shots,
        char=build_char(s),
        suggested_props=A["props"],
        label="%s / %s" % (M["ja"], A["ja"]),
    )


# ---------------------------------------------------------------- bgm_engine
# -*- coding: utf-8 -*-
"""
bgm_engine.py — 台本のムードから毎回ちがう BGM を合成する（既存音源不使用）

旧実装の問題:
  - 88Hz のサイン波を 1.5Hz で叩いていた → 低音の唸り（「ブンブン」）
  - コード・テンポ・音色がすべて固定 → 何本作っても同じ曲

新実装:
  - ムード（calm / bright / tense / playful / warm / focus）ごとに
    調・コード進行・テンポ・音色・密度・残響が変わる
  - seed で同じムードの中でもキー／進行の変種／アルペジオ型が変わる
  - beats の時刻に合わせて構成（薄い→厚い→抜き→締め）とアクセント音を置く
  - 低音は「1小節に1音・ゆっくり立ち上がる」だけ。パルスは鳴らさない

使い方:
    from bgm_engine import render_bgm, write_wav
    st = render_bgm(duration=25.0, mood="calm", seed=20260822, beats_t=[0,4.2,8.4,...])
    write_wav("bgm.wav", st)
"""

import math
import wave

import numpy as np

SR = 44100


# ---------------------------------------------------------------- 音程ユーティリティ

def hz(midi):
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


# コードタイプ = ルートからの半音
CHORDS = {
    "maj7":  [0, 4, 7, 11],
    "maj9":  [0, 4, 7, 11, 14],
    "add9":  [0, 4, 7, 14],
    "6/9":   [0, 4, 9, 14],
    "min7":  [0, 3, 7, 10],
    "min9":  [0, 3, 7, 10, 14],
    "min11": [0, 3, 7, 10, 17],
    "sus2":  [0, 2, 7, 14],
    "sus4":  [0, 5, 7, 10],
    "dim":   [0, 3, 6, 9],
    "maj":   [0, 4, 7],
    "min":   [0, 3, 7],
}


# ---------------------------------------------------------------- ムード定義
#
# roots        : 使ってよい主音（midi。3〜4オクターブ帯で voicing する）
# progressions : (度数, コード) の並び。回ごとに1つ選ぶ
# bpm          : 基準テンポ（seed で ±4 揺れる）
# bars         : 1コードあたりの拍数
# bright       : 倍音の明るさ 0..1
# voices       : 使う声部
# reverb       : 残響量 0..1
# level        : 最終音量（0.10 前後が「文字が主役」の適正）

MOODS = {
    # 静か・気づき・内省。テロップを読ませたい回
    "calm": dict(
        roots=[57, 55, 60, 53],                       # A3, G3, C4, F3
        progressions=[
            [(0, "maj9"), (-3, "min9"), (-7, "maj7"), (-5, "sus2")],
            [(0, "add9"), (5, "maj7"), (-3, "min7"), (2, "sus2")],
            [(0, "maj7"), (-2, "min9"), (-4, "add9"), (-5, "maj9")],
        ],
        bpm=72, beats_per_chord=8, bright=0.42,
        voices=dict(pad=1.0, mallet=0.30, bell=0.22, shaker=0.0, bass=0.55, air=0.35),
        arp="sparse", reverb=0.72, level=0.105,
    ),

    # 前向き・結果が出る回・ノウハウの締め
    "bright": dict(
        roots=[60, 62, 65, 58],
        progressions=[
            [(0, "maj9"), (7, "sus2"), (-3, "min7"), (5, "maj7")],
            [(0, "6/9"), (-4, "min7"), (-5, "maj9"), (2, "sus4")],
            [(0, "maj7"), (2, "min7"), (4, "min9"), (5, "maj9")],
        ],
        bpm=94, beats_per_chord=4, bright=0.70,
        voices=dict(pad=0.62, mallet=0.85, bell=0.30, shaker=0.42, bass=0.70, air=0.20),
        arp="up8", reverb=0.42, level=0.115,
    ),

    # 問題提起・失敗談・「これ、ずっと間違ってた」系
    "tense": dict(
        roots=[57, 55, 52, 59],
        progressions=[
            [(0, "min9"), (0, "min9"), (-2, "sus4"), (-4, "maj7")],
            [(0, "min11"), (3, "maj7"), (0, "min9"), (-1, "dim")],
            [(0, "min7"), (5, "min9"), (-2, "sus2"), (0, "min7")],
        ],
        bpm=78, beats_per_chord=8, bright=0.30,
        voices=dict(pad=1.0, mallet=0.34, bell=0.14, shaker=0.0, bass=0.72, air=0.55),
        arp="drone", reverb=0.80, level=0.100,
    ),

    # 軽い・意外性・オチのある回
    "playful": dict(
        roots=[62, 60, 64, 67],
        progressions=[
            [(0, "maj"), (-3, "min7"), (-5, "6/9"), (2, "sus4")],
            [(0, "6/9"), (4, "min7"), (5, "maj"), (2, "min9")],
            [(0, "add9"), (-2, "min7"), (-4, "maj7"), (-5, "6/9")],
        ],
        bpm=108, beats_per_chord=4, bright=0.78,
        voices=dict(pad=0.40, mallet=1.0, bell=0.34, shaker=0.55, bass=0.62, air=0.12),
        arp="bounce", reverb=0.34, level=0.115,
    ),

    # 振り返り・しみじみ・締めの回
    "warm": dict(
        roots=[53, 55, 58, 60],
        progressions=[
            [(0, "maj9"), (2, "min9"), (4, "min7"), (5, "maj7")],
            [(0, "maj7"), (-5, "add9"), (-3, "min9"), (-1, "sus4")],
            [(0, "add9"), (7, "sus2"), (0, "maj9"), (-4, "min7")],
        ],
        bpm=66, beats_per_chord=8, bright=0.50,
        voices=dict(pad=0.85, mallet=0.55, bell=0.40, shaker=0.0, bass=0.60, air=0.28),
        arp="sparse", reverb=0.66, level=0.108,
    ),

    # 手順・淡々と積み上げる回・数字の回
    "focus": dict(
        roots=[57, 60, 55, 62],
        progressions=[
            [(0, "sus2"), (0, "sus2"), (-2, "add9"), (-2, "add9")],
            [(0, "min7"), (-2, "sus2"), (0, "min7"), (3, "maj7")],
            [(0, "add9"), (5, "sus4"), (0, "add9"), (-3, "min7")],
        ],
        bpm=88, beats_per_chord=4, bright=0.55,
        voices=dict(pad=0.55, mallet=0.75, bell=0.18, shaker=0.30, bass=0.66, air=0.18),
        arp="pulse", reverb=0.40, level=0.110,
    ),
}

MOOD_LIST = list(MOODS.keys())


# ---------------------------------------------------------------- 声部（すべてベクトル化）

def _env(n, attack, decay, sustain, release, sr=SR):
    """ADSR。n サンプルぶん。"""
    a = max(1, int(attack * sr))
    d = max(1, int(decay * sr))
    r = max(1, int(release * sr))
    s = max(0, n - a - d - r)
    e = np.concatenate([
        np.linspace(0, 1, a),
        np.linspace(1, sustain, d),
        np.full(s, sustain),
        np.linspace(sustain, 0, r),
    ])
    return e[:n] if len(e) >= n else np.pad(e, (0, n - len(e)))


def voice_pad(freqs, dur, bright, sr=SR, detune=5.5, n_harm=8):
    """加算合成のパッド。倍音を LFO でゆっくり開閉して「動くローパス」にする。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)
    lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.055 * t + 0.9)   # 18秒周期
    openness = bright * (0.55 + 0.45 * lfo) * n_harm
    for k, f in enumerate(freqs):
        for h in range(1, n_harm + 1):
            fh = f * h
            if fh > 9000:
                break
            amp = 1.0 / (h ** 1.75)
            gate = np.clip(openness - h + 1.0, 0.0, 1.0)
            ph = (k * 1.7 + h * 0.37) % (2 * np.pi)
            out += amp * gate * np.sin(2 * np.pi * fh * t + ph)
            # わずかにデチューンした重ね（厚みが出る／唸りにならない程度）
            fd = fh * (1 + detune / 1200.0 / 100 * (1 if h % 2 else -1))
            out += 0.45 * amp * gate * np.sin(2 * np.pi * fd * t + ph + 1.1)
    out /= max(1e-9, len(freqs) * 1.9)
    return out * _env(n, 0.55, 0.35, 0.85, min(1.2, dur * 0.35))


def voice_mallet(freq, dur, bright, sr=SR):
    """木琴〜エレピ寄りの粒。倍音ごとに減衰が違うので「コーン」と落ちる。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)
    for h, amp in ((1, 1.0), (2, 0.34), (3, 0.16), (4.02, 0.09), (6.1, 0.05)):
        if freq * h > 12000:
            break
        dec = 3.0 + h * (3.4 - bright * 1.4)
        out += amp * np.exp(-t * dec) * np.sin(2 * np.pi * freq * h * t)
    out *= 1 - np.exp(-t * 900)          # アタックの角を取る
    return out * 0.5


def voice_bell(freq, dur, sr=SR):
    """テロップが切り替わる瞬間に置く小さな鈴。倍音は非整数。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    out = (np.exp(-t * 2.6) * np.sin(2 * np.pi * freq * t)
           + 0.42 * np.exp(-t * 4.1) * np.sin(2 * np.pi * freq * 2.76 * t)
           + 0.18 * np.exp(-t * 6.0) * np.sin(2 * np.pi * freq * 5.4 * t))
    out *= 1 - np.exp(-t * 1200)
    return out * 0.34


def voice_shaker(dur, rng, sr=SR):
    """高域だけのシャカ。低音を一切持たないのでテンポ感だけが乗る。"""
    n = max(2, int(dur * sr))
    t = np.arange(n) / sr
    nz = rng.standard_normal(n)
    nz = np.diff(np.concatenate([[0.0], nz]))      # 一次微分＝ハイパス
    return nz * np.exp(-t * 90) * 0.16


def voice_bass(freq, dur, sr=SR):
    """1小節に1音だけ。立ち上がりを遅くして『唸り』ではなく『支え』にする。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    f = max(48.0, freq)                            # 48Hz より下は出さない
    out = np.sin(2 * np.pi * f * t) + 0.22 * np.sin(2 * np.pi * f * 2 * t)
    return out * _env(n, 0.14, 0.5, 0.62, dur * 0.4) * 0.5


def voice_air(dur, rng, cutoff_ratio, sr=SR):
    """空気感。ノイズを帯域制限して極小音量で敷く。"""
    n = int(dur * sr)
    nz = rng.standard_normal(n)
    spec = np.fft.rfft(nz)
    fr = np.fft.rfftfreq(n, 1 / sr)
    lo, hi = 700.0, 700.0 + 5200.0 * cutoff_ratio
    curve = np.exp(-((np.log2(np.maximum(fr, 20) / hi)) ** 2) * 1.4)
    curve *= 1 / (1 + (lo / np.maximum(fr, 20)) ** 4)
    out = np.fft.irfft(spec * curve, n=n)
    out /= max(1e-9, np.max(np.abs(out)))
    t = np.arange(n) / sr
    out *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.037 * t)
    return out * 0.09


# ---------------------------------------------------------------- 空間系

def _comb(x, delay, fb):
    """y[n] = x[n] + fb*y[n-delay] をブロック単位でベクトル化。"""
    y = x.copy()
    d = int(delay)
    for i in range(d, len(y), d):
        j = min(i + d, len(y))
        y[i:j] += fb * y[i - d:i - d + (j - i)]
    return y


def _allpass(x, delay, g=0.62):
    d = int(delay)
    y = np.zeros_like(x)
    y[:d] = -g * x[:d]
    for i in range(d, len(x), d):
        j = min(i + d, len(x))
        seg = x[i - d:i - d + (j - i)]
        y[i:j] = -g * x[i:j] + seg + g * y[i - d:i - d + (j - i)]
    return y


def reverb(x, amount, sr=SR):
    if amount <= 0.001:
        return x
    wet = np.zeros_like(x)
    for d, fb in ((1557, 0.80), (1617, 0.79), (1491, 0.81), (1422, 0.78),
                  (1277, 0.76), (1116, 0.75)):
        wet += _comb(x, d * (sr // 44100 or 1), fb * (0.72 + 0.28 * amount))
    wet /= 6.0
    wet = _allpass(wet, 225)
    wet = _allpass(wet, 556)
    # 残響は高域を落として濁らせない
    spec = np.fft.rfft(wet)
    fr = np.fft.rfftfreq(len(wet), 1 / sr)
    spec *= 1 / (1 + (fr / 3200.0) ** 2)
    wet = np.fft.irfft(spec, n=len(wet))
    m = np.max(np.abs(wet))
    if m > 1e-9:
        wet /= m
    return x * (1 - 0.45 * amount) + wet * amount * 0.55


def highpass(x, f0, sr=SR):
    spec = np.fft.rfft(x)
    fr = np.fft.rfftfreq(len(x), 1 / sr)
    spec *= (fr / f0) ** 2 / (1 + (fr / f0) ** 2)
    return np.fft.irfft(spec, n=len(x))


# ---------------------------------------------------------------- 編曲

def _add(buf, seg, at, sr=SR):
    i = int(at * sr)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(seg))
    buf[i:j] += seg[:j - i]


def _voicing(root_midi, chord, rng, spread):
    """コードを 3〜5 声に散らす。回ごとに転回が変わる。"""
    ivs = list(CHORDS[chord])
    rng.shuffle(ivs)
    ivs = sorted(ivs[: 3 + int(spread * 2)])
    base = root_midi + 12
    notes = []
    for k, iv in enumerate(ivs):
        oct_shift = 12 * (1 if (k >= 2 and rng.random() < 0.45) else 0)
        notes.append(base + iv + oct_shift)
    return sorted(set(notes))


def _arp_pattern(kind, rng):
    if kind == "up8":
        return [0, 1, 2, 3, 2, 1], 0.5
    if kind == "bounce":
        return rng.permutation([0, 2, 1, 3, 0, 2]).tolist(), 0.5
    if kind == "pulse":
        return [0, 2], 1.0
    if kind == "sparse":
        return [0, 2, 1], 2.0
    return [], 0.0          # drone


def render_bgm(duration, mood="calm", seed=0, beats_t=None, sr=SR, level_scale=1.0):
    """ムードと seed から 1本ぶんの BGM を作って (n,2) の float 配列を返す。"""
    if mood not in MOODS:
        mood = "calm"
    M = MOODS[mood]
    rng = np.random.default_rng(int(seed) % (2 ** 32))

    root = int(rng.choice(M["roots"]))
    prog = list(M["progressions"][int(rng.integers(0, len(M["progressions"])))])
    if rng.random() < 0.5:                       # 進行の開始位置も回転させる
        k = int(rng.integers(1, len(prog)))
        prog = prog[k:] + prog[:k]
    bpm = M["bpm"] * float(rng.uniform(0.955, 1.045))
    spb = 60.0 / bpm
    chord_dur = spb * M["beats_per_chord"]
    v = M["voices"]

    n = int(duration * sr)
    buf = np.zeros(n + int(2.5 * sr))

    beats_t = sorted(beats_t or [])
    # 構成: 時間で密度を動かす（薄い→厚い→少し抜く→締める）
    def density(tt):
        p = tt / max(0.001, duration)
        if p < 0.16:
            return 0.42 + p / 0.16 * 0.28
        if p < 0.62:
            return 0.70 + (p - 0.16) / 0.46 * 0.30
        if p < 0.80:
            return 1.0 - (p - 0.62) / 0.18 * 0.32
        return 0.68 + (p - 0.80) / 0.20 * 0.24

    arp_seq, arp_step_beats = _arp_pattern(M["arp"], rng)

    # --- コード進行を敷く
    ci, tpos = 0, 0.0
    while tpos < duration:
        deg, ctype = prog[ci % len(prog)]
        r = root + deg
        d = density(tpos)
        notes = _voicing(r, ctype, rng, spread=d)
        freqs = [hz(m) for m in notes]

        if v["pad"] > 0:
            seg = voice_pad(freqs, min(chord_dur * 1.25, duration - tpos + 1.1),
                            M["bright"] * (0.72 + 0.28 * d), sr)
            _add(buf, seg * v["pad"] * (0.55 + 0.45 * d), tpos, sr)

        if v["bass"] > 0:
            bf = hz(r - 12 if r - 12 >= 33 else r)
            seg = voice_bass(bf, chord_dur * 0.94, sr)
            _add(buf, seg * v["bass"] * (0.6 + 0.4 * d), tpos + 0.02, sr)

        # --- アルペジオ / 粒
        if arp_seq and v["mallet"] > 0:
            step = spb * arp_step_beats
            k = 0
            at = tpos
            while at < min(tpos + chord_dur, duration):
                idx = arp_seq[k % len(arp_seq)] % len(freqs)
                if rng.random() < 0.30 + 0.62 * d:
                    f = freqs[idx] * (2.0 if (mood == "playful" and rng.random() < 0.25) else 1.0)
                    seg = voice_mallet(f, min(1.5, step * 3.2), M["bright"], sr)
                    _add(buf, seg * v["mallet"] * (0.45 + 0.55 * d) *
                         (1.0 if k % len(arp_seq) == 0 else 0.72), at, sr)
                at += step
                k += 1

        # --- シャカ（低音を持たないのでテンポだけ乗る）
        if v["shaker"] > 0:
            at = tpos + spb * 0.5
            while at < min(tpos + chord_dur, duration):
                seg = voice_shaker(0.09, rng, sr)
                _add(buf, seg * v["shaker"] * (0.4 + 0.6 * d), at, sr)
                at += spb * (0.5 if mood == "playful" else 1.0)

        tpos += chord_dur
        ci += 1

    # --- テロップの切り替わりに小さなアクセント（映像と音が同じ拍で動く）
    if v["bell"] > 0:
        for bt in beats_t:
            if bt <= 0.01 or bt >= duration - 0.2:
                continue
            deg, ctype = prog[int(bt // chord_dur) % len(prog)]
            iv = CHORDS[ctype][int(rng.integers(0, len(CHORDS[ctype])))]
            f = hz(root + deg + iv + 24)
            seg = voice_bell(f, min(2.2, duration - bt), sr)
            _add(buf, seg * v["bell"] * 0.9, bt - 0.04, sr)

    if v["air"] > 0:
        buf[:n] += voice_air(duration, rng, M["bright"]) * v["air"]

    buf = buf[:n]
    buf = highpass(buf, 42.0, sr)          # 40Hz 以下の唸りを物理的に落とす
    buf = reverb(buf, M["reverb"], sr)

    # --- 音量を揃える（RMS 基準。回ごとに音量が違うと不快なので固定）
    rms = float(np.sqrt(np.mean(buf ** 2))) or 1e-9
    buf *= (M["level"] * level_scale) / rms
    buf = np.tanh(buf * 1.6) / 1.6         # 軽いソフトクリップ

    # --- 前後のフェード
    fi, fo = int(sr * 0.9), int(sr * 1.4)
    buf[:fi] *= np.linspace(0, 1, fi) ** 1.4
    buf[-fo:] *= np.linspace(1, 0, fo) ** 1.4

    # --- ステレオ（左右をわずかにずらして広げる）
    dly = int(sr * 0.011)
    half = dly // 2
    right = np.concatenate([np.zeros(dly), buf[:-dly]]) * 0.55 + buf * 0.45
    left = buf * 0.92 + np.concatenate([np.zeros(half), buf[:-half]]) * 0.08
    st = np.stack([left, right], axis=1)
    m = np.max(np.abs(st))
    if m > 0.98:
        st *= 0.98 / m
    return st


def write_wav(path, st, sr=SR):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(st, -1, 1) * 32767).astype("<i2").tobytes())


# ---------------------------------------------------------------- 台本 → ムード自動判定

TONE_KEYWORDS = {
    "tense":   ["失敗", "間違", "しんど", "詰ま", "沼", "やめた", "無駄", "痛い", "誤解", "不安", "遅い", "疲れ"],
    "bright":  ["できた", "変わった", "解決", "楽に", "早い", "伸び", "結果", "成功", "軽く", "増え"],
    "playful": ["え、", "まさか", "拍子抜け", "そんな", "意外", "笑", "たった", "だけ", "ズル"],
    "warm":    ["ふりかえ", "思えば", "あの頃", "今なら", "いま思う", "続け", "ありがた"],
    "focus":   ["手順", "やり方", "ステップ", "まず", "つぎに", "設定", "作り方", "3つ", "比べ"],
}


def infer_mood(script):
    """台本の text から雰囲気を推定する。script["mood"] があればそれを優先。"""
    if isinstance(script, dict) and script.get("mood") in MOODS:
        return script["mood"]
    text = ""
    for b in (script.get("beats", []) if isinstance(script, dict) else []):
        text += (b.get("telop", "") or "") + (b.get("kicker", "") or "") + (b.get("key", "") or "")
    score = {k: 0 for k in MOODS}
    for mood, words in TONE_KEYWORDS.items():
        for w in words:
            score[mood] += text.count(w)
    best = max(score, key=lambda k: score[k])
    return best if score[best] > 0 else "calm"


# ---------------------------------------------------------------- renderer
# -*- coding: utf-8 -*-
"""@gude_aiai アニメーション動画レンダラ（v2）

scene.html を Chromium で1フレームずつ描画し、ffmpeg で mp4 にする。
v2 で追加:
  - 台本のトーンから BGM のムードを決め、bgm_engine で毎回ちがう曲を合成する
  - 同じムードでも回ごとにキー・進行・テンポが変わる（seed）
  - look_engine が回のタイプからカット割り・配色・転換を決める

台本に書ける（すべて省略可。省略すると本文から推定する）:
    "mood" : calm / bright / tense / playful / warm / focus
    "arc"  : story / compare / steps / number / reveal
    "seed" : 整数。省略すると出力ファイル名から作る

使い方:
    python3 render_anim.py script.json out.mp4 [--no-bgm] [--seed N]
"""
import json, os, subprocess, sys, shutil, hashlib
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))


BRAND = "gude"
W, H = BRANDS[BRAND]["w"], BRANDS[BRAND]["h"]
FPS = 30
SCENE = "file://" + os.path.join(HERE, "scene.html")
CHROME = "/opt/pw-browsers/chromium"


def render_frames(beats, dur, look, frame_dir):
    os.makedirs(frame_dir, exist_ok=True)
    n = int(dur * FPS)
    with sync_playwright() as p:
        launch = {"args": ["--force-color-profile=srgb", "--font-render-hinting=none",
                           "--disable-lcd-text", "--hide-scrollbars"]}
        if os.path.exists(CHROME):
            launch["executable_path"] = CHROME
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        page.add_init_script(
            "window.__BEATS__=%s;window.__DUR__=%f;window.__LOOK__=%s;"
            % (json.dumps(beats, ensure_ascii=False), dur, json.dumps(look, ensure_ascii=False)))
        page.goto(SCENE)
        page.wait_for_function("typeof window.seek === 'function'")
        # エンジンが本当に動いているかを、関数の有無ではなく描画結果で確かめる
        page.evaluate("window.seek(0)")
        ok = page.evaluate("document.querySelectorAll('#telopline .ch').length > 0")
        if not ok:
            browser.close()
            raise RuntimeError("scene.html がテロップを描画していない（DOM の取り違えの可能性）")
        page.wait_for_timeout(400)
        for i in range(n):
            page.evaluate("window.seek(%f)" % (i / FPS))
            page.screenshot(path=os.path.join(frame_dir, "f%05d.png" % i))
            if i % 90 == 0:
                print("  frame %d/%d" % (i, n), flush=True)
        browser.close()
    return n


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

    mood = infer_mood(script)
    arc = infer_arc(script, mood)
    look = build_look(BRAND, mood, arc, seed, len(beats), beats=beats)
    look["handle"] = script.get("handle", "@gude_aiai")

    print("look: %s  seed=%d" % (look["label"], seed))
    print("  bg=%s accent=%s accent2=%s" % (
        look["vars"]["--bg"], look["vars"]["--accent"], look["vars"]["--accent2"]))
    print("  cam=%s" % "/".join(s["cam"] for s in look["shots"]))

    fd = os.path.join(HERE, "_frames")
    shutil.rmtree(fd, ignore_errors=True)
    print("rendering frames...")
    n = render_frames(beats, dur, look, fd)

    voice = None

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-framerate", str(FPS), "-i", os.path.join(fd, "f%05d.png")]
    if "--no-bgm" not in flags:
        wav = os.path.join(HERE, "_bgm.wav")
        track = render_bgm(dur, mood, seed, beats_t=[b["t"] for b in beats])
        write_wav(wav, track)
        cmd += ["-i", wav]
    cmd += ["-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
            "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p"]
    if "--no-bgm" not in flags:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += ["-movflags", "+faststart", out]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-3000:]); sys.exit(1)
    shutil.rmtree(fd, ignore_errors=True)
    mb = os.path.getsize(out) / 1024 / 1024
    print("OK %s  %.2f MB  %d frames  %.1fs  [%s]" % (out, mb, n, dur, look["label"]))
    if mb > 9.5:
        print("WARNING: 10MB上限に近い。crf を上げるか尺を詰めること。")


if __name__ == "__main__":
    main()
