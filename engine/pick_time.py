# -*- coding: utf-8 -*-
"""@gude_aiai 投稿時刻の算出。うだっちの時間帯(8/10/12/15/20/23時)を避け、
日付をシードに分単位で毎日ずらす。:00 と :30 は必ず外す。"""
import hashlib, random, sys
from datetime import datetime, timedelta, timezone
JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------- 投稿時刻
# うだっちが使っている時間帯（8/10/12/15/20/23時）を完全に避ける。
# 各帯の中で日ごとに分単位で揺らし、AIの定時実行に見えないようにする。
BANDS = {
    "朝": (6 * 60 + 35, 7 * 60 + 45),    # 06:35-07:45
    "昼": (13 * 60 + 5, 14 * 60 + 25),   # 13:05-14:25
    "夜": (21 * 60 + 5, 22 * 60 + 25),   # 21:05-22:25
}
UDACCHI_HOURS = {8, 10, 12, 15, 20, 23}


def pick_time(day: datetime, band: str) -> datetime:
    """その日・その帯の投稿時刻を決める。日付でシードするので再現性がある。"""
    lo, hi = BANDS[band]
    seed = int(hashlib.sha256(f"{day:%Y-%m-%d}/{band}".encode()).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    minute_of_day = rnd.randint(lo, hi)
    # 00分/30分ちょうどは機械的に見えるので避ける
    if minute_of_day % 30 == 0:
        minute_of_day += rnd.choice([-7, -4, 3, 6])
    assert minute_of_day // 60 not in UDACCHI_HOURS, "うだっちの時間帯と衝突"
    return day.replace(hour=minute_of_day // 60, minute=minute_of_day % 60,
                       second=0, microsecond=0)


def next_slot(band: str) -> datetime:
    """今から見て次に来るその帯の時刻。過ぎていれば翌日。"""
    now = datetime.now(JST)
    t = pick_time(now, band)
    if t <= now + timedelta(minutes=3):
        t = pick_time(now + timedelta(days=1), band)
    return t




if __name__ == "__main__":
    band = sys.argv[1] if len(sys.argv) > 1 else "夜"
    print(int(next_slot(band).timestamp()))
    print(next_slot(band).strftime("%Y-%m-%dT%H:%M:%S+09:00"))
