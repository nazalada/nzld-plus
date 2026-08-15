#!/bin/bash
# Установка NZLD+ на чистую машину.
#
#   ./install.sh            — поставить всё: морду, дилиб, Python-окружение
#   ./install.sh --collect  — забрать морду и дилиб из рабочей установки в репозиторий
#                             (это для разработчика перед коммитом, не для пользователя)
#
# Дилиб лежит в репозитории уже собранным и подписанным — Xcode пользователю не нужен.
# Разработчику пересборка — ./build.sh, там же правила «без -O2» и «mv, не cp».
set -e
cd "$(dirname "$0")"
UD="$HOME/Movies/CapCut/User Data"

die() { echo; echo "✗  $1"; [ -n "$2" ] && echo "   $2"; echo; exit 1; }

# ── режим разработчика: собрать в репозиторий то, что правилось живьём ───────────
if [ "$1" = "--collect" ]; then
    [ -d "$UD" ] || die "нет каталога $UD" "собирать нечего: рабочей установки не существует."
    cp "$UD"/*.qml qml/
    cp "$UD"/plugin_assets/*.svg qml/plugin_assets/
    cp "$UD/ve_hook.dylib" ve_hook.dylib
    # Отпечаток исходника, из которого собран лежащий рядом дилиб. install.sh его сверяет:
    # без этого легко закоммитить вчерашний бинарь и потом отлаживать не тот код.
    shasum -a 256 ve_hook.cpp > ve_hook.sha256
    echo "собрано в репозиторий: морда, дилиб ($(stat -f%z ve_hook.dylib) байт), отпечаток исходника"
    exit 0
fi

echo "NZLD+ — установка"
echo

# ── 1. карантин ─────────────────────────────────────────────────────────────────
# macOS вешает com.apple.quarantine на всё скачанное из сети: на ZIP с гитхаба —
# обязательно, на git clone — нет.
# ЗАМЕРЕНО 2026-08-12 (macOS 27.0): сам инжект карантин НЕ ломает — библиотека с меткой
# грузится и в обычный процесс, и в hardened с теми же энтайтлментами, что у CapCut.
# Снимаем метку ради ДРУГОГО: по «CapCut Probe.command» человек кликает мышью, а
# помеченный скрипт Finder встречает диалогом «неизвестный разработчик».
if xattr -p com.apple.quarantine . >/dev/null 2>&1 || xattr -p com.apple.quarantine ve_hook.dylib >/dev/null 2>&1; then
    echo "•  снимаю метку «скачано из интернета» — иначе macOS не даст запустить"
    echo "   «CapCut Probe.command» двойным кликом"
    xattr -dr com.apple.quarantine . 2>/dev/null || true
fi

# ── 2. CapCut ───────────────────────────────────────────────────────────────────
[ -d /Applications/CapCut.app ] || die "CapCut не найден в /Applications" \
    "Поставь CapCut и запусти его хотя бы раз, потом вернись сюда."
[ -d "$UD" ] || die "нет каталога $UD" \
    "CapCut установлен, но ни разу не запускался. Запусти его один раз и вернись сюда."

# ── 3. морда ────────────────────────────────────────────────────────────────────
mkdir -p "$UD/plugin_assets"
cp qml/*.qml "$UD/"
cp qml/plugin_assets/*.svg "$UD/plugin_assets/"
echo "•  панель установлена"

# ── 4. дилиб ────────────────────────────────────────────────────────────────────
[ -f ve_hook.dylib ] || die "в репозитории нет ve_hook.dylib" \
    "Собери его сам: ./build.sh (нужен Xcode Command Line Tools)."

if [ -f ve_hook.sha256 ] && ! shasum -a 256 -c ve_hook.sha256 --status 2>/dev/null; then
    echo "⚠  дилиб собран НЕ из лежащего рядом ve_hook.cpp — работать будет он, а не исходник"
fi

# Кладём через mv, а не cp поверх живого файла: CapCut может держать старую библиотеку
# отображённой в память, и запись в тот же inode бьёт page hashes уже загруженного кода.
cp ve_hook.dylib "$UD/ve_hook.dylib.new"
xattr -c "$UD/ve_hook.dylib.new" 2>/dev/null || true
codesign --verify --strict "$UD/ve_hook.dylib.new" 2>/dev/null || {
    rm -f "$UD/ve_hook.dylib.new"
    die "подпись дилиба не прошла проверку" \
        "Файл повреждён при скачивании. Скачай репозиторий заново (лучше git clone, а не ZIP)."
}
mv "$UD/ve_hook.dylib.new" "$UD/ve_hook.dylib"
echo "•  дилиб установлен, подпись валидна"

# ── 5. Python ───────────────────────────────────────────────────────────────────
# Пол — 3.12 (его требует numpy). У macOS «из коробки» python3 = 3.9.6, и на нём pip
# падает с «No matching distribution found for torch», что читается как «нет такой
# версии torch», а не «твой питон слишком старый». Поэтому ищем сами и говорим прямо.
find_python() {
    local c p
    for c in python3.14 python3.13 python3.12 python3; do
        p=$(command -v "$c" 2>/dev/null) || continue
        if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            echo "$p"; return 0
        fi
    done
    return 1
}

if [ ! -x .venv/bin/python ]; then
    PY=$(find_python) || die "не нашёл Python 3.12 или новее" \
"Тот python3, что идёт с macOS, — 3.9.6, его не хватает (numpy требует 3.12+).
   Поставь любым способом и запусти установку снова:
     brew install python@3.14
   либо скачай установщик с https://www.python.org/downloads/macos/"
    echo "•  Python: $PY ($("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
    "$PY" -m venv .venv || die "не удалось создать окружение .venv"
fi

echo "•  ставлю зависимости (≈207 МБ качается, ≈950 МБ на диске — дольше всего torch)"
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || die "не удалось поставить зависимости" "Проверь интернет и запусти установку снова."

echo
echo "Готово. Запуск — двойной клик по «CapCut Probe.command»."
echo "При первом трекинге скачается модель CoTracker3, нужен интернет."
