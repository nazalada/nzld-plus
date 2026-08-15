#!/usr/bin/env python3
"""Живой синк рендера после трека: арифметика времени выстрела.

Зачем именно этот тест: команда `addCommonKeyframe` берёт `play_head` во времени ТАЙМЛАЙНА и
сама пересчитывает его во время исходника формулой `src.start + (ph - tgt.start) * speed`.
Это ЗАМЕРЕНО на движке (2026-08-08, стенд TRACKING): послали 1503355 на сегмент с
tgt.start=500000 / src.start=300000 — ключ лёг ровно на 1303355; на клипе со скоростью 0.7866
(стенд ИСКАЖЕНИЕ) 100000 легло на 78657. Мы шлём время СВОЕГО ключа, переведённое обратно в
таймлайн, чтобы снимок сел на него и не наплодил лишних ключей.

Если обратный перевод разъедется с формулой движка, ничего не упадёт и не заругается: ключ
просто ляжет мимо, а на клипе с совпадающими шкалами (start=0, speed=1) баг вообще не виден —
ровно поэтому проверка отдельная и на НЕсовпадающих шкалах.

Запуск: .venv/bin/python test_live_sync.py
"""
import sys

import capcut_track as ct


def engine_kf_time(seg, play_head):
    """Что делает движок с play_head — по замеру, не по догадке."""
    src = seg.get("source_timerange") or {"start": 0}
    return src["start"] + (play_head - seg["target_timerange"]["start"]) * seg.get("speed", 1.0)


STAND = {"target_timerange": {"start": 500000, "duration": 2000000},
         "source_timerange": {"start": 300000, "duration": 2000000}, "speed": 1.0}
SPEEDED = {"target_timerange": {"start": 1200000, "duration": 5000000},
           "source_timerange": {"start": 400000, "duration": 3932854}, "speed": 0.7865707194244604}
FLAT = {"target_timerange": {"start": 0, "duration": 3000000},
        "source_timerange": {"start": 0, "duration": 3000000}, "speed": 1.0}

ok = True

# 1. Замер стенда воспроизводится формулой движка, и наш обратный перевод — её инверсия.
assert engine_kf_time(STAND, 1503355) == 1303355, "формула движка разошлась с замером"
assert ct.kf_time(STAND, 1503355) == 1303355, "kf_time разошлась с замером движка"

# 2. Круговой перевод: посылаем время СВОЕГО ключа — движок обязан вернуть его же.
for seg in (STAND, SPEEDED, FLAT):
    t0 = (seg["source_timerange"]["start"]
          + seg["source_timerange"]["duration"] // 2)          # ключ где-то в середине трека
    ph = int(round(ct.to_timeline(seg, t0)))
    landed = engine_kf_time(seg, ph)
    if abs(landed - t0) > 1:                                   # 1 мкс — цена округления play_head
        print(f"FAIL круговой перевод: ключ {t0} -> play_head {ph} -> лёг на {landed}")
        ok = False

# 3. Стреляем только тем, что реально записано: выстрел по пустому свойству создал бы
#    одинокий ключ на свойстве, которого трек не касался.
assert ct._live_prop(([(0, 0.1)], [(0, 0.2)], None, None))[0] == "KFTypePositionX"
assert ct._live_prop((None, None, [(0, 1.0)], None))[0] == "KFTypeScaleX"
assert ct._live_prop((None, None, None, [(0, 5.0)]))[0] == "KFTypeRotation"
assert ct._live_prop((None, None, None, None))[0] is None

# 4. Планарный трек приходит СЛОВАРЁМ треков углов — стрелять надо тем, что реально записано.
assert ct._live_prop({"KFTypeCornerPinUpLeft": [(0, [0.1, 0.2])]})[0] == "KFTypeCornerPinUpLeft"
assert ct._live_prop({"KFTypeCornerPinUpLeft": [],
                      "KFTypeCornerPinDownRight": [(0, [0.1, 0.2])]})[0] == "KFTypeCornerPinDownRight"
assert ct._live_prop({})[0] is None

print("OK" if ok else "ЕСТЬ ПАДЕНИЯ")
sys.exit(0 if ok else 1)
