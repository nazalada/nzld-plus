#!/usr/bin/env python3
"""Что решает _sync_render перед выстрелом: стрелять ли вообще, каким свойством, в какое время
и надо ли досылать второй раз. Ошибка тут не падает, а тихо мажет — команда снимает СТАРОЕ
значение или уходит в закрытый проект. Живьём это ловится только замером, поэтому здесь.

Запуск: python3 test_camera_sync.py
"""
import os
import tempfile

import camera
import capcut_track as ct

DRAFT = {"tracks": [
    {"id": "T-ПОЗЖЕ", "segments": [{"id": "S-ПОЗЖЕ",          # окно камеры его НЕ накрывает
                                    "target_timerange": {"start": 12_000_000, "duration": 2_000_000},
                                    "source_timerange": {"start": 0, "duration": 2_000_000}}]},
    {"id": "T-ГЛАВНЫЙ", "segments": [{"id": "S-ГЛАВНЫЙ",
                                      "target_timerange": {"start": 1_000_000, "duration": 6_000_000},
                                      "source_timerange": {"start": 500_000, "duration": 6_000_000}}]},
]}
SCENE = {"camId": "CAM", "tracks": {"T-ПОЗЖЕ": 0.0, "T-ГЛАВНЫЙ": 1.0},
         "camKfs": [{"t": 0}, {"t": 3_000_000}, {"t": 9_000_000}]}
DRIVE, CMD = "DRIVE scenes=1 wrote 7/7\n", "MKREQ отправлена api=addCommonKeyframe\n"


def call(log_after="драйв раньше", drive_later=True, **scene):
    """Прогнать _sync_render на подставном CapCut. Возвращает список выстрелов."""
    shots = []
    sc = dict(SCENE, **scene)
    keys = ("_project_open", "_log_wait", "live_sync", "read_draft", "_scene_load_side", "VE_LOG_PATH")
    orig = {k: getattr(ct, k) for k in keys}
    fd, logpath = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        f.write({"драйв раньше": DRIVE + CMD, "команда раньше": CMD + DRIVE,
                 "драйва нет": CMD}[log_after])
    ct.VE_LOG_PATH = logpath
    ct._project_open = lambda p: sc.pop("_closed", None) is None
    ct._log_wait = lambda *a, **k: drive_later          # дождались ли ЗАПОЗДАВШЕГО драйва
    ct.read_draft = lambda p: DRAFT
    ct._scene_load_side = lambda p: {"scenes": {"TOK": sc}}
    ct.live_sync = lambda seg, prop, t, log=None: shots.append((seg["id"], prop, t)) or True
    try:
        camera._sync_render("/нет/такого", "TOK", 0, log=lambda *a: None)
    finally:
        for k, v in orig.items():
            setattr(ct, k, v)
        os.unlink(logpath)
    return shots


if __name__ == "__main__":
    # сегмент с 1.0с длиной 6с ловит ключ камеры на 3.0с; source-время = 500000 + (3000000-1000000)
    assert call() == [("S-ГЛАВНЫЙ", "KFTypePositionX", 2_500_000)], call()

    # облёт: позиции у слоя больше нет, дилиб пишет углы — стрелять надо по углу
    assert call(pan={"gain": 25.0})[0][1] == ct.CORNER_PROPS[0]
    assert call(tilts={"T-ГЛАВНЫЙ": [5.0, 0.0]})[0][1] == ct.CORNER_PROPS[0]
    assert call(pan={"gain": 0.0})[0][1] == "KFTypePositionX"   # облёт выключен — снова позиция

    # гонка на границе тика: команда обогнала драйв ⇒ сняла старое значение, нужен второй выстрел
    assert len(call(log_after="команда раньше")) == 2, "запоздавший драйв — надо дослать"
    assert len(call(log_after="драйв раньше")) == 1, "драйв успел — второй выстрел лишний"
    assert len(call(log_after="драйва нет", drive_later=False)) == 1, "драйва нет — не долбить"

    assert call(_closed=True) == [], "проект закрыт — команда уходить не должна"
    assert call(camKfs=[{"t": 20_000_000}]) == [], "ключей камеры внутри сегментов нет"
    assert call(tracks={}) == [], "ведомых дорожек нет"

    print("OK: один выстрел нужным свойством в существующий ключ; догоняем запоздавший драйв; "
          "в закрытый проект молчим")
