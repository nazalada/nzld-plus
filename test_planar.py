#!/usr/bin/env python3
"""Планарный трек: углы corner-pin считаются в коробке САМОГО СЛОЯ, а не канваса.

Зачем именно эти проверки. Оба бага были невидимы на глаз:
  1. Углы писались в канвасных NDC. При масштабе слоя ≈1 это совпадает с правильным ответом
     (поэтому перспектива камеры на полноэкранных слоях выглядела верной), а follower на 0.37
     уезжал в несколько раз. Замерено на экране: слой 0.366 со статичным квадом [-0.9..-0.1]
     лёг в x 719..786 px — ровно предсказание «коробка слоя» (718..786), не «канвас» (565..754).
  2. Через гомографию гнались углы РАМКИ ROI, из-за чего слой насильно натягивало на обведённый
     прямоугольник. Теперь при H = единица углы обязаны выйти единичными: слой стоит где стоял.

Запуск: .venv/bin/python test_planar.py
"""
import sys

import numpy as np

import capcut_track as ct

CW, CH = 1920, 1080
FW, FH = 640, 360          # материал follower'а; fit в канвас = x3, при scale 0.5 -> полуширина 480 px канваса


def draft():
    """Видео во весь канвас (fit=1, scale=1) + follower 640x360 со scale 0.5 в центре."""
    return {
        "canvas_config": {"width": CW, "height": CH},
        "materials": {"videos": [
            {"id": "MV", "type": "video", "width": CW, "height": CH},
            {"id": "MF", "type": "video", "width": FW, "height": FH},
        ]},
        "tracks": [{"id": "T", "type": "video", "segments": [seg("SV", "MV", 1.0), seg("SF", "MF", 0.5)]}],
    }


def seg(sid, mid, scale):
    return {"id": sid, "material_id": mid,
            "target_timerange": {"start": 0, "duration": 5_000_000},
            "source_timerange": {"start": 0, "duration": 5_000_000},
            "clip": {"scale": {"x": scale, "y": scale}, "transform": {"x": 0.0, "y": 0.0},
                     "rotation": 0.0, "alpha": 1.0},
            "common_keyframes": []}


def corners(homos, times=(0, 2_500_000), quad_src=None):
    d = draft()
    by = {s["id"]: s for s in d["tracks"][0]["segments"]}
    out = ct.compute_cornerpin_ml(d, by["SV"], by["SF"], [np.array(h) for h in homos],
                                  list(times), quad_src)
    # {prop: [(t, [x, y])]} -> список кадров, в каждом 4 угла в порядке CORNER_PROPS
    return [[out[p][i][1] for p in ct.CORNER_PROPS] for i in range(len(times))]


def main():
    bad = 0
    eye = np.eye(3)

    # 1. H = единица -> ровно единичный квад: слой остаётся там, где его положил пользователь
    q = corners([eye, eye])[0]
    want = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
    if max(abs(a - c) + abs(b - dd) for (a, b), (c, dd) in zip(q, want)) > 1e-9:
        bad += 1
        print("ПРОВАЛ: при единичной гомографии углы не единичные ->", q)
    else:
        print("единичная гомография -> единичный квад (слой не сдвинулся)")

    # 2. Сдвиг плоскости на 96 px исходника. Видео 1:1 в канвас, значит и в канвасе 96 px.
    #    Полуширина коробки follower'а = 3.0 * 0.5 * 640 / 2 = 480 px -> ждём ровно 0.2 NDC.
    q = corners([eye, np.array([[1.0, 0, 96.0], [0, 1.0, 0], [0, 0, 1.0]])])[1]
    dx = [x for x, _ in q]
    dy = [y for _, y in q]
    if max(abs(v - w) for v, w in zip(dx, [-0.8, 1.2, -0.8, 1.2])) > 1e-9 or max(abs(v) for v in dy) < 0.99:
        bad += 1
        print("ПРОВАЛ: сдвиг плоскости пересчитан неверно ->", q)
    else:
        print("сдвиг 96 px исходника -> ровно 0.2 NDC коробки слоя")

    # 3. Настоящая перспектива: верх сжат вдвое -> квад обязан стать трапецией, а не параллелограммом
    import cv2
    src = np.float32([[0, 0], [CW, 0], [0, CH], [CW, CH]])
    dst = np.float32([[CW * 0.25, 0], [CW * 0.75, 0], [0, CH], [CW, CH]])
    q = corners([eye, cv2.getPerspectiveTransform(src, dst)])[1]
    top, bot = q[1][0] - q[0][0], q[3][0] - q[2][0]
    if not (top < bot * 0.75):
        bad += 1
        print("ПРОВАЛ: перспектива не проявилась, верх %.3f против низа %.3f" % (top, bot))
    else:
        print("перспектива: верх %.3f уже низа %.3f — трапеция, а не параллелограмм" % (top, bot))

    # 4. Канал corner-pin обязан включаться, иначе ключи молча мертвы
    d = draft()
    sf = d["tracks"][0]["segments"][1]
    ct.enable_corner_pin(d, sf, "PIN-1")
    mat = ct.material_index(d)["MF"][1]
    if not (mat.get("corner_pin") or {}).get("enable_corner_pin"):
        bad += 1
        print("ПРОВАЛ: канал corner-pin не включился ->", mat.get("corner_pin"))
    else:
        print("канал corner-pin включён, квад единичный:", mat["corner_pin"]["corner_pin_info"]["lower_right_x"] == 1.0)

    # 5. Отчёт о качестве: не-плоскость обязана быть помечена подозрительной
    rep = ct._plane_report({"npoints": 49, "quality": [[40, 0.4]] * 10}, log=lambda *_: None)
    rep_bad = ct._plane_report({"npoints": 49, "quality": [[9, 5.0]] * 8 + [[0, -1.0]] * 2},
                               log=lambda *_: None)
    if rep.get("suspect") or not rep_bad.get("suspect") or rep_bad["lost"] != 2:
        bad += 1
        print("ПРОВАЛ: разбор качества плоскости ->", rep, rep_bad)
    else:
        print("качество: чистый трек не помечен, кривой помечен suspect и 2 потерянных кадра")

    print("ВСЁ ПРОШЛО" if not bad else "ПРОВАЛОВ: %d" % bad)
    return 1 if bad else 0


sys.exit(main())
