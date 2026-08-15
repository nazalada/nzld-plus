// Рамка ROI поверх РОДНОГО плеера — переиспользуем рамку самого CapCut.
// База: qrc:/Player/view/area_select/RectangleSelectorBase.qml (пунктир DashedBox + 8 ручек
// SizeControl + перетаскивание + прилипание к направляющим + перекрестье в центре).
// Инжектится в EditControl_QMLTYPE_* (Player/view/EditControl.qml), где на строке 2716 они сами
// вешают CustomTrackingSelector для своего отслеживания. Берём БАЗУ: у неё вьюмодель с дефолтом,
// поэтому видимостью и геометрией управляем мы, а не их трекер.
// Импорты — точная копия импортов EditControl.qml (мы создаёмся с его контекстом → id `root` его).
import QtQuick
import QtQuick.Window
import Qt5Compat.GraphicalEffects
import FusionUI
import Player

import Foundation
import GeneralBiz
import VECreator
import Agent

RectangleSelectorBase {
    id: sel

    // realRenderRect / absorLineControler — из EditControl через наследование контекста,
    // ровно как это делает их собственный CustomTrackingSelector.
    realRenderRect: root.realRenderRect
    absorLineControler: root.absorLineControler
    color: Qt.rgba(0, 192 / 255, 204 / 255, 1)   // их бирюза, как в EditControl.qml:2718

    viewModel: AreaSelectBaseViewModel {}

    property string pluginDbg: "(init)"
    // Показ решает панель: "1" только когда открыта вкладка «Трекер» И заданы источник + follower.
    // Строкой, а не bool: QVariant(QString)->bool конвертится молча и непредсказуемо.
    property string roiShow: "0"
    onRoiShowChanged: sel.viewModel.visible = (roiShow === "1")
    // Геометрия для бэкенда — ДОЛИ канваса (0..1). Считаем от самого item'а относительно
    // realRenderRect (= реальная область картинки), а НЕ из вьюмодели: там единицы смешанные
    // (centerX в NDC [-1,1], а width в пикселях) — не гадаем, меряем.
    property string roiJson: ""

    function recompute() {
        var rr = sel.realRenderRect
        if (!rr || rr.width <= 0 || rr.height <= 0 || sel.width <= 0 || sel.height <= 0) {
            roiJson = ""
            return
        }
        var fx = (sel.x - rr.x) / rr.width
        var fy = (sel.y - rr.y) / rr.height
        var fw = sel.width / rr.width
        var fh = sel.height / rr.height
        if (fw < 0.01 || fh < 0.01) { roiJson = ""; return }
        roiJson = JSON.stringify([fx, fy, fw, fh])
    }

    onXChanged: recompute()
    onYChanged: recompute()
    onWidthChanged: recompute()
    onHeightChanged: recompute()

    Component.onCompleted: {
        var d = []
        function probe(n, f) { try { d.push(n + "=" + f()) } catch (e) { d.push(n + " ERR:" + e) } }
        probe("rr", function () { return JSON.stringify(root.realRenderRect) })
        probe("vm", function () { return sel.viewModel })
        // Стартовая рамка — по центру, ~40% канваса, но СКРЫТАЯ: покажет панель, когда выберут
        // режим трекера и оба слоя. Единицы вьюмодели ИЗМЕРЕНЫ: centerX/Y — NDC [-1,1],
        // width/height — экранные пиксели (у дефолтной вьюмодели videoX/Y=0, xScale/yScale=1).
        probe("set", function () {
            sel.viewModel.visible = (sel.roiShow === "1")
            sel.viewModel.centerX = 0
            sel.viewModel.centerY = 0
            sel.viewModel.width = root.realRenderRect.width * 0.4
            sel.viewModel.height = root.realRenderRect.height * 0.4
            return "ok"
        })
        probe("vmvals", function () {
            return "vis=" + sel.viewModel.visible + " cx=" + sel.viewModel.centerX +
                   " cy=" + sel.viewModel.centerY + " w=" + sel.viewModel.width +
                   " h=" + sel.viewModel.height + " vx=" + sel.videoX + " vy=" + sel.videoY +
                   " sx=" + sel.xScale + " sy=" + sel.yScale
        })
        probe("item", function () {
            return "x=" + sel.x.toFixed(1) + " y=" + sel.y.toFixed(1) +
                   " w=" + sel.width.toFixed(1) + " h=" + sel.height.toFixed(1) +
                   " vis=" + sel.visible
        })
        recompute()
        probe("roi", function () { return sel.roiJson })
        // ИЩЕМ ПЛЕЙХЕД: g_playhead в C++ ненадёжен (его хук — путь восстановления кейфреймов,
        // на клипе без ключей не стрельнет ни разу). Пробуем найти время в контексте плеера.
        // currentProgressText НЕ существует (старая заметка врала) — перечислил свойства
        // VEPlayerViewModel и щупаю кандидатов на плейхед.
        // ИЗМЕРЕНО: плейхеда на VEPlayerViewModel НЕТ. Свойства перебраны — есть nextFrame/preFrame/
        // firstFrame/lastFrame (методы), seekingProgress и visibleProgressInfo БУЛЕВЫ, statusInfo =
        // заголовок плеера. Старая заметка про playerViewModel.currentProgressText — НЕВЕРНА.
        // Время держит вьюмодель ТАЙМЛАЙНА (модуль TimeLine, другое дерево) — искать там.
        sel.pluginDbg = d.join(" | ")
    }
}
