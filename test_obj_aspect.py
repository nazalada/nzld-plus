"""Автовысота объекта: h=0 ⇒ прямоугольник объекта повторяет пропорции коробки слоя.

Регресс на замер 2026-08-08: коробка текста 3.06:1, объект задан вручную 4.53:1 —
corner-pin натянул одно на другое и сплющил текст ровно на их отношение.
"""
import numpy as np

import track3d

ASPECT = 1080 / 353.0                       # коробка компаунда с текстом, замер BBOX2
LENS = [2.0, 1.0]                           # репер: квад вдвое шире, чем выше


def main():
    o = track3d.resolve_obj(LENS, {"w": 0.6, "h": 0.0, "aspect": ASPECT})
    assert abs((o["w"] * LENS[0]) / (o["h"] * LENS[1]) - ASPECT) < 1e-9, o

    # заданную руками высоту не трогаем, даже если аспект известен
    assert track3d.resolve_obj(LENS, {"w": 0.6, "h": 0.2, "aspect": ASPECT})["h"] == 0.2
    # без аспекта — прежнее поведение, объект во весь квад
    assert track3d.resolve_obj(LENS, {"h": 0})["h"] == 1.0
    assert track3d.resolve_obj(LENS, None)["h"] == 1.0

    # и в мировых углах отношение сторон получается тем же
    basis = (np.zeros(3), np.array([2.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 2.0]))
    c = track3d.object_corners(basis, 0, 0, 0, o["w"], o["h"])
    w, h = np.linalg.norm(c[1] - c[0]), np.linalg.norm(c[2] - c[0])
    assert abs(w / h - ASPECT) < 1e-6, (w, h)
    print(f"ok: h={o['h']:.4f} при w={o['w']}, стороны объекта {w:.3f}×{h:.3f}")


if __name__ == "__main__":
    main()
