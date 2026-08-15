#!/usr/bin/env python3
"""Автоматизация UI CapCut для MVP: закрыть проект на главную и открыть его обратно.
Требует один раз выданного Accessibility для терминала/приложения, запускающего скрипт.

click_tile / reopen кликают по координате миниатюры проекта (Quartz),
координата берётся из Accessibility по имени драфта — не по фиксированным пикселям.
"""
import os
import subprocess
import sys
import time

import Quartz


def _osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()
        # -1743 = macOS не дал управлять другими приложениями. Сырой текст osascript
        # доезжает до панели как есть и читается как поломка плагина, хотя чинится
        # одной галкой в настройках. Переводим на человеческий.
        if "-1743" in err or "not allowed" in err or "не разрешён" in err:
            raise RuntimeError(
                "macOS не разрешил плагину управлять CapCut. Открой «Системные настройки» → "
                "«Конфиденциальность и безопасность» → «Универсальный доступ» и включи там "
                "Терминал (то приложение, из которого запущен «CapCut Probe.command»). "
                "Потом перезапусти плагин.")
        raise RuntimeError(err or "osascript failed")
    return r.stdout.strip()


def is_running():
    return subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True).returncode == 0


def is_editing():
    """Открыт ли ПРОЕКТ в редакторе (а не главная). Надёжнее файла `.locked`: тот можно потерять —
    CapCut держит его открытым дескриптором, а мы (или pkill+ _stale_lock) могли его unlink'нуть,
    и `os.path.exists` навсегда врёт «закрыт», хотя проект открыт. Признак «пункт меню Вернуться на
    главную доступен» истинен РОВНО когда проект открыт (на главной он неактивен)."""
    if not is_running():
        return False
    try:
        out = _osa('tell application "System Events" to tell process "CapCut" to get enabled of '
                   'menu item "Вернуться на главную" of menu 1 of menu bar item "CapCut" of menu bar 1')
        return out.strip() == "true"
    except RuntimeError:
        return False   # окно в переходе / меню недоступно — считаем не в редакторе


def is_home():
    """ТОЧНО ли CapCut на главной. Ищем плитки проектов — они есть только там, то есть это
    ПОЛОЖИТЕЛЬНОЕ доказательство, а не вывод по отсутствию признаков.

    Зачем отдельно от is_editing(): та отвечает False и когда проект открыт, но меню недоступно
    (окно в переходе, приложение занято, проект только что создан и пункт «Вернуться на главную»
    ещё не включён). Для опроса это безобидно, а перед ЗАПИСЬЮ В ДРАФТ — нет: решив «закрыт»,
    когда на самом деле открыт, мы пишем слой в файл, который CapCut держит в памяти и
    перезапишет своим состоянием при ближайшем сохранении. Работа исчезает молча — ровно так
    камера «создалась и не появилась» в свежем проекте, где .locked ещё не было.
    Нет ответа — считаем, что НЕ на главной (осторожная сторона).

    АКТИВИРУЕМ ПЕРЕД ОПРОСОМ, и это обязательно. Замер 2026-08-08: сразу после go_home()
    у CapCut в Accessibility остаётся НОЛЬ окон (проект уже закрыт, .locked снят, пункт меню
    погас — а плиток не видно), и так держится минутами. is_home() честно отвечал «нет», и
    require_closed отказывал на закрытом проекте: «закрой проект и повтори» сразу после того,
    как сам его закрыл. Одного `activate` хватает — окно тут же появляется в AX.
    Ровно поэтому _activate_and_find всегда активирует первым делом. Опрашивать этим в цикле
    нельзя (утащит фокус) — зовётся только из require_closed перед записью."""
    if not is_running():
        return False
    try:
        out = _osa('''
tell application "CapCut" to activate
tell application "System Events" to tell process "CapCut"
    repeat with w in windows
        repeat with e in UI elements of w
            try
                if description of e starts with "HomePageDraftTitle:" then return "yes"
            end try
        end repeat
    end repeat
    return "no"
end tell''')
        return out.strip() == "yes"
    except RuntimeError:
        return False


def show_console():
    """Вывести окно терминала (сервер + прогресс-бар трекинга) на передний план, чтобы его
    не перекрывало окно CapCut. Иначе бар печатается, но его не видно за CapCut'ом."""
    for proc, appname in (("iTerm2", "iTerm"), ("Terminal", "Terminal")):
        if subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0:
            try:
                _osa(f'tell application "{appname}" to activate')
            except RuntimeError:
                pass
            return


def go_home():
    """CapCut → «Вернуться на главную». Сохраняет и закрывает текущий проект."""
    _osa('tell application "System Events" to tell process "CapCut" '
         'to click menu item "Вернуться на главную" of menu 1 of menu bar item "CapCut" of menu bar 1')


