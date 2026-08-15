#!/usr/bin/env python3
"""Регресс на пересчёт углов «по чернилам» (capcut_track.quad_for_ink).

Проверяет то, что ломается молча: у видео пересчёт обязан быть тождеством, а у надписи —
ставить на целевой квад ЧЕРНИЛА, а не коробку. Ошибка здесь не падает и не логируется, она
просто сдвигает слой — поэтому тест на числах, а не на «не упало».

Запуск: .venv/bin/python test_ink_quad.py
"""
import capcut_track as ct


def close(a, b, eps=1e-9):
    return all(abs(x - y) < eps for p, q in zip(a, b) for x, y in zip(p, q))


def test_video_identity():
    """коробка = объект ⇒ квад не меняется"""
    q = [(-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5)]
    assert close(ct.quad_for_ink(q, 1.0, 1.0), q)


def test_ink_lands_on_target():
    """главное свойство: гомография квада возвращает чернила ровно на цель"""
    import numpy as np
    fx, fy = 0.2, 0.07                       # надпись — крошечная доля коробки
    target = [(-0.6, -0.4), (0.6, -0.35), (-0.55, 0.45), (0.62, 0.5)]   # намеренно неправильный квад
    box = ct.quad_for_ink(target, fx, fy)
    # обратно: гомография «коробка → её новый квад», применённая к прямоугольнику чернил
    A, b = [], []
    for (sx, sy), (dx, dy) in zip(((-1, -1), (1, -1), (-1, 1), (1, 1)), box):
        A.append([sx, sy, 1, 0, 0, 0, -dx * sx, -dx * sy]); b.append(dx)
        A.append([0, 0, 0, sx, sy, 1, -dy * sx, -dy * sy]); b.append(dy)
    H = np.append(np.linalg.solve(np.array(A, float), np.array(b, float)), 1.0).reshape(3, 3)
    got = []
    for cx, cy in ((-fx, -fy), (fx, -fy), (-fx, fy), (fx, fy)):
        p = H @ np.array([cx, cy, 1.0]); got.append((p[0] / p[2], p[1] / p[2]))
    assert close(got, target, 1e-9), got


def test_smaller_ink_blows_box_up():
    """чем мельче чернила, тем дальше уезжает коробка — и ровно во столько же раз"""
    t = [(-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5)]
    for f in (0.5, 0.25, 0.1):
        box = ct.quad_for_ink(t, f, f)
        assert abs(box[0][0] - (-0.5 / f)) < 1e-9, (f, box)


def test_offset_ink_lands_on_target():
    """СМЕЩЁННАЯ надпись: цель обязана достаться чернилам, а не тому месту, где они были бы по центру.
    Без учёта центра квад уезжает ровно на смещение — эту дыру ловит именно этот тест."""
    import numpy as np
    fx, fy, cx, cy = 0.2, 0.07, 0.3, -0.2
    target = [(-0.4, -0.3), (0.4, -0.3), (-0.4, 0.3), (0.4, 0.3)]
    box = ct.quad_for_ink(target, fx, fy, cx, cy)
    A, b = [], []
    for (sx, sy), (dx, dy) in zip(((-1, -1), (1, -1), (-1, 1), (1, 1)), box):
        A.append([sx, sy, 1, 0, 0, 0, -dx * sx, -dx * sy]); b.append(dx)
        A.append([0, 0, 0, sx, sy, 1, -dy * sx, -dy * sy]); b.append(dy)
    H = np.append(np.linalg.solve(np.array(A, float), np.array(b, float)), 1.0).reshape(3, 3)
    got = []
    for px, py in ((cx - fx, cy - fy), (cx + fx, cy - fy), (cx - fx, cy + fy), (cx + fx, cy + fy)):
        p = H @ np.array([px, py, 1.0]); got.append((p[0] / p[2], p[1] / p[2]))
    assert close(got, target, 1e-9), got
    assert not close(box, ct.quad_for_ink(target, fx, fy)), "центр не учитывается — это и была дыра"


def test_rect_reads_offset_from_inner_text():
    """смещение берём у ВНУТРЕННЕГО текста: живой заворот оставляет его внутри компаунда,
    и y при этом переворачивается (в драфте вверх, в NDC углов вниз)"""
    draft = {"materials": {"drafts": [{"id": "C", "draft": {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"texts": [{"id": "T"}]},
        "tracks": [{"segments": [{"material_id": "T", "clip": {
            "scale": {"x": 4.905}, "transform": {"x": 0.3, "y": 0.2}}}]}]}}]}}
    cx, cy, fx, fy = ct.ink_rect(draft, {"id": "P", "extra_material_refs": ["C"]}, (220.2, 131.5))
    assert abs(cx - 0.3) < 1e-9 and abs(cy + 0.2) < 1e-9, (cx, cy)
    assert abs(fx - 1.0) < 0.01 and abs(fy - 0.336) < 0.01, (fx, fy)


def test_fractions_video_and_text():
    """доли: у обычного слоя (1,1); у компаунда — от внутреннего канваса и масштаба текста"""
    assert ct.ink_fractions({}, {"id": "x"}, None) == (1.0, 1.0)
    draft = {"materials": {"drafts": [{"id": "C", "draft": {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {"texts": [{"id": "T"}]},
        "tracks": [{"segments": [{"material_id": "T", "clip": {"scale": {"x": 4.905}}}]}]}}]}}
    seg = {"id": "P", "extra_material_refs": ["C"]}
    fx, fy = ct.ink_fractions(draft, seg, (220.2, 131.5))
    assert abs(fx - 1.0) < 0.01 and abs(fy - 0.336) < 0.01, (fx, fy)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok:", name)
    print("регресс пройден")
