// Плейхед: невидимый Item внутри MainTimeLine — читает вьюмодель КУРСОРА таймлайна.
// В плеере времени нет (измерено: VEPlayerViewModel его не хранит), а курсор таймлайна —
// это и есть плейхед: MainTimeLine.qml:1551 объявляет MainTimeLineCursorViewModel,
// у него есть clickSeekByTime(t) ⇒ соответствие время↔пиксель он ЗНАЕТ.
// Импорты — точная копия импортов MainTimeLine.qml (создаёмся с его контекстом → id `cursor`/`root`).
import QtQml
import QtQuick
import QtQuick.Controls
import QtQuick.Shapes
import QtQuick.Window
import Qt5Compat.GraphicalEffects
import GeneralBiz
import FusionUI
import MaterialPanel
import TimeLine
import VECreator
import GeneralBizUI
import Player
import Agent

Item {
    id: probe
    visible: false
    width: 0
    height: 0

    property string pluginDbg: "(init)"

    // ИЗМЕРЕНО: у MainTimeLineCursorViewModel времени НЕТ — только centerX (пиксели канваса)
    // + метод clickSeekByTime(t). Масштаб лежит рядом: timeLineCanvas.zoomPixelSize
    // (= MainTimeLineCanvasViewModel.pixelSize), и он в ПИКСЕЛЯХ НА СЕКУНДУ — это видно по их же
    // константе VECConstKeys.kRulerPixelSize1s, с которой его сравнивают (MainTimeLine.qml:1473).
    // ⇒ время = centerX / pixelSize. Биндинги живые, так что playheadUs обновляется на скрабе сам.
    // ПЛЕЙХЕД, СВЕРЕННЫЙ С САМИМ CAPCUT: centerX=473.75, pxSize=7.6002575e-5 → 6233333 мкс,
    // а CapCut в тот же момент показывал 00:00:06:07 = 6с + 7кадров @30fps = 6233333 мкс. Точно.
    // pxSize (= MainTimeLineCanvasViewModel.pixelSize) — ПИКСЕЛИ НА МИКРОСЕКУНДУ (НЕ на секунду:
    // предположение «px/сек» из-за их же константы kRulerPixelSize1s оказалось неверным).
    property real pxSize: timeLineCanvas.zoomPixelSize
    property real curX: cursor.viewModel.centerX
    // Биндинг живой → на скрабе обновляется само; C++ забирает строку в панель.
    property string playheadUs: pxSize > 0 ? String(Math.round(curX / pxSize)) : "0"

    onPlayheadUsChanged: probe.pluginDbg = "us=" + playheadUs + " x=" + curX.toFixed(2)
    Component.onCompleted: probe.pluginDbg = "us=" + playheadUs + " x=" + curX.toFixed(2)
}
