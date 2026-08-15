"""CapCut — целиком на ВТОРОЙ экран и в полный экран.

Зачем: когда окно на одном мониторе, а меню всплывает на другом, координатные клики
скриптов уезжают мимо. Один монитор + фуллскрин = координаты стабильны.
⚠️ AX-«window 1» — НЕ главное окно: ловил служебное 183×88. Берём самое большое по площади.
"""
import os
import subprocess
import time

import Quartz


def displays():
    err, ids, cnt = Quartz.CGGetActiveDisplayList(8, None, None)
    out = []
    for d in ids[:cnt]:
        b = Quartz.CGDisplayBounds(d)
        out.append((d, int(b.origin.x), int(b.origin.y), int(b.size.width), int(b.size.height),
                    bool(Quartz.CGDisplayIsMain(d))))
    return out


def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def ax(cmd):
    return osa(f'tell application "System Events" to tell process "CapCut" to {cmd}')


def main_window_index():
    return osa('''tell application "System Events" to tell process "CapCut"
	set best to 0
	set bi to 1
	set i to 0
	repeat with w in windows
		set i to i + 1
		try
			set sz to size of w
			set a to (item 1 of sz) * (item 2 of sz)
			if a > best then
				set best to a
				set bi to i
			end if
		end try
	end repeat
	return bi as string
end tell''')


def project_open():
    """Открыт ли проект: двигать окно во время работы редактора НЕЛЬЗЯ."""
    import glob
    return bool(glob.glob(os.path.expanduser(
        "~/Movies/CapCut/User Data/Projects/com.lveditor.draft/*/.locked")))


def place():
    if project_open():
        return ("проект открыт — окно не трогаю: перенос на другой экран во время работы "
                "редактора гасит QML-раскладку, редактор становится серым (поймано 2026-08-09)")
    ds = displays()
    second = [d for d in ds if not d[5]]
    if not second:
        return "второго экрана нет — оставляю как есть"
    _, x, y, w, h, _ = second[0]
    subprocess.run(["osascript", "-e", 'tell application "CapCut" to activate'])
    time.sleep(0.8)
    try:
        wi = max(1, int(main_window_index()))
    except ValueError:
        return "AX не отдал окна (CapCut в состоянии без окон) — перезапусти его"
    ax(f'set value of attribute "AXFullScreen" of window {wi} to false')   # иначе окно не двигается
    time.sleep(0.7)
    ax(f'set position of window {wi} to {{{x + 20}, {y + 20}}}')
    ax(f'set size of window {wi} to {{{w - 40}, {h - 80}}}')
    time.sleep(0.5)
    fs = ax(f'set value of attribute "AXFullScreen" of window {wi} to true')
    time.sleep(2.0)
    return (f"экран {w}×{h} @({x},{y}) | окно #{wi}: {ax(f'return position of window {wi}')} "
            f"размер {ax(f'return size of window {wi}')} | фуллскрин: {fs or 'ok'}")


if __name__ == "__main__":
    for d in displays():
        print(f"  экран {d[0]}: {d[3]}×{d[4]} @({d[1]},{d[2]})"
              + (" — основной" if d[5] else " — ВТОРОЙ"))
    print(place())
