// Кнопка NZLD+ в шапке дорожки: привязать/отвязать дорожку к 3D-камере прямо из таймлайна.
// Строится ИХ ЖЕ типом QUIButton (как родные замок/глаз/звук в TimeLineTrackHeader.qml),
// поэтому совпадает с ними по построению, а не «нарисована похоже».
//
// Какая это дорожка: у шапки есть viewModel с trackIndex — id дорожки в их вьюмодели НЕТ,
// только индекс. Индекс сопоставляем с /api/cam, где у каждой дорожки есть поле index.
// В таймлайне дорожки нарисованы сверху вниз, в драфте нумерация снизу вверх — отсюда разворот.
import QtQuick
import FusionUI
import GeneralBizUI

Item {
    id: btn

    readonly property string api: "http://127.0.0.1:8756"
    property string pluginDbg: "(init)"
    property string projDir: ""
    property string trackId: ""
    property bool bound: false
    property bool busy: false

    // Шапка — выше по дереву: мы лежим в Row, тот в Rectangle, тот в TimeLineTrackHeader.
    readonly property var hdr: {
        var p = btn.parent
        for (var i = 0; i < 4 && p; i++) {
            if (p.viewModel !== undefined) return p
            p = p.parent
        }
        return null
    }
    readonly property int trackIndex: hdr && hdr.viewModel ? (hdr.viewModel.trackIndex || 0) : -1

    width: 16
    height: 16

    function http(method, path, body, cb) {
        var x = new XMLHttpRequest()
        x.onreadystatechange = function () {
            if (x.readyState !== XMLHttpRequest.DONE) return
            var r = null
            try { r = JSON.parse(x.responseText) } catch (e) {}
            cb(x.status === 200 ? r : null)
        }
        x.open(method, btn.api + path)
        if (body) {
            x.setRequestHeader("Content-Type", "application/json")
            x.send(JSON.stringify(body))
        } else x.send()
    }

    // Состояние тянем с сервера: он знает и дорожки проекта, и привязки активной камеры.
    // ОПРАШИВАЕМ, а не читаем один раз при создании: во-первых, привязку можно менять и из
    // панели (два входа в одно состояние — иконка врала бы до перезахода), во-вторых, кнопка
    // создаётся раньше, чем проект успевает пометиться открытым, и первый ответ приходит
    // про ЧУЖОЙ проект. Поймано живьём: все спирали остались серыми при живых привязках.
    property bool loading: false

    Timer {
        interval: 2500; repeat: true; running: true
        onTriggered: if (!btn.loading && !btn.busy) btn.refresh()
    }

    function refresh() {
        if (btn.loading) return
        btn.loading = true
        http("GET", "/api/projects", null, function (ps) {
            if (!ps) { btn.loading = false; btn.pluginDbg = "сервер недоступен"; return }
            var pick = null
            for (var i = 0; i < ps.length; i++) if (ps[i].open) { pick = ps[i]; break }
            // ТОЛЬКО открытый проект. Раньше на «нет открытых» брали первый в списке — а кнопка
            // создаётся до того, как проект помечен открытым, и она читала состояние ЧУЖОГО
            // проекта: спирали оставались серыми при живых привязках. Лучше подождать опроса.
            if (!pick) { btn.loading = false; return }
            btn.projDir = pick.dir
            http("GET", "/api/cam?dir=" + encodeURIComponent(pick.dir), null, function (s) {
                btn.loading = false
                if (!s) return
                // ЗАМЕРЕНО (screenY каждой кнопки против позиций дорожек на экране):
                // trackIndex шапки СОВПАДАЕТ с индексом дорожки в драфте — 0 = НИЖНЯЯ дорожка.
                // Никаких разворотов и вычитаний: любая арифметика тут промахивалась на
                // служебные дорожки и кнопка привязывала ЧУЖУЮ (поймано на живом проекте).
                var tr = s.tracks || []
                var mine = null
                for (var j = 0; j < tr.length; j++) if (tr[j].index === btn.trackIndex) { mine = tr[j]; break }
                // Совпадения нет = это служебная дорожка (камера, DoF): их сервер отфильтровал.
                // Прячемся, но опрос продолжается — дорожки появляются и исчезают на лету.
                btn.visible = !!mine
                if (!mine) return
                btn.trackId = mine.id
                var cams = s.cameras || []
                var b = cams.length ? (cams[0].binds || {}) : ({})
                btn.bound = b[mine.id] !== undefined
                btn.pluginDbg = "idx=" + btn.trackIndex + " bound=" + btn.bound
            })
        })
    }

    // Клик = привязать/отвязать эту дорожку к первой камере проекта.
    function toggle() {
        if (btn.busy || !btn.trackId || !btn.projDir) return
        btn.busy = true
        http("GET", "/api/cam?dir=" + encodeURIComponent(btn.projDir), null, function (s) {
            if (!s || !(s.cameras || []).length) { btn.busy = false; btn.pluginDbg = "камеры нет"; return }
            var tok = s.cameras[0].tok
            var want = btn.bound ? null : 0.5
            http("POST", "/api/cam/bind",
                 { dir: btn.projDir, tok: tok, track: btn.trackId, depth: want },
                 function (r) { btn.busy = false; btn.bound = !btn.bound; btn.refresh() })
        })
    }

    Component.onCompleted: refresh()

    QUIButton {
        anchors.centerIn: parent
        radius: 8
        iconWidth: 16
        iconHeight: 16
        toolTips: btn.bound ? "Убрать дорожку из 3D-сцены" : "Добавить дорожку в 3D-сцену"
        defaultIconSource: "file://@UD@/plugin_assets/" +
                           (btn.bound ? "swirl-on.svg" : "swirl-off.svg")
        defaultColor: LVColor.Clear
        hoveredMaskColor: LVColor.White0A
        activedMaskColor: LVColor.Black0
        onClicked: btn.toggle()
    }
}
