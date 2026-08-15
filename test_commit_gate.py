#!/usr/bin/env python3
"""Гейт записи в драфт у commit_camera: при ОТКРЫТОМ проекте — громкий отказ до записи,
при закрытом — работа как раньше. Причина теста: close_to_home пропускала работу по
ОТСУТСТВИЮ улик (нет .locked ⇒ «закрыт»), и ключи уходили в удерживаемый CapCut файл,
где автосейв стирал их молча. Раз это путь потери данных — проверяем ОБА случая.

Запуск: python3 test_commit_gate.py
"""
import json
import os
import tempfile

import capcut_track as ct

DRAFT = {"canvas_config": {"width": 1920, "height": 1080}, "tracks": [], "materials": {}}


def _proj():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "draft_info.json"), "w") as f:
        json.dump(DRAFT, f)
    return d


def _fake_ui(running, home):
    class UI:
        is_running = staticmethod(lambda: running)
        is_home = staticmethod(lambda: home)
        is_editing = staticmethod(lambda: not home)
        go_home = staticmethod(lambda: None)
        show_console = staticmethod(lambda: None)
        reopen = staticmethod(lambda *a: None)
    return UI


def run(running, home, proj, held=None):
    """Вернуть текст ошибки commit_camera при заданном состоянии CapCut.
    held — держит ли CapCut файлы проекта (второй, независимый признак открытости)."""
    ui, sleep, wrote = ct.capcut_ui, ct.time.sleep, []
    hp = ct.holds_project
    ct.capcut_ui = _fake_ui(running, home)
    ct.holds_project = lambda p: (running and not home) if held is None else held
    ct.time.sleep = lambda s: None          # 12 попыток go_home по секунде — не ждём их живьём
    inj = ct.inject_curves
    ct.inject_curves = lambda *a, **k: wrote.append(a) or 0
    try:
        ct.commit_camera(proj, ["x"], [0.5], log=lambda *a: None)
        return "", wrote
    except Exception as e:  # noqa: BLE001 — текст ошибки и есть предмет проверки
        return str(e), wrote
    finally:
        ct.capcut_ui, ct.time.sleep, ct.inject_curves, ct.holds_project = ui, sleep, inj, hp


def stale_lock(held):
    """Снимет ли _stale_lock замок, когда CapCut держит/не держит проект."""
    import camera
    d = tempfile.mkdtemp()
    lock = os.path.join(d, ".locked")
    open(lock, "w").close()
    orig = ct.holds_project
    ct.holds_project = lambda p: held
    try:
        camera._stale_lock(d, log=lambda *a: None)
    finally:
        ct.holds_project = orig
    return not os.path.exists(lock)


if __name__ == "__main__":
    # ЗАМОК: снимать его можно, только если проект НИКТО не держит. Раньше спрашивали «запущен ли
    # CapCut», а пока он стартует, pgrep его не видит — так мы сносили ЖИВОЙ замок, после чего
    # все проверки «проект открыт?» врали «закрыт» и запись уходила в удерживаемый файл
    # (так пропала камера в «3D CAMERA» 2026-08-09).
    assert stale_lock(held=False) is True, "проект никем не удерживается — протухший замок снять"
    assert stale_lock(held=True) is False, "CapCut держит проект — замок живой, не трогать"

    proj = _proj()

    # 1) ПРОЕКТ ОТКРЫТ (и .locked нет — ровно тот случай, где старая проверка врала «закрыт»)
    err, wrote = run(running=True, home=False, proj=proj)
    assert "держит проект открытым" in err, f"открытый проект не отказал: {err!r}"
    assert not wrote, "отказ случился ПОСЛЕ записи — ключи уже потеряны"

    # 2) ПРОЕКТ ЗАКРЫТ — гейт пропускает, дальше обычная работа
    #    (упирается в отсутствие слоя камеры в пустом драфте — значит гейт пройден)
    err, wrote = run(running=True, home=True, proj=proj)
    assert "Камера 3D" in err, f"гейт не пропустил закрытый проект: {err!r}"

    # 3) CapCut вообще не запущен — файл ничей, тоже пропускаем
    err, wrote = run(running=False, home=False, proj=proj)
    assert "Камера 3D" in err, f"гейт не пропустил незапущенный CapCut: {err!r}"

    # 4) ПЛИТОК ГЛАВНОЙ НЕ ВИДНО, но проект уже никто не держит: is_home() врёт «не дома» —
    #    раньше это намертво тормозило запись на закрытом проекте, теперь пускает по дескрипторам
    err, wrote = run(running=True, home=False, proj=proj, held=False)
    assert "Камера 3D" in err, f"гейт застрял на ложном «не дома»: {err!r}"

    # 5) Обратная сторона: плиток не видно И проект удерживается — отказ, записи нет
    err, wrote = run(running=True, home=False, proj=proj, held=True)
    assert "держит проект открытым" in err and not wrote, f"удерживаемый проект пропущен: {err!r}"

    print("OK: открытый — отказ до записи; закрытый (в т.ч. когда AX молчит) — работает")
