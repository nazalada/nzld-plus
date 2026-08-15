"""Развод процессов: pycolmap НЕ должен оказаться в процессе плагина.

Главная проверка — последняя: если однажды кто-то вернёт прямой вызов track3d в наш процесс,
рядом с torch поднимется вторая libomp, и без заглушки KMP_DUPLICATE_LIB_OK это abort, а с
заглушкой — что хуже — молча неверные числа солвера. Тест дешёвый: солв не гоняем.
"""
import os
import sys

import capcut_track as ct


def main():
    assert ct.track3d_available(), "pycolmap не установлен — 3D-трек не заработает"
    assert "pycolmap" not in sys.modules, "track3d_available() не должен ИМПОРТИРОВАТЬ pycolmap"

    r = ct.track3d_call({"op": "ping"}, log=lambda *_: None)
    assert r["pid"] != os.getpid(), "солвер обязан считаться в ОТДЕЛЬНОМ процессе"
    pid = r["pid"]

    # процесс долгоживущий: второй запрос уходит в тот же pid, иначе сцена предпросмотра
    # терялась бы между крутилками
    assert ct.track3d_call({"op": "ping"}, log=lambda *_: None)["pid"] == pid

    # отказ приходит строкой и НЕ роняет воркер
    for req, needle in (({"op": "нетТакойОперации"}, ""),
                        ({"op": "object_at", "key": "нет такой сцены", "obj": {}, "t_src": 0},
                         "не подготовлена")):
        try:
            ct.track3d_call(req, log=lambda *_: None)
            raise AssertionError(f"ждали отказ на {req['op']}")
        except ValueError as e:
            assert needle in str(e), (needle, str(e))
    assert ct.track3d_call({"op": "ping"}, log=lambda *_: None)["pid"] == pid, "воркер умер на отказе"

    assert "pycolmap" not in sys.modules, "pycolmap просочился в процесс плагина"
    print(f"ok: солвер в процессе {pid}, pycolmap в нашем процессе отсутствует")


if __name__ == "__main__":
    main()
