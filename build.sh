#!/bin/bash
# Сборка и деплой ve_hook.dylib. Деплой ТОЛЬКО через mv: cp после подписи бьёт хеши,
# CapCut отказывается грузить библиотеку с невалидной подписью.
set -e
cd "$(dirname "$0")"
OUT="$HOME/Movies/CapCut/User Data/ve_hook.dylib"
STAGE="$OUT.new"
# БЕЗ -O2 намеренно: так собраны все рабочие версии (сверено по числу символов).
# Хуки — ручные детуры с украденными инструкциями, менять уровень оптимизации = менять
# то, что компилятор делает с trampoline-кодом. Выигрыш не стоит риска.
clang++ -arch arm64 -dynamiclib -std=c++17 \
    -Wno-deprecated-declarations -Wno-unused-function \
    -framework Foundation -framework AppKit -framework CoreGraphics -framework WebKit \
    -o "$STAGE" ve_hook.cpp
codesign -f -s - "$STAGE"
codesign --verify --strict "$STAGE"
mv "$STAGE" "$OUT"
echo "OK: $(stat -f%z "$OUT") байт"