def _activate_and_find(draft_name):
    """За ОДИН вызов: активировать CapCut (вернуть фокус, если потерян) и найти координату
    миниатюры проекта во ВСЕХ окнах (второе окно/попап могло увести главную). None если нет."""
    out = _osa(f'''
tell application "CapCut" to activate
tell application "System Events" to tell process "CapCut"
    repeat with w in windows
        repeat with e in UI elements of w
            set d to ""
            try
                set d to description of e
            end try
            if d is "HomePageDraftTitle:{draft_name}" then
                set p to position of e
                set s to size of e
                return (((item 1 of p) + (item 1 of s) / 2) as integer as string) & "|" & (((item 2 of p) - 52) as integer as string)
            end if
        end repeat
    end repeat
    return "none"
end tell''')
    if out == "none":
        return None
    x, y = out.split("|")
    return float(x), float(y)


def tile_point(draft_name):
    return _activate_and_find(draft_name)


def double_click(x, y):
    def post(ev, clicks=0):
        e = Quartz.CGEventCreateMouseEvent(None, ev, (x, y), 0)
        if clicks:
            Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, clicks)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    post(Quartz.kCGEventMouseMoved)
    post(Quartz.kCGEventLeftMouseDown, 1)
    post(Quartz.kCGEventLeftMouseUp, 1)
    post(Quartz.kCGEventLeftMouseDown, 2)
    post(Quartz.kCGEventLeftMouseUp, 2)


HIDE_PANELS = os.path.expanduser("~/Movies/CapCut/User Data/ve_hide_panels.txt")


def _hide_panels(on):
    """Флаг для дилиба: спрятать вкладку+док плагина. Иначе webview-док (level 5) висит поверх
    «Главной» и накрывает плитку проекта — Quartz-клик reopen уходит в док, а не в плитку."""
    try:
        if on:
            open(HIDE_PANELS, "w").close()
        elif os.path.exists(HIDE_PANELS):
            os.remove(HIDE_PANELS)
    except OSError:
        pass


def reopen(draft_name, lock_path=None, timeout=25):
    """Открыть проект с главной. Каждую итерацию ДЕРЖИМ окно активным (потерял фокус —
    сразу возвращаем) и быстро кликаем плитку. Тугой цикл без длинных пауз.
    На время кликов прячем панели плагина, чтобы док не перехватывал клик по плитке."""
    _hide_panels(True)
    time.sleep(0.45)   # дать дилибу (poll 300мс) реально убрать панели до первого клика
    try:
        return _reopen_loop(draft_name, lock_path, timeout)
    finally:
        _hide_panels(False)


def _window_center():
    """Центр САМОГО БОЛЬШОГО окна CapCut в глобальных координатах.
    ⚠️ Жёсткую точку сюда писать нельзя: окно может жить на втором мониторе, и прокрутка
    уйдёт на чужой экран (поймано 2026-08-09)."""
    out = _osa('tell application "System Events" to tell process "CapCut"\n set best to 0\n set vx to 0\n set vy to 0\n repeat with w in windows\n  try\n   set p to position of w\n   set z to size of w\n   set a to (item 1 of z) * (item 2 of z)\n   if a > best then\n    set best to a\n    set vx to (item 1 of p) + (item 1 of z) / 2\n    set vy to (item 2 of p) + (item 2 of z) / 2\n   end if\n  end try\n end repeat\n return (vx as integer as string) & "|" & (vy as integer as string)\nend tell')
    try:
        x, y = out.split("|")
        return float(x), float(y)
    except (ValueError, AttributeError):
        return None


def _scroll_home(lines):
    """Прокрутить список проектов: плитка вне видимой части AX не видна, клик уходит мимо."""
    pt = _window_center()
    if not pt:
        return
    e = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines)
    Quartz.CGEventSetLocation(e, pt)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    time.sleep(0.35)


def _reopen_loop(draft_name, lock_path, timeout):
    deadline = time.time() + timeout
    tries = 0
    while time.time() < deadline:
        tries += 1
        if tries % 6 == 0:          # плитку не нашли подряд — крутим список: вверх, потом вниз
            _scroll_home(8 if (tries // 6) % 2 else -8)
        try:
            pt = _activate_and_find(draft_name)  # активация + поиск за один осаскрипт
        except RuntimeError:
            pt = None  # окно в переходе (-1719) — продолжаем
        if pt:
            double_click(*pt)
            if lock_path is None:
                return True
            for _ in range(4):  # быстрая проверка открытия
                if os.path.exists(lock_path):
                    return True
                time.sleep(0.1)
        else:
            time.sleep(0.12)
    return bool(lock_path) and os.path.exists(lock_path)


if __name__ == "__main__":
    # ручной тест: python capcut_ui.py home | reopen <name> | point <name>
    cmd = sys.argv[1] if len(sys.argv) > 1 else "point"
    if cmd == "home":
        go_home(); print("на главную")
    elif cmd == "reopen":
        print("открыт" if reopen(sys.argv[2]) else "плитка не найдена")
    else:
        print(tile_point(sys.argv[2] if len(sys.argv) > 2 else "new project tracking"))
