"""ОТДЕЛЬНЫЙ ПРОЦЕСС для решения камеры. Всё, что трогает pycolmap, живёт только здесь.

Зачем процесс, а не просто вызов: torch (обычный трекер) и pycolmap несут КАЖДЫЙ свою копию
libomp, и второй импорт в одном процессе валит бэкенд — `OMP: Error #15`, abort. Раньше это
глушилось `KMP_DUPLICATE_LIB_OK=TRUE`, а это официальный «небезопасный, неподдерживаемый»
обход самих OpenMP: он разрешает молча выдавать НЕВЕРНЫЕ результаты. В солвере камеры такой
отказ — худший из возможных: bundle adjustment вернёт правдоподобные числа, а трек уедет, и по
картинке этого не различить. Поэтому процессы разведены, а заглушка убрана.

Протокол — JSON-строки: запрос в stdin, ответы в stdout. Промежуточные строки `{"log": ...}`
зовущий отдаёт в свой лог, последняя строка — результат или `{"error": ...}`. Прогресс пишем
прямо в файл, путь приходит в задании: панель читает его сама, и посредник не нужен.

Воркер ДОЛГОЖИВУЩИЙ: состояние решённой сцены остаётся здесь между запросами, поэтому
предпросмотр (`object_at`) отвечает за миллисекунды, а не переплачивает солвом на каждую
крутилку.
"""
import json
import os
import sys
import traceback

import numpy as np

import track3d

STATES = {}


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def logger(msg):
    send({"log": str(msg)})


def progress_writer(path):
    """Тот же файл и тот же формат, что у capcut_track.write_progress — панель читает его."""
    if not path:
        return None

    def w(pct, msg=""):
        try:
            with open(path, "w") as f:
                f.write(f"{int(max(0, min(100, pct)))} {msg}")
        except OSError:
            pass
    return w


def quad_tuples(q):
    return [tuple(float(v) for v in p) for p in q]


def op_solve_quad(r):
    homos, times, quad_ref, meta = track3d.solve_quad(
        r["path"], quad_tuples(r["quad"]), r["work"], t0_us=r.get("t0_us", 0),
        t1_us=r.get("t1_us"), ref_us=r.get("ref_us"), obj=r.get("obj"),
        log=logger, progress=progress_writer(r.get("progress_path")))
    return {"homos": [np.asarray(h).tolist() for h in homos], "times": list(times),
            "quad_ref": np.asarray(quad_ref).tolist(), "meta": meta}


def op_prepare(r):
    st = track3d.prepare_plane(
        r["path"], quad_tuples(r["quad"]), r["work"], t0_us=r.get("t0_us", 0),
        t1_us=r.get("t1_us"), ref_us=r.get("ref_us"),
        log=logger, progress=progress_writer(r.get("progress_path")))
    STATES[r["key"]] = st
    return {"ref_src_us": st["ref_src_us"], "meta": st["meta"]}


def op_object_at(r):
    st = STATES.get(r["key"])
    if st is None:
        raise ValueError("сцена не подготовлена (процесс солвера перезапускался) — нажми «Подготовить»")
    H, quad_obj = track3d.object_at(st, r.get("obj"), float(r["t_src"]))
    return {"H": np.asarray(H).tolist(), "quad_obj": np.asarray(quad_obj).tolist()}


def op_drop(r):
    STATES.pop(r.get("key"), None)
    return {"dropped": True}


OPS = {"solve_quad": op_solve_quad, "prepare": op_prepare, "object_at": op_object_at,
       "drop": op_drop, "ping": lambda r: {"pid": os.getpid()}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            send({"ok": True, **OPS[r["op"]](r)})
        except Exception as e:  # noqa: BLE001 — любой отказ уходит зовущему строкой, процесс живёт дальше
            send({"error": f"{e}", "trace": traceback.format_exc()[-800:]})


if __name__ == "__main__":
    main()
