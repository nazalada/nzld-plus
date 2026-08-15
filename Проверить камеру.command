#!/bin/zsh
# Двойной клик = полный ЖИВОЙ прогон 3D-камеры: поднять CapCut с плагином, открыть проект,
# дождаться параллакса, снять скриншот, вынести вердикт. Нужен НЕЗАПЕРТЫЙ экран.
cd "$(dirname "$0")" || exit 1
# свежий тул-сервер: осиротевший старый отдаёт устаревший код и держит порт
lsof -ti tcp:8756 2>/dev/null | xargs kill 2>/dev/null
BROWSER=none .venv/bin/python app.py > "$HOME/Movies/CapCut/User Data/ve_server.log" 2>&1 &
sleep 2
.venv/bin/python check_camera.py "${1:-0715 (2)}"
echo "(окно можно закрыть)"
