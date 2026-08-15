#!/usr/bin/env python3
"""Автоматический ЖИВОЙ прогон 3D-камеры: поднять CapCut с плагином, открыть проект,
дождаться драйва, снять скриншот и вынести вердикт по логу.

Зачем: живой тест требует незапертого экрана (окна CapCut не композитятся, пока Mac заперт).
Этот скрипт делает весь прогон сам — двойной клик вместо ручной проверки.

Запуск: двойной клик по «Проверить камеру.command» либо
    .venv/bin/python check_camera.py [имя проекта]
"""
import os
import subprocess
import sys
import time

import capcut_ui

HOME = os.path.expanduser("~")
UD = os.path.join(HOME, "Movies/CapCut/User Data")
LOG = os.path.join(UD, "plugin_probe.log")
DYLIB = os.path.join(UD, "ve_hook.dylib")
PROJ_ROOT = os.path.join(UD, "Projects/com.lveditor.draft")
SHOTS = os.path.join(UD, "plugin_shots")
CAPCUT = "/Applications/CapCut.app/Contents/MacOS/CapCut"


def screen_locked():
    import Quartz
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(d.get("CGSSessionScreenIsLocked"))


def shot(tag):
    """Снимок ГЛАВНОГО окна CapCut (самого большого) -> PNG. Возвращает путь или None."""
    import Quartz
    os.makedirs(SHOTS, exist_ok=True)
    wl = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                           Quartz.kCGNullWindowID) or []
    best, area = None, 0
    for w in wl:
        if "CapCut" not in str(w.get("kCGWindowOwnerName", "")) or w.get("kCGWindowLayer"):
            continue
        b = w.get("kCGWindowBounds", {})
        a = b.get("Width", 0) * b.get("Height", 0)
        if a > area:
            best, area = w.get("kCGWindowNumber"), a
    if not best:
        return None
    img = Quartz.CGWindowListCreateImage(Quartz.CGRectNull,
                                         Quartz.kCGWindowListOptionIncludingWindow,
                                         best, Quartz.kCGWindowImageBoundsIgnoreFraming)
    if not img:
        return None
    p = os.path.join(SHOTS, f"{tag}.png")
    url = Quartz.CFURLCreateWithFileSystemPath(None, p, Quartz.kCFURLPOSIXPathStyle, False)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, img, None)
    return p if Quartz.CGImageDestinationFinalize(dest) else None


def launch_injected():
    """CapCut с инжектом. ВАЖНО: переменную должен экспортировать промежуточный скрипт и сделать
    exec — если задать её inline перед командой, до процесса она не доезжает (проверено)."""
    sh = "/tmp/ve_launch.sh"
    with open(sh, "w") as f:
        f.write(f'#!/bin/zsh\nexport DYLD_INSERT_LIBRARIES="{DYLIB}"\nexec "{CAPCUT}"\n')
    os.chmod(sh, 0o755)
    subprocess.Popen([sh], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def injected(pid):
    out = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True).stdout
    return "DYLD_INSERT_LIBRARIES" in out


def capcut_pid():
    r = subprocess.run(["pgrep", "-f", "MacOS/CapCut"], capture_output=True, text=True)
    return int(r.stdout.split()[0]) if r.stdout.strip() else None


def log_has(*needles, since=0):
    try:
        txt = open(LOG, "rb").read().decode("utf-8", "replace")[since:]
    except OSError:
        return {}
    return {n: (n in txt) for n in needles}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "0715 (2)"
    proj = os.path.join(PROJ_ROOT, name)
    ok = fail = 0

    def say(good, text):
        nonlocal ok, fail
        print(f"  {'✓' if good else '✗'} {text}")
        if good:
            ok += 1
        else:
            fail += 1

    print(f"\n════ ЖИВОЙ ПРОГОН 3D-КАМЕРЫ · «{name}» ════\n")
    if screen_locked():
        print("  ✗ Экран заблокирован — окна CapCut не рисуются, тест невозможен.")
        print("    Разблокируй Mac и запусти снова.\n")
        return 1
    if not os.path.isdir(proj):
        print(f"  ✗ Проект не найден: {proj}\n")
        return 1

    subprocess.run(["pkill", "-x", "CapCut"], capture_output=True)
    time.sleep(2)
    lock = os.path.join(proj, ".locked")
    if os.path.exists(lock):
        os.unlink(lock)          # протухший лок от прошлого запуска
    open(LOG, "w").close()

    print("1. Поднимаю CapCut с плагином…")
    launch_injected()
    pid = None
    for _ in range(40):
        time.sleep(1)
        pid = capcut_pid()
        if pid:
            break
    say(bool(pid), f"процесс запущен (pid {pid})")
    if not pid:
        return 1
    time.sleep(6)
    say(injected(pid), "плагин инжектнут (DYLD_INSERT_LIBRARIES в процессе)")
    h = log_has("ve_hook loaded", "RIG loaded")
    say(h.get("ve_hook loaded"), "дилиб загрузился")
    say(h.get("RIG loaded"), "риг сцены прочитан")

    print("\n2. Открываю проект…")
    try:
        capcut_ui.reopen(name, lock)
    except Exception as e:                                   # noqa: BLE001
        print(f"    (авто-открытие: {e})")
    for _ in range(30):
        time.sleep(1)
        if os.path.exists(lock):
            break
    say(os.path.exists(lock), "проект открыт")

    print("\n3. Жду, пока камера прогонит слои…")
    mark = 0
    drive = False
    for _ in range(25):
        time.sleep(1)
        if log_has("DRIVE ", since=mark).get("DRIVE "):
            drive = True
            break
    say(drive, "драйв параллакса сработал (строка DRIVE в логе)")
    inj = log_has("errorString=[]", "VEPluginTab")
    say(inj.get("errorString=[]"), "панель скомпилировалась без ошибок")
    say(inj.get("VEPluginTab"), "вкладка плагина встала в интерфейс")

    print("\n4. Снимаю экран…")
    p = shot("editor")
    say(bool(p), f"скриншот: {p or '—'}")

    print(f"\n════ ИТОГ: {ok} ок, {fail} провалено ════")
    if fail == 0:
        print("Всё живое. Открой вкладку плагина → «3D камера» и подвигай «Камера 3D» в плеере.\n")
    else:
        print(f"Смотри лог: {LOG}\n")
    for line in ("DRIVE ", "MATCH cam", "CLEAR "):
        try:
            hits = [l for l in open(LOG, encoding="utf-8", errors="replace") if line in l]
            if hits:
                print(f"  {hits[-1].strip()[:120]}")
        except OSError:
            pass
    print()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
