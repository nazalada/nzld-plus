// NZLD+ — вкладка плагина в левой верхней панели CapCut.
// ПРИНЦИП: панель = ТОЛЬКО настройки. Таймлайн и превью НЕ дублируем — они уже есть на экране.
// Клипы выбираются в РОДНОМ таймлайне (C++ шлёт сюда selSegId), кадр смотрится в РОДНОМ плеере.
//
// ВЁРСТКА: только РОДНЫЕ компоненты CapCut, ничего своего не рисуем.
//   ParamSettingComboBox — выбор инструмента     PanelSettingsGroup — секция инспектора (свернуть/сброс)
//   VESpinBoxSlider — ползунок + числовое поле   TableRowItemS — строка «подпись + контрол»
//   VECheckBox / PrimaryButton / SecondayButton / TertiaryButton
//   LVColor / LVFont — палитра и шрифты (никаких hex и pixelSize)
//
// СОСТОЯНИЕ контролов живёт в свойствах pane, а не в id контролов: секции могут грузиться
// лениво, и снаружи их id не видны; плюс из свойств тривиально сохраняются настройки (/api/prefs).
import QtQuick
import GeneralBiz
import FusionUI
import MaterialPanel
import VECreator
import GeneralBizUI
import FunctionPanel

// Кнопка вкладки — ИХ ЖЕ тип: совпадает с родными по построению, а не «нарисована похоже».
NewTabItem {
    id: plug

    readonly property string assets: "file://@UD@/plugin_assets/"
    // ТОЛЬКО 127.0.0.1, НЕ "localhost": app.py слушает лишь IPv4 (ThreadingHTTPServer("127.0.0.1")),
    // а Qt резолвит localhost в IPv6 ::1 -> connection refused, МОЛЧА (ошибки QML CapCut глотает).
    readonly property string api: "http://127.0.0.1:8756"

    title: "NZLD+"
    icon: plug.assets + "nzld.svg"
    selectedIcon: plug.assets + "nzld-selected.svg"
    isSelected: plug.mine

    property bool mine: false
    property string pluginDbg: "(init)"
    // var, а не Item: обращаемся к динамическим свойствам панели (panel.src/panel.flw) —
    // на типизированном Item такой доступ не резолвится.
    property var panel: null

    // Рамку в плеере показываем ТОЛЬКО когда открыта наша вкладка и заданы источник + follower.
    // В СЛЕЖКЕ follower'а нет по определению, поэтому там условие только на источник — иначе
    // рамку нечем нарисовать и режим не запустить вовсе.
    property string roiWanted: (plug.mine && plug.panel && plug.panel.src
                                && ((plug.panel.tool === "track" && plug.panel.flw.length > 0)
                                    || plug.panel.tool === "reframe")) ? "1" : "0"

    // Живое выделение CapCut: C++ кладёт сюда id через QObject::setProperty на каждую смену.
    property string selSegId: ""
    // Рамка ROI живёт в плеере (родная RectangleSelectorBase), C++ пробрасывает её сюда
    // строкой: доли канваса [fx,fy,fw,fh] 0..1.
    property string roiFrac: ""
    // Плейхед из РОДНОГО таймлайна (мкс), считается в ve_tlprobe.qml.
    property string playheadUs: "0"

    MouseArea {
        anchors.fill: parent
        onClicked: {
            plug.mine = true
            // Гасим родной инспектор ИХ ЖЕ штатным состоянием — ровно это делает их reset().
            try { root.viewmodel.index = MainTabEnum.NoFound } catch (e) {}
        }
    }

    Connections {
        target: root
        function onCurrentIndexChanged() {
            if (root.currentIndex !== MainTabEnum.NoFound)
                plug.mine = false
        }
    }

    // Выделили сегмент, которого нет в карте (клип добавили ПОСЛЕ создания панели) →
    // перечитать /api/timeline. Перечитываем только когда id сменился И не резолвится.
    onSelSegIdChanged: {
        var n = -1
        try { n = Object.keys(plug.panel.segById).length } catch (e) {}
        plug.pluginDbg = "PUSH selSegId=[" + plug.selSegId + "] segById.count=" + n
        if (plug.panel && plug.selSegId && !plug.panel.segById[plug.selSegId])
            plug.panel.loadOpenProject()
        // «НИТКА»: панель ждёт клика по клипу в РОДНОМ таймлайне. Жест развёрнут (из панели к клипу)
        // сознательно: шапка дорожки id дорожки не отдаёт, а клик по клипу приходит по уже
        // проверенному каналу selSegId, и дорожку сервер резолвит по сегменту.
        try {
            if (plug.panel && plug.selSegId && plug.panel.armBind)
                plug.panel.catchBind(plug.selSegId)
        } catch (e) {}
    }

    Component.onCompleted: {
        // Цвета кнопки вкладки — с ЖИВОГО соседа, а не угаданные константы.
        try {
            var t = tabBarObj.get(0)
            plug.normalColor = t.normalColor
            plug.selectedColor = t.selectedColor
        } catch (e) {}
        // Горячая перезагрузка: сносим панель прошлой загрузки (C++ снимает только кнопку).
        try {
            for (var i = content.children.length - 1; i >= 0; i--)
                if (content.children[i].objectName === "VEPluginPanel")
                    content.children[i].destroy()
        } catch (e) {}
        try {
            plug.panel = panelComp.createObject(content)
            try {
                plug.panel.loadOpenProject()
                plug.pluginDbg = "load вызван OK, panel=" + plug.panel
            } catch (e) {
                plug.pluginDbg = "load ERR: " + e
            }
        } catch (e) {
            plug.pluginDbg = "panel ERR: " + e
        }
    }

    Component {
        id: panelComp

        Rectangle {
            id: pane
            objectName: "VEPluginPanel"
            anchors.fill: parent
            visible: plug.mine
            color: LVColor.Fill_Background_Level3

            // ---- состояние проекта ----
            property var segById: ({})            // id -> сегмент (имена/вид из /api/timeline, но НЕ рисуем таймлайн)
            property string projDir: ""
            property var src: null                // источник (видео)
            property var flw: []                  // follower-слои
            property string statusText: ""
            property bool statusErr: false
            property string logText: ""
            property bool busy: false
            property real progPct: 0
            property string progMsg: ""
            property bool progOn: false

            // ---- НАСТРОЙКИ (переживают перезапуск, /api/prefs) ----
            property string tool: "track"          // какой инструмент открыт: track | reframe | cam
            property real optPrecision: 3          // «Точность» трекинга 1..5 (бывш. «жёсткость»)
            // СЛЕЖКА ЗА ОБЪЕКТОМ — отдельный ИНСТРУМЕНТ, а не галка к трекингу: ключи ложатся на
            // САМ видеоклип, чтобы объект остался там, где он был на опорном кадре. Follower не
            // нужен, планарный и 3D не при чём, движок только ML.
            readonly property bool optReframe: pane.tool === "reframe"
            property bool optZoom: true            // авто-зум слежки: сдвиг не открывает чёрные края
            // Живого режима как настройки БОЛЬШЕ НЕТ: живой путь стал нормой, переключатель мог
            // только вернуть переоткрытия. Кил-свитч для разработки остался файлом ve_no_livecmd.txt.
            property bool optPos: true             // что писать: позиция
            property bool optScale: true           // ...масштаб
            property bool optRot: true             // ...поворот
            // Планарный режим: вместо трансформы слоя гоняем ЧЕТЫРЕ УГЛА (corner_pin).
            // Нужен, когда вставку клеим на плоскость (экран, вывеска) — similarity там «плывёт».
            property bool optPlanar: false
            // 3D-ТРЕК: движение берётся из РЕШЕНИЯ КАМЕРЫ по всей сцене, а поверхность — плоскость
            // из облака точек. Нужен там, где на самой цели фактуры нет (зелёный экран): планарному
            // треку не за что цепляться, а комната текстурой богата.
            property bool opt3d: false
            // ПРИВЯЗКА ОБЪЕКТА в репере плоскости, всё в ДОЛЯХ КВАДА: смещение x/y вдоль плоскости,
            // ВЫСОТА z по нормали (z>0 — над плоскостью, в сторону зрителя), размер w/h.
            // (0,0,0,1,1) = сам обведённый квад. Именно z даёт «текст висит НАД коробкой».
            property real obj3dX: 0.0
            property real obj3dY: 0.0
            property real obj3dZ: 0.0
            property real obj3dW: 1.0
            property real obj3dH: 0.0
            // ЖИВОЙ ПРЕДПРОСМОТР: решение камеры считается ОДИН раз («Подготовить»), дальше
            // крутилки пересчитывают углы на текущем кадре за миллисекунды, и слой рисует САМ
            // CapCut — своим шрифтом и стилями, уже искажённым. Крутить вслепую больше не надо.
            property string prep3dKey: ""
            property bool prep3dBusy: false
            // ВЫБОР ПОВЕРХНОСТИ: 4 ручки в плеере (их рисует дилиб поверх превью). surfQuad —
            // доли канваса [[x,y]*4] в порядке UL,UR,DL,DR; null = поверхность ещё не выбрана и
            // работает прежняя прямоугольная рамка.
            property bool surfOn: false
            property var surfQuad: null
            property bool hintsSeen: false         // подсказки-инструкции показываем только в первый раз
            property bool prefsReady: false        // до загрузки настроек НЕ сохраняем (иначе затрём дефолтами)

            // ---- СУЩНОСТЬ «3D-КАМЕРА» ----
            // Камера владеет ПРИВЯЗАННЫМИ ДОРОЖКАМИ. Привязка явная и потрековая; при создании
            // камера забирает все дорожки под собой, глубина по порядку (выше = ближе).
            property var camTracks: []             // дорожки проекта СНИЗУ ВВЕРХ (как в драфте)
            // В таймлайне верхняя дорожка нарисована СВЕРХУ, а в драфте она последняя.
            readonly property var camTracksTop: camTracks.slice().reverse()
            property var cams: []                  // камеры проекта: {tok, label, binds:{trackId:depth}, dof}
            property int camIdx: 0                 // активная камера
            readonly property var cam: camIdx >= 0 && camIdx < cams.length ? cams[camIdx] : null
            // null = DoF выключен (сервер не считал готовность); иначе {ctl, pairs, ready}
            readonly property var dofReady: cam && cam.dofReady !== undefined ? cam.dofReady : null
            property var binds: ({})               // {trackId: глубина} активной камеры — рабочая копия для UI
            // Наклон плоскости дорожки в градусах: {trackId: [вокруг горизонтали, вокруг вертикали]}.
            // Свойство ПОТРЕКОВОЕ, как и глубина, поэтому и живёт на той же пластине дорожки.
            property var tilts: ({})
            property string camBusy: ""            // текст операции, пока идёт создание/удаление
            property bool armBind: false           // «нитка» взведена: ждём клик по клипу в родном таймлайне
            // Сцены, у которых привязки живы, а клипа камеры на таймлайне уже нет (удалили Backspace,
            // откатили, скопировали проект). Раньше это была ТИШИНА: параллакс просто пропадал.
            property var lost: []
            property bool dofConfirm: false        // спросили про заворот в сборные клипы, ждём ответ
            readonly property real panGain: cam && cam.pan ? (cam.pan.gain || 0) : 0

            // Сегмент, выделенный в РОДНОМ таймлайне прямо сейчас.
            readonly property var selSeg: segById[plug.selSegId] !== undefined ? segById[plug.selSegId] : null

            // Регион для трека: либо родная рамка, либо выбранная по углам поверхность.
            // 3D-треку рамка не годится в принципе: ему нужна ИМЕННО плоскость, заданная углами.
            // Поэтому в 3D готовность = выбранная поверхность, а не «хоть какая-то область».
            // Слежке нужна ИМЕННО рамка вокруг объекта: углы и планарность к ней отношения не имеют,
            // поэтому её готовность считается отдельно, не глядя на optPlanar/opt3d.
            readonly property bool haveRegion: pane.optReframe
                ? pane.roi !== null
                : (pane.opt3d
                   ? pane.surfQuad !== null
                   : (pane.roi !== null || (pane.optPlanar && pane.surfQuad !== null)))
            function quadPx() {   // доли канваса -> px канваса; null = поверхность не выбрана
                if (pane.optReframe) return null
                if (!(pane.optPlanar || pane.opt3d) || !pane.surfQuad || !pane.canvasW) return null
                var o = []
                for (var i = 0; i < pane.surfQuad.length; i++)
                    o.push([pane.surfQuad[i][0] * pane.canvasW, pane.surfQuad[i][1] * pane.canvasH])
                return o
            }

            function say(t, e) { statusText = t; statusErr = e === true }

            // ---- ЖИВОЙ ПРЕДПРОСМОТР 3D ----
            function prep3d() {
                if (!src || !flw.length || !pane.quadPx()) {
                    say("сначала источник, слой и плоскость", true); return
                }
                pane.prep3dBusy = true; progOn = true; progPct = 0
                progMsg = "Решение камеры…"; progTimer.start()
                say("считаю камеру, это один раз…")
                httpPost("/api/preview3d/prepare", {
                    "dir": pane.projDir, "vid": src.id, "target": flw[0].id,
                    "quad": pane.quadPx(), "start": Math.round(pane.startUs)
                }, function (r) {
                    progTimer.stop(); pane.prep3dBusy = false; progPct = 100; hideProg.start()
                    pane.prep3dKey = r.key
                    say("предпросмотр живой — крути параметры")
                    logText = "кадров решено: " + (r.registered || 0) +
                              " · точек на плоскости: " + (r.plane_inliers || 0)
                    pane.push3d()
                }, function (e) {
                    progTimer.stop(); pane.prep3dBusy = false; progOn = false
                    say("ошибка", true); logText = String(e)
                })
            }
            // крутилку подвинули → пересчитать углы на ТЕКУЩЕМ кадре и отдать движку
            // Предпросмотр работает СРАЗУ, без всякого солва: пока объект лежит В ПЛОСКОСТИ,
            // это чистый варп по обведённой сетке. Решение камеры нужно только для ВЫСОТЫ и
            // для кадров, кроме опорного, — тогда шлём ещё и ключ подготовленного состояния.
            function push3d() {
                if (!pane.opt3d || !src || !flw.length || !pane.quadPx()) return
                httpPost("/api/preview3d/apply", {
                    "key": pane.prep3dKey,
                    "dir": pane.projDir, "vid": src.id, "target": flw[0].id,
                    "quad": pane.quadPx(),
                    "obj": { "x": pane.obj3dX, "y": pane.obj3dY, "z": pane.obj3dZ,
                             "w": pane.obj3dW, "h": pane.obj3dH },
                    "playhead": Math.round(pane.startUs)
                }, function (r) {
                    if (r.note) say(r.note)
                }, function (e) { say("предпросмотр: " + e, true) })
            }
            Timer {   // крутилка ездит непрерывно — шлём не на каждый пиксель, а по остановке
                id: prev3dTimer
                interval: 120; repeat: false
                onTriggered: pane.push3d()
            }

            // ---- ВЫБОР ПОВЕРХНОСТИ ПО УГЛАМ ----
            // Ручки живут в дилибе (Cocoa-оверлей поверх превью), панель их только включает и
            // забирает результат. Стартовый квад — прошлый выбор либо серединный прямоугольник.
            function startSurf(q) {
                httpPost("/api/surfpick", { "on": true, "quad": q }, function (r) {
                    pane.surfOn = true
                    if (r.quad) pane.surfQuad = r.quad
                    surfPoll.start()
                    say("тяни ручки по углам поверхности")
                }, function (e) { say("ошибка: " + e, true) })
            }
            function stopSurf() {
                surfPoll.stop()
                httpPost("/api/surfpick", { "on": false }, function () {
                    pane.surfOn = false
                    say(pane.surfQuad ? "поверхность выбрана" : "готово")
                }, function (e) { pane.surfOn = false; say("ошибка: " + e, true) })
            }
            function toggleSurf() { if (pane.surfOn) stopSurf(); else startSurf(pane.surfQuad) }

            Timer {   // пока тянут ручки — забираем квад, чтобы панель показывала актуальное
                id: surfPoll
                interval: 400; repeat: true; running: false
                onTriggered: pane.httpGet("/api/surfquad", function (r) { if (r.quad) pane.surfQuad = r.quad })
            }

            // ---- HTTP прямо из QML на существующий app.py: отдельный мост не нужен ----
            function httpGet(path, cb, errcb) {
                var x = new XMLHttpRequest()
                x.onreadystatechange = function () {
                    if (x.readyState !== XMLHttpRequest.DONE) return
                    if (x.status === 200) { try { cb(JSON.parse(x.responseText)) } catch (e) { if (errcb) errcb("разбор: " + e) } }
                    else if (errcb) errcb("HTTP " + x.status)
                }
                x.open("GET", plug.api + path); x.send()
            }
            function httpPost(path, obj, cb, errcb) {
                var x = new XMLHttpRequest()
                x.onreadystatechange = function () {
                    if (x.readyState !== XMLHttpRequest.DONE) return
                    var r = null
                    try { r = JSON.parse(x.responseText) } catch (e) {}
                    if (x.status === 200 && r && r.ok) cb(r)
                    else if (errcb) errcb(r && r.error ? r.error : "HTTP " + x.status)
                }
                x.open("POST", plug.api + path)
                x.setRequestHeader("Content-Type", "application/json")
                x.send(JSON.stringify(obj))
            }

            // ---- настройки ----
            function prefsLoad() {
                httpGet("/api/prefs", function (p) {
                    if (p.tool !== undefined) pane.tool = p.tool
                    if (p.precision !== undefined) pane.optPrecision = p.precision
                    // p.live НЕ читаем НАМЕРЕННО: в чужих prefs может лежать live:false с тех пор,
                    // когда галка была. Удалить контрол мало — значение подняло бы переоткрытия
                    // обратно, молча. Живой режим теперь константа true, prefs его не хранит.
                    if (p.zoom !== undefined) pane.optZoom = p.zoom
                    if (p.pos !== undefined) pane.optPos = p.pos
                    if (p.scale !== undefined) pane.optScale = p.scale
                    if (p.rot !== undefined) pane.optRot = p.rot
                    if (p.planar !== undefined) pane.optPlanar = p.planar
                    if (p.t3d !== undefined) pane.opt3d = p.t3d
                    if (p.objX !== undefined) pane.obj3dX = p.objX
                    if (p.objY !== undefined) pane.obj3dY = p.objY
                    if (p.objZ !== undefined) pane.obj3dZ = p.objZ
                    if (p.objW !== undefined) pane.obj3dW = p.objW
                    if (p.objH !== undefined) pane.obj3dH = p.objH
                    if (p.hintsSeen !== undefined) pane.hintsSeen = p.hintsSeen
                    pane.prefsReady = true
                }, function (e) { pane.prefsReady = true })
            }
            function prefsSave() {
                if (!pane.prefsReady) return   // не затираем сохранённое дефолтами до загрузки
                httpPost("/api/prefs", {
                    tool: pane.tool, precision: pane.optPrecision, zoom: pane.optZoom,
                    pos: pane.optPos, scale: pane.optScale, rot: pane.optRot, planar: pane.optPlanar,
                    t3d: pane.opt3d, objX: pane.obj3dX, objY: pane.obj3dY, objZ: pane.obj3dZ,
                    objW: pane.obj3dW, objH: pane.obj3dH,
                    hintsSeen: pane.hintsSeen
                }, function (r) {}, function (e) {})
            }
            onToolChanged: prefsSave()
            onOptPrecisionChanged: prefsSave()
            onOptZoomChanged: prefsSave()
            onOptPosChanged: prefsSave()
            onOptScaleChanged: prefsSave()
            onOptRotChanged: prefsSave()
            onOptPlanarChanged: prefsSave()
            onOpt3dChanged: prefsSave()
            onObj3dXChanged: { prefsSave(); prev3dTimer.restart() }
            onObj3dYChanged: { prefsSave(); prev3dTimer.restart() }
            onObj3dZChanged: { prefsSave(); prev3dTimer.restart() }
            onObj3dWChanged: { prefsSave(); prev3dTimer.restart() }
            onObj3dHChanged: { prefsSave(); prev3dTimer.restart() }
            onHintsSeenChanged: prefsSave()

            // Таймлайн ТЯНЕМ (нужны имена/вид сегментов), но НЕ РИСУЕМ — он уже есть на экране.
            function loadOpenProject() {
                httpGet("/api/projects", function (ps) {
                    var pick = null
                    for (var i = 0; i < ps.length; i++) if (ps[i].open) { pick = ps[i]; break }
                    if (!pick && ps.length) pick = ps[0]
                    if (!pick) { say("проектов не найдено", true); return }
                    pane.projDir = pick.dir
                    httpGet("/api/timeline?dir=" + encodeURIComponent(pick.dir), function (t) {
                        var m = ({})
                        for (var i = 0; i < t.tracks.length; i++)
                            for (var j = 0; j < t.tracks[i].segments.length; j++) {
                                var s = t.tracks[i].segments[j]
                                m[s.id] = s
                            }
                        pane.segById = m
                        pane.canvasW = t.canvas.w
                        pane.canvasH = t.canvas.h
                        say("")
                    }, function (e) { say("таймлайн: " + e, true) })
                    pane.loadCam()
                }, function (e) { say("сервер недоступен — запусти CapCut Probe.command", true) })
            }

            // ---- сущность камеры ----
            function loadCam() {
                if (!projDir) return
                httpGet("/api/cam?dir=" + encodeURIComponent(projDir), function (s) {
                    pane.camTracks = s.tracks || []
                    pane.cams = s.cameras || []
                    pane.lost = s.lost || []
                    if (pane.camIdx >= pane.cams.length) pane.camIdx = Math.max(0, pane.cams.length - 1)
                    pane.binds = pane.cam ? (pane.cam.binds || {}) : ({})
                    pane.tilts = pane.cam ? (pane.cam.tilts || {}) : ({})
                }, function (e) { say("камера: " + e, true) })
            }
            // Глубина читается ЦВЕТОМ ползунка: ближний — фирменная бирюза, дальний уходит
            // в приглушённый сине-серый. Цвет ФУНКЦИОНАЛЕН (кодирует глубину), поэтому считается,
            // а не берётся токеном; отдаём его в progressColor РОДНОГО ползунка.
            function depthColor(d) {
                var k = Math.max(0, Math.min(1, d))
                return Qt.rgba(0.0 + 0.34 * k, 0.757 - 0.29 * k, 0.804 - 0.24 * k, 1)
            }
            // Цвет линии на графике кривой. Только родные токены — своих оттенков не заводим.
            function curveColor(k) {
                return k === "px" ? LVColor.Brand_Primary_Default
                     : k === "py" ? LVColor.State_Warning_Default
                     : LVColor.Text_Primary
            }
            function setPan(gain) {
                if (!cam) return
                httpPost("/api/cam/pan", { dir: projDir, tok: cam.tok, gain: gain },
                         function (r) { pane.loadCam() },
                         function (e) { say("поворот камеры: " + e, true) })
            }
            function depthOf(trackId) {   // глубина дорожки в активной камере (-1 = не привязана)
                var d = pane.binds[trackId]
                return d === undefined ? -1 : d
            }
            // Привязка/глубина идут ЖИВЬЁМ: сервер переписывает ve_rig.txt, дилиб перечитывает по mtime
            // и сам пересчитывает параллакс. Проект НЕ закрывается.
            // binds заменяем ЦЕЛИКОМ — иначе QML не заметит изменения var-свойства.
            function bindTrack(trackId, depth) {
                if (!cam) return
                var b = {}
                for (var k in pane.binds) b[k] = pane.binds[k]
                if (depth === null) delete b[trackId]
                else b[trackId] = depth
                pane.binds = b
                httpPost("/api/cam/bind", { dir: projDir, tok: cam.tok, track: trackId, depth: depth },
                         function (r) { say(depth === null ? "дорожка отвязана — ключи сняты" : "") },
                         function (e) { say("привязка: " + e, true); pane.loadCam() })
            }
            function tiltOf(trackId) {   // [вокруг горизонтали, вокруг вертикали] в градусах
                var t = pane.tilts[trackId]
                return (t && t.length === 2) ? t : [0, 0]
            }
            // Наклон едет тем же живым путём, что и глубина: сервер правит сайдкар, пересобирает
            // ve_rig.txt, дилиб перечитывает его по mtime и сам гонит углы. Проект не закрывается.
            // ⚠️ Ноль — это ЗНАЧЕНИЕ, а не «поля нет»: бэкенд по нулю выкидывает запись из сайдкара,
            // чтобы не копить мусор, поэтому сброс обязан ПОСЛАТЬ ноль, а не пропустить параметр.
            function setTilt(trackId, tx, ty) {
                if (!cam) return
                var t = {}
                for (var k in pane.tilts) t[k] = pane.tilts[k]
                if (tx === 0 && ty === 0) delete t[trackId]   // так же, как решает сервер
                else t[trackId] = [tx, ty]
                pane.tilts = t   // целиком, иначе QML не заметит изменения var-свойства
                httpPost("/api/cam/tilt",
                         { dir: projDir, tok: cam.tok, track: trackId, tiltx: tx, tilty: ty },
                         function (r) {}, function (e) { say("наклон: " + e, true); pane.loadCam() })
            }
            // Клик по клипу пришёл, пока нитка взведена → привязываем ЕГО ДОРОЖКУ.
            function catchBind(segId) {
                if (!cam) { armBind = false; return }
                armBind = false
                httpPost("/api/cam/bind", { dir: projDir, tok: cam.tok, seg: segId, depth: 0.5 },
                         function (r) {
                             pane.binds = r.tracks || {}
                             say("дорожка привязана — тяни глубину")
                         },
                         function (e) { say("привязка: " + e, true) })
            }
            function createCam() {
                if (pane.camBusy) return
                pane.camBusy = "Создаю камеру…"; say("")
                httpPost("/api/cam/create", { dir: projDir }, function (r) {
                    pane.camBusy = ""
                    say("камера создана — привязано дорожек " + Object.keys(r.tracks || {}).length)
                    pane.loadCam()
                }, function (e) { pane.camBusy = ""; say("камера: " + e, true) })
            }
            function prepDof(focus, aperture) {
                if (!cam || pane.camBusy) return
                pane.camBusy = "Готовлю глубину резкости…"; say("")
                httpPost("/api/cam/dofprep", { dir: projDir, tok: cam.tok, focus: focus, aperture: aperture },
                         function (r) {
                             pane.camBusy = ""
                             say(r.ready ? "глубина резкости готова — кейфрей масштаб клипа «DoF контроллер»"
                                         : "подготовил, но размывать нечего: у объектов нет сборных клипов с размытием", !r.ready)
                             pane.loadCam()
                         }, function (e) { pane.camBusy = ""; say("глубина резкости: " + e, true) })
            }
            function restoreCam(tok) {
                if (pane.camBusy) return
                pane.camBusy = "Возвращаю камеру…"; say("")
                httpPost("/api/cam/restore", { dir: projDir, tok: tok }, function (r) {
                    pane.camBusy = ""; say("камера возвращена — привязки на месте")
                    pane.loadCam()
                }, function (e) { pane.camBusy = ""; say("восстановление: " + e, true) })
            }
            function deleteCam() {
                if (!cam || pane.camBusy) return
                pane.camBusy = "Удаляю камеру…"
                httpPost("/api/cam/delete", { dir: projDir, tok: cam.tok }, function (r) {
                    pane.camBusy = ""; say("камера удалена, ключи сняты")
                    pane.camIdx = 0; pane.loadCam()
                }, function (e) { pane.camBusy = ""; say("удаление: " + e, true) })
            }
            function setKeepin(on) {
                if (!cam) return
                httpPost("/api/cam/keepin", { dir: projDir, tok: cam.tok, on: on },
                         function (r) {}, function (e) { say("рамка: " + e, true) })
            }
            function setCamDof(on, focus, aperture) {
                if (!cam) return
                httpPost("/api/cam/dof", { dir: projDir, tok: cam.tok, on: on, focus: focus, aperture: aperture },
                         function (r) {}, function (e) { say("DoF: " + e, true) })
            }

            function setSource() {
                if (!selSeg) { say("выдели клип в таймлайне CapCut", true); return }
                if (!(selSeg.is_video && selSeg.kind === "videos")) { say("источник должен быть ВИДЕО", true); return }
                src = selSeg; say("")
            }
            function addFollower() {
                if (!selSeg) { say("выдели слой в таймлайне CapCut", true); return }
                if (src && selSeg.id === src.id) { say("это источник, не follower", true); return }
                var f = flw.slice()
                for (var i = 0; i < f.length; i++)
                    if (f[i].id === selSeg.id) { f.splice(i, 1); flw = f; return }   // повторное нажатие снимает
                f.push(selSeg); flw = f; say("")
            }
            function flwNames() {
                if (!flw.length) return ""
                var n = []
                for (var i = 0; i < flw.length; i++) n.push(flw[i].name)
                return n.join(", ")
            }

            // Перед долгим треком спрашиваем про перезапись: ключи уже могли быть наложены,
            // и молча их затирать нельзя (спека C16).
            property bool askOverwrite: false
            // Кому ложатся ключи: в трекинге — follower'ам, в СЛЕЖКЕ — самому видеоклипу.
            // Один список на всех, чтобы «есть ли ключи» и «снять ключи» не разъехались с записью.
            function keyTargets() {
                if (pane.optReframe) return pane.src ? [pane.src.id] : []
                var ids = []
                for (var i = 0; i < pane.flw.length; i++) ids.push(pane.flw[i].id)
                return ids
            }
            function startTrack() {
                if (!src || busy) return
                var ids = keyTargets()
                httpGet("/api/trackkeys?dir=" + encodeURIComponent(projDir) + "&ids=" + ids.join(","),
                        function (r) {
                            if (r && r.keys > 0) pane.askOverwrite = true
                            else pane.runTrack()
                        },
                        function (e) { pane.runTrack() })   // не смогли проверить — не блокируем работу
            }
            // Снять всё, что наложил трекинг (позиция/масштаб/поворот/углы). Необратимо → подтверждение.
            property bool askClear: false
            function clearTrack() {
                if (busy) return
                var ids = keyTargets()
                if (!ids.length) { say(pane.optReframe ? "источник не задан" : "нет follower-слоёв", true); return }
                busy = true; say("снимаю ключи…")
                // reopen: false — жёстко. Раньше здесь стояло !optLive, и с выключенным живым
                // режимом снятие тянуло за собой переоткрытие проекта.
                httpPost("/api/cleartrack", { dir: projDir, targets: ids, reopen: false },
                         function (r) { busy = false; say("снято ключей: " + (r.removed || 0)) },
                         function (e) { busy = false; say("снятие: " + e, true) })
            }

            // Движок трекинга — ТОЛЬКО ML (CSRT спрятан: по замерам он заметно слабее).
            function runTrack() {
                if (!src || busy) return
                // В СЛЕЖКЕ набор свойств не выбирается: позиция — это и есть сама компенсация,
                // масштаб — авто-зум. Поворот бэкенд там не считает (rs=None), слать его нечего.
                var props = []
                if (pane.optReframe) {
                    props.push("pos")
                    if (pane.optZoom) props.push("scale")
                } else {
                    if (pane.optPos) props.push("pos")
                    if (pane.optScale) props.push("scale")
                    if (pane.optRot) props.push("rot")
                }
                var ids = keyTargets()
                busy = true; progOn = true; progPct = 0; progMsg = "Трекинг…"; logText = ""
                pane.hintsSeen = true   // первый запуск прошёл — подсказки больше не нужны
                say("трекинг идёт…"); progTimer.start()
                // 3D-ТРЕК идёт на свою ручку: там решение камеры, плоскость из облака и объект
                // в её репере. Тест параллакса стоит на сервере ПЕРЕД тяжёлым солвом, поэтому
                // клип без смещения камеры получит отказ за секунды, а не через сорок.
                if (pane.opt3d && !pane.optReframe) {
                    httpPost("/api/track3d", {
                        "dir": pane.projDir, "vid": src.id, "targets": ids,
                        "start": Math.round(pane.startUs), "live": true,
                        "quad": pane.quadPx(),
                        "obj": { "x": pane.obj3dX, "y": pane.obj3dY, "z": pane.obj3dZ,
                                 "w": pane.obj3dW, "h": pane.obj3dH }
                    }, function (r) {
                        progTimer.stop(); busy = false; progPct = 100; progMsg = "Готово"; hideProg.start()
                        say("готово")
                        logText = "слоёв: " + (r.layers || 0) + " · ключей: " + (r.keyframes || 0) +
                                  " · кадров решено: " + (r.registered || 0) +
                                  " · доводка: " + (r.refined || 0) + "/" +
                                  ((r.refined || 0) + (r.refused || 0))
                    }, function (e) {
                        progTimer.stop(); busy = false; progOn = false
                        say("ошибка", true); logText = String(e)
                    })
                    return
                }
                // СЛЕЖКА: follower'ов нет, ключи ложатся на сам видеоклип — targets уходит пустым,
                // планарность и углы к этому режиму отношения не имеют.
                httpPost("/api/track", {
                    "dir": pane.projDir, "vid": src.id, "targets": pane.optReframe ? [] : ids,
                    "roi": pane.roi, "start": Math.round(pane.startUs),
                    "engine": "ml", "props": props,
                    "level": Math.round(pane.optPrecision), "live": true,
                    "reframe": pane.optReframe,
                    "planar": pane.optPlanar && !pane.optReframe, "quad": pane.quadPx()
                }, function (r) {
                    progTimer.stop(); busy = false; progPct = 100; progMsg = "Готово"; hideProg.start()
                    say("готово")
                    logText = (pane.optReframe ? "ключей: " + (r.keyframes || 0)
                                               : "слоёв: " + (r.layers || 0) + " · ключей: " + (r.keyframes || 0)) +
                              " · покрытие " + Math.round((r.covered || 0) * 100) + "%"
                }, function (e) {
                    progTimer.stop(); busy = false; progOn = false
                    say("ошибка", true); logText = String(e)
                })
            }

            // Рамка — родная RectangleSelectorBase в плеере; сюда приезжает долями канваса.
            // Бэкенд (run_track) ждёт ROI в ПИКСЕЛЯХ КАНВАСА.
            property var roi: {
                if (!plug.roiFrac || !pane.canvasW) return null
                try {
                    var f = JSON.parse(plug.roiFrac)
                    return [f[0] * pane.canvasW, f[1] * pane.canvasH,
                            f[2] * pane.canvasW, f[3] * pane.canvasH]
                } catch (e) { return null }
            }
            property real canvasW: 0
            property real canvasH: 0
            // Кадр старта = плейхед РОДНОГО таймлайна. Своего таймлайна у нас нет и не надо.
            property real startUs: parseFloat(plug.playheadUs) || 0
            onStartUsChanged: if (pane.prep3dKey) prev3dTimer.restart()
            function fmtT(us) {
                var s = us / 1e6, m = Math.floor(s / 60), r = s - m * 60
                return m + ":" + (r < 10 ? "0" : "") + r.toFixed(2)
            }

            Timer {
                id: progTimer
                interval: 400; repeat: true
                onTriggered: pane.httpGet("/api/progress", function (p) {
                    if (!pane.busy) return
                    pane.progPct = p.pct
                    if (p.msg) pane.progMsg = p.msg
                })
            }
            Timer { id: hideProg; interval: 1200; onTriggered: pane.progOn = false }
            // Привязки можно менять ДВУМЯ путями: здесь и спиралью в шапке дорожки. Без опроса
            // панель показывала бы состояние на момент открытия (проверено: в таймлайне две
            // привязки, в панели одна). Опрашиваем, только когда вкладка камеры реально видна.
            Timer {
                interval: 2000; repeat: true
                running: plug.mine && pane.tool === "cam"
                onTriggered: if (!pane.camBusy) pane.loadCam()
            }
            Component.onCompleted: { prefsLoad(); loadOpenProject() }

            // ================= шапка: выбор инструмента =================
            ParamSettingComboBox {
                id: toolBox
                anchors { left: parent.left; right: parent.right; top: parent.top
                          leftMargin: 16; rightMargin: 16; topMargin: 12 }
                height: 24
                text: "Инструмент"
                // «Слежка за объектом» стоит именно ЗДЕСЬ, а не галкой в трекере: у неё другой
                // смысл (двигаем сам кадр, а не слой) и другой набор входов — follower не нужен.
                itemList: ["Трекер", "Слежка за объектом", "3D камера"]
                selectedIndex: pane.tool === "cam" ? 2 : (pane.tool === "reframe" ? 1 : 0)
                onSelectedIndexChanged: {
                    pane.tool = selectedIndex === 2 ? "cam" : (selectedIndex === 1 ? "reframe" : "track")
                    if (pane.tool === "cam") pane.loadCam()
                }
            }
            Text {
                id: statusLine
                anchors { left: parent.left; right: parent.right; top: toolBox.bottom
                          leftMargin: 16; rightMargin: 16; topMargin: 6 }
                elide: Text.ElideRight
                visible: pane.statusText !== ""
                text: pane.statusText
                color: pane.statusErr ? LVColor.State_Warning_Default : LVColor.Text_Tertiary
                font: LVFont.SubtitleRegular11
            }
            Rectangle {
                id: hline
                anchors { left: parent.left; right: parent.right; top: statusLine.visible ? statusLine.bottom : toolBox.bottom; topMargin: 10 }
                height: 1; color: LVColor.Line_Level2
            }

            // ================= ТРЕКЕР и СЛЕЖКА =================
            // Одна колонка на два инструмента: вход у них общий (видео + рамка), расходятся
            // только follower и набор свойств — их и прячем по pane.optReframe.
            Flickable {
                visible: pane.tool !== "cam"
                anchors { left: parent.left; right: parent.right; top: hline.bottom; bottom: parent.bottom
                          leftMargin: 16; rightMargin: 16; topMargin: 12; bottomMargin: 12 }
                contentHeight: col.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: col
                    width: parent.width
                    spacing: 10

                    // ---- Выбор ----
                    PanelSettingsGroup {
                        id: gSel
                        width: parent.width
                        height: totalHeight
                        title: "Выбор"
                        useCheck: false; useKeyFrame: false; useReset: false
                        collapsible: true
                        dataHeight: selBox.height
                        Column {
                            id: selBox
                            y: gSel.dataY
                            width: parent.width
                            spacing: 8
                            TableRowItemS {
                                width: parent.width
                                text: "Выделено"
                                contentItem: Text {
                                    height: 18
                                    verticalAlignment: Text.AlignVCenter
                                    elide: Text.ElideRight
                                    text: pane.selSeg ? pane.selSeg.name : "— выдели клип в CapCut"
                                    color: pane.selSeg ? LVColor.Brand_Primary_Default : LVColor.Text_Tertiary
                                    font: LVFont.SubtitleRegular11
                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                // В слежке кнопка одна и занимает всю ширину: follower'а нет.
                                property real bw: pane.optReframe ? width : (width - spacing) / 2
                                SecondayButton {
                                    width: parent.bw; height: 28
                                    text: pane.optReframe ? "Видео для слежки" : "Источник"
                                    enabled: pane.selSeg !== null
                                    onClicked: pane.setSource()
                                }
                                SecondayButton {
                                    width: parent.bw; height: 28
                                    visible: !pane.optReframe
                                    text: "Follower"
                                    enabled: pane.selSeg !== null && pane.src !== null
                                    onClicked: pane.addFollower()
                                }
                            }
                            TableRowItemS {
                                width: parent.width
                                text: pane.optReframe ? "Видео" : "Источник"
                                contentItem: Text {
                                    height: 18
                                    verticalAlignment: Text.AlignVCenter
                                    elide: Text.ElideRight
                                    text: pane.src ? pane.src.name : "не задан"
                                    color: pane.src ? LVColor.Text_Primary : LVColor.Text_Tertiary
                                    font: LVFont.SubtitleRegular11
                                }
                            }
                            TableRowItemS {
                                width: parent.width
                                visible: !pane.optReframe
                                text: "Follower"
                                contentItem: Text {
                                    height: 18
                                    verticalAlignment: Text.AlignVCenter
                                    elide: Text.ElideRight
                                    text: pane.flw.length ? pane.flwNames() : "не заданы"
                                    color: pane.flw.length ? LVColor.Brand_Primary_Default : LVColor.Text_Tertiary
                                    font: LVFont.SubtitleRegular11
                                }
                            }
                            TertiaryButton {
                                width: 120; height: 24
                                text: "Сбросить выбор"
                                onClicked: { pane.src = null; pane.flw = []; pane.say("") }
                            }
                        }
                    }

                    // ---- Параметры ----
                    PanelSettingsGroup {
                        id: gPar
                        width: parent.width
                        height: totalHeight
                        title: "Параметры"
                        useCheck: false; useKeyFrame: false; useReset: true
                        collapsible: true
                        dataHeight: parBox.height
                        onResetBtnClicked: { pane.optPrecision = 3; pane.optZoom = true }
                        Column {
                            id: parBox
                            y: gPar.dataY
                            width: parent.width
                            spacing: 8
                            TableRowItemS {
                                width: parent.width
                                text: "Точность"
                                contentItem: VESpinBoxSlider {
                                    height: 24
                                    minValue: 1; maxValue: 5; decimals: 0
                                    curValue: pane.optPrecision
                                    onValueChanged: function (v) { pane.optPrecision = v }
                                }
                            }
                            // Авто-зум — единственная настройка слежки: без него сдвиг клипа
                            // открывает чёрные поля по краям кадра.
                            VECheckBox {
                                width: parent.width; height: 22
                                visible: pane.optReframe
                                title: "Авто-зум (не открывать чёрные края)"
                                checkable: true
                                checked: pane.optZoom
                                onClicked: pane.optZoom = checked
                            }
                        }
                    }

                    // ---- Что писать ----
                    PanelSettingsGroup {
                        id: gWhat
                        width: parent.width
                        height: totalHeight
                        // Слежке выбирать нечего: позиция обязательна (она и есть компенсация),
                        // масштаб — авто-зум из «Параметров», планарность и 3D не при чём.
                        visible: !pane.optReframe
                        title: "Что писать"
                        useCheck: false; useKeyFrame: false; useReset: true
                        collapsible: true
                        dataHeight: whatBox.height
                        onResetBtnClicked: { pane.optPos = true; pane.optScale = true; pane.optRot = true
                                             pane.optPlanar = false; pane.opt3d = false
                                             pane.obj3dX = 0; pane.obj3dY = 0; pane.obj3dZ = 0
                                             pane.obj3dW = 1; pane.obj3dH = 0 }
                        Column {
                            id: whatBox
                            y: gWhat.dataY
                            width: parent.width
                            spacing: 8
                            // Планарный режим ЗАМЕНЯЕТ трансформу углами — держать оба набора
                            // одновременно бессмысленно, поэтому обычные галочки при нём гаснут.
                            VECheckBox {
                                width: parent.width; height: 22
                                title: "Планарный (углы плоскости)"
                                checkable: true; checked: pane.optPlanar
                                onClicked: { pane.optPlanar = checked; if (checked) pane.opt3d = false }
                            }
                            // 3D-ТРЕК. Тоже гонит углы, но берёт их из РЕШЕНИЯ КАМЕРЫ, поэтому
                            // фактура нужна не на цели, а в сцене. Держать вместе с планарным
                            // нечего: это два способа получить те же четыре угла.
                            VECheckBox {
                                width: parent.width; height: 22
                                title: "3D-трек (решение камеры)"
                                checkable: true; checked: pane.opt3d
                                onClicked: { pane.opt3d = checked; if (checked) pane.optPlanar = false }
                            }
                            // ПРИВЯЗКА ОБЪЕКТА в репере плоскости — то, ради чего затевался 3D:
                            // плоскость это система координат, а не рамка для натяжки. Всё в долях
                            // квада, поэтому 0/0/0 и 1×1 — это ровно обведённый четырёхугольник.
                            Column {
                                width: parent.width
                                spacing: 6
                                visible: pane.opt3d
                                height: visible ? implicitHeight : 0
                                PrimaryButton {
                                    width: parent.width; height: 26
                                    enabled: pane.surfQuad !== null && !pane.prep3dBusy && !pane.busy
                                    text: pane.prep3dKey ? "Пересчитать камеру"
                                                         : "Решить камеру для высоты (≈45 с)"
                                    onClicked: pane.prep3d()
                                }
                                Text {
                                    width: parent.width; wrapMode: Text.Wrap
                                    text: pane.prep3dKey
                                          ? "Камера решена: работает и высота, и любой кадр."
                                          : "Смещение и размер видны в плеере сразу. Для ВЫСОТЫ нужно решение камеры — эта кнопка."
                                    color: pane.prep3dKey ? LVColor.Brand_Primary_Default : LVColor.Text_Tertiary
                                    font: LVFont.OverlineRegular10
                                }
                                TableRowItemS {
                                    width: parent.width
                                    text: "Смещение X"
                                    contentItem: VESpinBoxSlider {
                                        height: 24
                                        minValue: -3; maxValue: 3; decimals: 2
                                        curValue: pane.obj3dX
                                        onValueChanged: function (v) { pane.obj3dX = v }
                                    }
                                }
                                TableRowItemS {
                                    width: parent.width
                                    text: "Смещение Y"
                                    contentItem: VESpinBoxSlider {
                                        height: 24
                                        minValue: -3; maxValue: 3; decimals: 2
                                        curValue: pane.obj3dY
                                        onValueChanged: function (v) { pane.obj3dY = v }
                                    }
                                }
                                TableRowItemS {
                                    width: parent.width
                                    text: "Высота над плоскостью"
                                    contentItem: VESpinBoxSlider {
                                        height: 24
                                        minValue: -2; maxValue: 2; decimals: 2
                                        curValue: pane.obj3dZ
                                        onValueChanged: function (v) { pane.obj3dZ = v }
                                    }
                                }
                                TableRowItemS {
                                    width: parent.width
                                    text: "Ширина"
                                    contentItem: VESpinBoxSlider {
                                        height: 24
                                        minValue: 0.05; maxValue: 4; decimals: 2
                                        curValue: pane.obj3dW
                                        onValueChanged: function (v) { pane.obj3dW = v }
                                    }
                                }
                                TableRowItemS {
                                    width: parent.width
                                    // 0 = авто: высота считается из пропорций коробки слоя, иначе
                                    // объект и слой расходятся в пропорциях и картинку плющит.
                                    text: "Высота (0 — по слою)"
                                    contentItem: VESpinBoxSlider {
                                        height: 24
                                        minValue: 0; maxValue: 4; decimals: 2
                                        curValue: pane.obj3dH
                                        onValueChanged: function (v) { pane.obj3dH = v }
                                    }
                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 6
                                height: 22
                                enabled: !pane.optPlanar && !pane.opt3d
                                opacity: enabled ? 1.0 : 0.4
                                property real cw: (width - 2 * spacing) / 3
                                VECheckBox {
                                    width: parent.cw; height: 22; title: "позиция"
                                    checkable: true; checked: pane.optPos
                                    onClicked: pane.optPos = checked
                                }
                                VECheckBox {
                                    width: parent.cw; height: 22; title: "масштаб"
                                    checkable: true; checked: pane.optScale
                                    onClicked: pane.optScale = checked
                                }
                                VECheckBox {
                                    width: parent.cw; height: 22; title: "поворот"
                                    checkable: true; checked: pane.optRot
                                    onClicked: pane.optRot = checked
                                }
                            }
                            // ВЫБОР ПОВЕРХНОСТИ ПО УГЛАМ. Прямоугольная рамка не умеет обвести
                            // наклонённый экран: в неё всегда попадают рука и фон, а вставку потом
                            // некуда класть точно. Здесь пользователь тянет четыре ручки прямо в
                            // плеере по углам экрана — они же задают, куда ляжет вставка.
                            Row {
                                width: parent.width
                                spacing: 6
                                height: 26
                                // 3D-режиму эти же ручки нужны не меньше: ИМИ И ВЫБИРАЕТСЯ, какую
                                // плоскость трекать. Пока тут стояло одно optPlanar, в 3D кнопка
                                // пропадала — выбрать плоскость было нечем, и на сервер уходил
                                // пустой квад.
                                visible: pane.optPlanar || pane.opt3d
                                PrimaryButton {
                                    width: (parent.width - parent.spacing) / 2; height: 26
                                    text: pane.surfOn ? "Готово" : "Выбрать поверхность"
                                    onClicked: pane.toggleSurf()
                                }
                                PrimaryButton {
                                    width: (parent.width - parent.spacing) / 2; height: 26
                                    text: "Сбросить углы"
                                    enabled: pane.surfOn
                                    onClicked: { pane.surfQuad = null; pane.startSurf(null) }
                                }
                            }
                            Text {
                                width: parent.width; wrapMode: Text.Wrap
                                visible: pane.optPlanar || pane.opt3d
                                text: pane.surfOn
                                      ? (pane.opt3d
                                         ? "Тяни четыре ручки по краям ПЛОСКОСТИ, которую трекаем: верх коробки, экран, стол. Она станет системой координат для объекта."
                                         : "Тяни четыре ручки в плеере по углам экрана. Вставка ляжет ровно в них.")
                                      : (pane.surfQuad
                                         ? (pane.opt3d ? "Плоскость выбрана. Объект ставится от неё полями ниже."
                                                       : "Поверхность выбрана по углам — вставка ляжет ровно в них.")
                                         : (pane.opt3d ? "Сначала выбери плоскость — четыре угла в плеере. Без неё 3D-трек не запустится."
                                                       : "Вставка ляжет на плоскость по четырём углам — для экранов и вывесок."))
                                color: pane.surfQuad ? LVColor.Brand_Primary_Default : LVColor.Text_Tertiary
                                font: LVFont.OverlineRegular10
                            }
                        }
                    }

                    // ---- состояние готовности: подсказка только пока не освоился ----
                    Text {
                        width: parent.width; wrapMode: Text.Wrap
                        topPadding: 4
                        visible: !pane.hintsSeen || !pane.haveRegion
                        color: pane.haveRegion && plug.roiWanted === "1" ? LVColor.Brand_Primary_Default : LVColor.Text_Tertiary
                        font: LVFont.SubtitleRegular11
                        text: {
                            if (pane.optReframe) {
                                if (!pane.src) return "Выдели видео в таймлайне и назначь его для слежки."
                                if (!pane.roi) return "Обведи рамкой объект, за которым следим: клип поедет так, чтобы объект остался на месте."
                                return "Рамка " + Math.round(pane.roi[2]) + "×" + Math.round(pane.roi[3]) +
                                       " px · объект держим таким, как на " + pane.fmtT(pane.startUs)
                            }
                            if (!pane.src) return "Выдели видео в таймлайне и назначь его источником."
                            if (!pane.flw.length) return "Выдели слой и добавь как Follower."
                            if (pane.optPlanar && pane.surfQuad)
                                return "Поверхность по углам · старт с " + pane.fmtT(pane.startUs)
                            if (!pane.roi) return pane.optPlanar
                                ? "Нажми «Выбрать поверхность» и растяни углы по экрану."
                                : "Обведи объект рамкой в плеере."
                            return "Рамка " + Math.round(pane.roi[2]) + "×" + Math.round(pane.roi[3]) +
                                   " px · старт с " + pane.fmtT(pane.startUs)
                        }
                    }

                    // ---- прогресс ----
                    Column {
                        width: parent.width; spacing: 6
                        visible: pane.progOn
                        Item {
                            width: parent.width; height: 14
                            Text {
                                anchors.left: parent.left; text: pane.progMsg
                                color: LVColor.Text_Primary; font: LVFont.SubtitleRegular11
                            }
                            Text {
                                anchors.right: parent.right; text: Math.round(pane.progPct) + "%"
                                color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                            }
                        }
                        CustomProgressBar {
                            width: parent.width; height: 4
                            progress: Math.max(0, Math.min(1, pane.progPct / 100))
                        }
                    }

                    Text {
                        width: parent.width; wrapMode: Text.Wrap
                        visible: pane.logText !== ""
                        text: pane.logText
                        color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                    }

                    // ---- действие: всегда внизу ----
                    Item { width: 1; height: 4 }   // воздух перед кнопкой действия (у кнопки нет padding)

                    // ---- подтверждение перезаписи ключей ----
                    Column {
                        width: parent.width; spacing: 6
                        visible: pane.askOverwrite
                        Text {
                            width: parent.width; wrapMode: Text.Wrap
                            text: "На слое уже есть ключи трекинга. Перезаписать их?"
                            color: LVColor.State_Warning_Default; font: LVFont.SubtitleRegular11
                        }
                        Row {
                            width: parent.width; spacing: 8
                            property real bw: (width - spacing) / 2
                            PrimaryButton {
                                width: parent.bw; height: 28
                                text: "Перезаписать"
                                onClicked: { pane.askOverwrite = false; pane.runTrack() }
                            }
                            SecondayButton {
                                width: parent.bw; height: 28
                                text: "Отмена"
                                onClicked: pane.askOverwrite = false
                            }
                        }
                    }

                    // ---- подтверждение снятия трека ----
                    Column {
                        width: parent.width; spacing: 6
                        visible: pane.askClear
                        Text {
                            width: parent.width; wrapMode: Text.Wrap
                            text: pane.optReframe
                                  ? "Снять с видео все ключи слежки? Отменить это нельзя."
                                  : "Снять с follower-слоёв все ключи трекинга? Отменить это нельзя."
                            color: LVColor.State_Warning_Default; font: LVFont.SubtitleRegular11
                        }
                        Row {
                            width: parent.width; spacing: 8
                            property real bw: (width - spacing) / 2
                            PrimaryButton {
                                width: parent.bw; height: 28
                                text: "Снять"
                                onClicked: { pane.askClear = false; pane.clearTrack() }
                            }
                            SecondayButton {
                                width: parent.bw; height: 28
                                text: "Отмена"
                                onClicked: pane.askClear = false
                            }
                        }
                    }

                    PrimaryButton {
                        width: parent.width; height: 32
                        visible: !pane.askOverwrite && !pane.askClear
                        text: pane.optReframe ? "Следить за объектом с этого кадра" : "Трекнуть с этого кадра"
                        enabled: !pane.busy && pane.src !== null && pane.haveRegion
                                 && (pane.optReframe || pane.flw.length > 0)
                        onClicked: pane.startTrack()
                    }
                    TertiaryButton {
                        width: parent.width; height: 24
                        visible: !pane.askOverwrite && !pane.askClear && pane.keyTargets().length > 0
                        text: pane.optReframe ? "Убрать слежку с видео" : "Убрать трек со слоёв"
                        enabled: !pane.busy
                        onClicked: pane.askClear = true
                    }
                }
            }

            // ================= 3D КАМЕРА =================
            // Только настройки. Движение камеры, ключи и превью — РОДНЫЕ (плеер + таймлайн + ромб).
            // Пресетов движения и ручной позы здесь нет сознательно: камера двигается в плеере.
            Flickable {
                visible: pane.tool === "cam"
                anchors { left: parent.left; right: parent.right; top: hline.bottom; bottom: parent.bottom
                          leftMargin: 16; rightMargin: 16; topMargin: 12; bottomMargin: 12 }
                contentHeight: ccol.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: ccol
                    width: parent.width
                    spacing: 10

                    // ---- клип камеры удалили, а сцена жива ----
                    Repeater {
                        model: pane.lost
                        Column {
                            width: ccol.width; spacing: 8
                            Rectangle {   // подложка-предупреждение: это ЧП, а не рядовая подсказка
                                width: parent.width; height: warn.implicitHeight + 20; radius: 4
                                color: Qt.rgba(0.90, 0.35, 0.30, 0.12)
                                Text {
                                    id: warn
                                    x: 10; y: 10; width: parent.width - 20; wrapMode: Text.Wrap
                                    color: LVColor.Text_Primary; font: LVFont.SubtitleRegular11
                                    text: "Клип камеры удалён с таймлайна, но сцена цела: привязок — " +
                                          modelData.binds + ", ключей движения — " + modelData.keys + ". " +
                                          "Верните его через ⌘Z или нажмите «Вернуть камеру» — " +
                                          "глубины и путь камеры сохранятся."
                                }
                            }
                            PrimaryButton {
                                width: parent.width; height: 32
                                text: pane.camBusy ? pane.camBusy : "Вернуть камеру"
                                enabled: !pane.camBusy
                                onClicked: pane.restoreCam(modelData.tok)
                            }
                        }
                    }

                    // ---- камеры нет ----
                    Column {
                        width: parent.width; spacing: 10
                        visible: !pane.cam && pane.lost.length === 0
                        Text {
                            width: parent.width; wrapMode: Text.Wrap
                            color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                            text: "Камера заберёт дорожки под собой и разложит их по глубине: " +
                                  "верхняя — ближняя, нижняя — дальняя. Двигать камеру и ставить ключи — " +
                                  "в плеере и на таймлайне, как обычный клип."
                        }
                        Text {
                            width: parent.width; wrapMode: Text.Wrap
                            visible: pane.camTracks.length === 0
                            color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                            text: "В проекте нет дорожек с медиа."
                        }
                        PrimaryButton {
                            width: parent.width; height: 32
                            text: pane.camBusy ? pane.camBusy : "Создать камеру"
                            enabled: !pane.camBusy && pane.camTracks.length > 0
                            onClicked: pane.createCam()
                        }
                    }

                    // ---- камера есть ----
                    Column {
                        width: parent.width; spacing: 10
                        visible: pane.cam !== null

                        // Выбор сцены — только когда камер больше одной.
                        ParamSettingComboBox {
                            width: parent.width; height: 24
                            visible: pane.cams.length > 1
                            text: "Сцена"
                            itemList: {
                                var a = []
                                for (var i = 0; i < pane.cams.length; i++) a.push("Сцена " + (i + 1))
                                return a
                            }
                            selectedIndex: pane.camIdx
                            onSelectedIndexChanged: {
                                pane.camIdx = selectedIndex
                                pane.binds = pane.cam ? (pane.cam.binds || {}) : ({})
                                pane.armBind = false
                            }
                        }

                        // ---- Сцена сбоку: наглядная раскладка слоёв по глубине ----
                        // Камера слева, дальше — глубина вправо. Пластина = дорожка, цвет кодирует
                        // глубину (та же шкала, что у ползунка). Клик по пластине выбирает дорожку.
                        PanelSettingsGroup {
                            id: gScene
                            width: parent.width
                            height: totalHeight
                            title: "Сцена сбоку"
                            useCheck: false; useKeyFrame: false; useReset: false
                            collapsible: true
                            dataHeight: sceneBox.height
                            Item {
                                id: sceneBox
                                y: gScene.dataY
                                width: parent.width
                                height: 96

                                readonly property real camX: 18
                                readonly property real farX: width - 14
                                readonly property real midY: 44
                                property string hovered: ""   // подпись слоя под курсором
                                function xOf(d) { return camX + Math.max(0, Math.min(1, d)) * (farX - camX) }

                                Rectangle {   // подложка сцены
                                    anchors.fill: parent
                                    radius: 4
                                    color: LVColor.Fill_Background_Level4
                                }
                                Rectangle {   // ось глубины
                                    x: sceneBox.camX; y: sceneBox.midY
                                    width: sceneBox.farX - sceneBox.camX; height: 1
                                    color: LVColor.Line_Level2
                                }
                                // камера
                                Rectangle {
                                    x: sceneBox.camX - 9; y: sceneBox.midY - 7
                                    width: 12; height: 14; radius: 2
                                    color: LVColor.Brand_Primary_Default
                                }
                                Text {
                                    x: sceneBox.camX - 12; y: sceneBox.midY + 12
                                    text: "камера"
                                    color: LVColor.Text_Tertiary; font: LVFont.OverlineRegular10
                                }
                                Text {
                                    anchors { right: parent.right; rightMargin: 6 }
                                    y: sceneBox.midY + 12
                                    text: "даль"
                                    color: LVColor.Text_Tertiary; font: LVFont.OverlineRegular10
                                }
                                // слои сцены
                                Repeater {
                                    model: pane.camTracksTop
                                    delegate: Item {
                                        required property var modelData
                                        required property int index
                                        readonly property real d: pane.depthOf(modelData.id)
                                        visible: d >= 0
                                        x: sceneBox.xOf(d) - 8
                                        y: 12
                                        width: 16; height: 64
                                        Rectangle {   // пластина слоя
                                            anchors.horizontalCenter: parent.horizontalCenter
                                            y: 6
                                            width: 3; height: 44; radius: 1
                                            color: pane.depthColor(parent.d)
                                        }
                                        Text {        // номер слоя
                                            anchors.horizontalCenter: parent.horizontalCenter
                                            y: 52
                                            text: (index + 1)
                                            color: LVColor.Text_Tertiary; font: LVFont.OverlineRegular10
                                        }
                                        // Подпись наведённого слоя показываем внизу схемы: attached ToolTip
                                        // требует QtQuick.Controls, а лишний импорт в инжектнутой панели — риск.
                                        MouseArea {
                                            id: ma
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            onContainsMouseChanged: sceneBox.hovered =
                                                containsMouse ? ((modelData.media || modelData.label) + " · " + parent.d.toFixed(2)) : ""
                                        }
                                    }
                                }
                                Text {
                                    anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 4 }
                                    width: parent.width - 12
                                    horizontalAlignment: Text.AlignHCenter
                                    elide: Text.ElideMiddle
                                    text: sceneBox.hovered !== "" ? sceneBox.hovered
                                          : (Object.keys(pane.binds).length === 0 ? "ни одна дорожка не привязана" : "")
                                    color: sceneBox.hovered !== "" ? LVColor.Text_Primary : LVColor.Text_Tertiary
                                    font: LVFont.OverlineRegular10
                                }
                            }
                        }

                        // ---- Кривая камеры = кривая искажения ----
                        // У перспективы НЕТ собственной кривой: четыре угла corner-pin это ВЫЧИСЛЯЕМАЯ
                        // функция позы камеры. Поэтому показываем кривую КАМЕРЫ и точки, в которых дилиб
                        // реально пересчитывает углы — так видно, что кривая применяется, а не надо
                        // верить на слово. Ручки нормирует сервер (camera._cam_curve): control-точки
                        // CapCut АБСОЛЮТНЫ, и повторять их арифметику здесь — напрашиваться на ту же
                        // ошибку, из-за которой искажение когда-то шло по чужой кривой.
                        PanelSettingsGroup {
                            id: gCurve
                            width: parent.width
                            height: totalHeight
                            title: "Кривая камеры → искажение"
                            useCheck: false; useKeyFrame: false; useReset: false
                            collapsible: true
                            dataHeight: curveBox.height
                            Item {
                                id: curveBox
                                y: gCurve.dataY
                                width: parent.width
                                height: 148
                                readonly property var cv: pane.cam && pane.cam.curve ? pane.cam.curve : null
                                readonly property real gx: 8
                                readonly property real gyTop: 6
                                readonly property real gw: width - gx - 10
                                readonly property real gh: 84
                                function px(u) { return gx + u * gw }
                                function py(w) { return gyTop + (1 - w) * gh }

                                Rectangle {   // поле графика
                                    x: curveBox.gx; y: curveBox.gyTop
                                    width: curveBox.gw; height: curveBox.gh
                                    radius: 2
                                    color: LVColor.Fill_Background_Level3
                                    border.width: 1
                                    border.color: LVColor.Line_Level2
                                }
                                Repeater {    // ключи камеры — вертикальные направляющие
                                    model: curveBox.cv && curveBox.cv.props.length ? curveBox.cv.props[0].keys : []
                                    delegate: Rectangle {
                                        required property var modelData
                                        x: curveBox.px(modelData[0]); y: curveBox.gyTop
                                        width: 1; height: curveBox.gh
                                        color: LVColor.Line_Level2
                                    }
                                }
                                Repeater {    // сами кривые: по одной ломаной на свойство
                                    model: curveBox.cv ? curveBox.cv.props : []
                                    delegate: Item {
                                        id: propItem
                                        required property var modelData
                                        readonly property var pr: modelData
                                        anchors.fill: parent
                                        Repeater {
                                            model: propItem.pr.pts.length - 1
                                            delegate: Rectangle {
                                                required property int index
                                                readonly property real ax: curveBox.px(propItem.pr.pts[index][0])
                                                readonly property real ay: curveBox.py(propItem.pr.pts[index][1])
                                                readonly property real bx: curveBox.px(propItem.pr.pts[index + 1][0])
                                                readonly property real by: curveBox.py(propItem.pr.pts[index + 1][1])
                                                x: ax; y: ay - 0.75
                                                width: Math.max(1, Math.sqrt((bx - ax) * (bx - ax) + (by - ay) * (by - ay)))
                                                height: 1.5
                                                radius: 0.75
                                                transformOrigin: Item.Left
                                                rotation: Math.atan2(by - ay, bx - ax) * 180 / Math.PI
                                                color: pane.curveColor(propItem.pr.key)
                                            }
                                        }
                                    }
                                }
                                Repeater {    // точки сетки: где ИМЕННО пересчитываются углы перспективы
                                    model: curveBox.cv ? curveBox.cv.grid : []
                                    delegate: Rectangle {
                                        required property var modelData
                                        x: curveBox.px(modelData) - 1
                                        y: curveBox.gyTop + curveBox.gh + 3
                                        width: 2; height: 4; radius: 1
                                        color: LVColor.Brand_Primary_Default
                                        opacity: 0.55
                                    }
                                }
                                Row {         // легенда
                                    x: curveBox.gx
                                    y: curveBox.gyTop + curveBox.gh + 12
                                    spacing: 10
                                    Repeater {
                                        model: curveBox.cv ? curveBox.cv.props : []
                                        delegate: Row {
                                            required property var modelData
                                            spacing: 4
                                            Rectangle {
                                                width: 8; height: 2; radius: 1
                                                anchors.verticalCenter: parent.verticalCenter
                                                color: pane.curveColor(modelData.key)
                                            }
                                            Text {
                                                text: modelData.label
                                                color: modelData.cubic ? LVColor.Text_Primary : LVColor.Text_Tertiary
                                                font: LVFont.OverlineRegular10
                                            }
                                        }
                                    }
                                }
                                Text {        // подпись: сколько точек и с каким шагом
                                    x: curveBox.gx
                                    y: curveBox.gyTop + curveBox.gh + 28
                                    width: curveBox.gw
                                    elide: Text.ElideRight
                                    text: !curveBox.cv
                                          ? "у камеры нет кривой — поставьте ей ключи в родном редакторе"
                                          : "искажение пересчитывается в " + curveBox.cv.grid.length
                                            + " точках — метки снизу"
                                    color: LVColor.Text_Tertiary
                                    font: LVFont.OverlineRegular10
                                }
                            }
                        }

                        // ---- Дорожки в сцене ----
                        PanelSettingsGroup {
                            id: gTracks
                            width: parent.width
                            height: totalHeight
                            title: "Дорожки в сцене"
                            useCheck: false; useKeyFrame: false; useReset: false
                            collapsible: true
                            dataHeight: trkBox.height
                            Column {
                                id: trkBox
                                y: gTracks.dataY
                                width: parent.width
                                spacing: 8
                                Repeater {
                                    model: pane.camTracksTop
                                    delegate: Column {
                                        required property var modelData
                                        readonly property real d: pane.depthOf(modelData.id)
                                        readonly property bool bound: d >= 0
                                        readonly property var tl: pane.tiltOf(modelData.id)
                                        width: trkBox.width
                                        spacing: 3
                                        VECheckBox {
                                            width: parent.width; height: 22
                                            title: (modelData.media || modelData.label) +
                                                   (modelData.nsegs > 1 ? "  ·  " + modelData.nsegs + " клипов" : "")
                                            checkable: true
                                            checked: bound
                                            onClicked: pane.bindTrack(modelData.id, bound ? null : 0.5)
                                        }
                                        TableRowItemS {
                                            width: parent.width
                                            visible: bound
                                            text: "Глубина"
                                            contentItem: VESpinBoxSlider {
                                                height: 24
                                                minValue: 0; maxValue: 1; decimals: 2
                                                curValue: bound ? d : 0.5
                                                onValueChanged: function (v) { pane.bindTrack(modelData.id, v) }
                                            }
                                        }
                                        // НАКЛОН ПЛОСКОСТИ ДОРОЖКИ — два угла в градусах. Свойство
                                        // потрековое, поэтому стоит здесь же, под глубиной, а не
                                        // отдельной секцией: у дорожки это такая же её характеристика.
                                        // За единицу у VESpinBoxSlider отвечает unitChar (не suffix).
                                        TableRowItemS {
                                            width: parent.width
                                            visible: bound
                                            text: "Наклон вперёд"
                                            contentItem: VESpinBoxSlider {
                                                height: 24
                                                minValue: -60; maxValue: 60; decimals: 0; unitChar: "°"
                                                curValue: tl[0]
                                                onValueChanged: function (v) { pane.setTilt(modelData.id, v, tl[1]) }
                                            }
                                        }
                                        TableRowItemS {
                                            width: parent.width
                                            visible: bound
                                            text: "Наклон вбок"
                                            contentItem: VESpinBoxSlider {
                                                height: 24
                                                minValue: -60; maxValue: 60; decimals: 0; unitChar: "°"
                                                curValue: tl[1]
                                                onValueChanged: function (v) { pane.setTilt(modelData.id, tl[0], v) }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // ---- Глубина резкости ----
                        PanelSettingsGroup {
                            id: gDof
                            width: parent.width
                            height: totalHeight
                            title: "Глубина резкости"
                            useCheck: false; useKeyFrame: false; useReset: true
                            collapsible: true
                            collapsed: true
                            dataHeight: dofBox.height
                            onResetBtnClicked: pane.setCamDof(false, 1.96, 0.3)
                            Column {
                                id: dofBox
                                y: gDof.dataY
                                width: parent.width
                                spacing: 8
                                VECheckBox {
                                    id: dofOn
                                    width: parent.width; height: 22
                                    title: "Включить"
                                    checkable: true
                                    checked: pane.cam && pane.cam.dof ? pane.cam.dof.on === true : false
                                    onClicked: pane.setCamDof(checked, dofFocus.curValue, dofAper.curValue)
                                }
                                // Галочка сама по себе только пишет тумблер. Реально размывает
                                // дилиб — по эффекту «Размытие» внутри сборного клипа каждого
                                // объекта, а фокус ведёт слой-контроллер. Пока этого нет, честно
                                // говорим об этом, а не делаем вид, что всё включилось.
                                Text {
                                    width: parent.width; wrapMode: Text.Wrap
                                    visible: dofOn.checked && pane.dofReady !== null && !pane.dofReady.ready
                                    color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                                    text: "Размывает движок — эффектом «Размытие» внутри сборного клипа. Пока его нет, размывать нечего."
                                }
                                SecondayButton {
                                    width: parent.width; height: 28
                                    visible: dofOn.checked && pane.dofReady !== null && !pane.dofReady.ready && !pane.dofConfirm
                                    text: "Подготовить сцену"
                                    enabled: !pane.camBusy
                                    onClicked: pane.dofConfirm = true
                                }
                                // Заворот в сборные клипы МЕНЯЕТ структуру таймлайна — спрашиваем,
                                // как и перед снятием ключей: тихо перестраивать чужой проект нельзя.
                                Column {
                                    width: parent.width; spacing: 6
                                    visible: dofOn.checked && pane.dofConfirm
                                    Text {
                                        width: parent.width; wrapMode: Text.Wrap
                                        color: LVColor.Text_Primary; font: LVFont.SubtitleRegular11
                                        text: "Привязанные дорожки будут завёрнуты в сборные клипы с размытием внутри — это меняет структуру таймлайна. Продолжить?"
                                    }
                                    PrimaryButton {
                                        width: parent.width; height: 28
                                        text: pane.camBusy ? pane.camBusy : "Да, подготовить"
                                        enabled: !pane.camBusy
                                        onClicked: { pane.dofConfirm = false; pane.prepDof(dofFocus.curValue, dofAper.curValue) }
                                    }
                                    SecondayButton {
                                        width: parent.width; height: 28
                                        text: "Отмена"
                                        onClicked: pane.dofConfirm = false
                                    }
                                }
                                Text {
                                    width: parent.width; wrapMode: Text.Wrap
                                    visible: dofOn.checked && pane.dofReady !== null && pane.dofReady.ready
                                    color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                                    text: "Ползунки ниже — СТАРТОВЫЕ значения слоя. После «Подготовить сцену» рулит сам клип «DoF контроллер»: масштаб по вертикали — глубина, по горизонтали — диафрагма. Ключи ромбом, кривые родные."
                                }
                                TableRowItemS {
                                    width: parent.width
                                    visible: dofOn.checked
                                    text: "Фокус"
                                    contentItem: VESpinBoxSlider {
                                        id: dofFocus
                                        height: 24
                                        minValue: 1.0; maxValue: 5.0; decimals: 2
                                        curValue: pane.cam && pane.cam.dof ? pane.cam.dof.focus : 1.96
                                    }
                                }
                                TableRowItemS {
                                    width: parent.width
                                    visible: dofOn.checked
                                    text: "Сила"
                                    contentItem: VESpinBoxSlider {
                                        id: dofAper
                                        height: 24
                                        minValue: 0.0; maxValue: 1.0; decimals: 2
                                        curValue: pane.cam && pane.cam.dof ? pane.cam.dof.aperture : 0.3
                                    }
                                }
                            }
                        }

                        Text {
                            width: parent.width; wrapMode: Text.Wrap
                            visible: !pane.hintsSeen
                            color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                            text: "Выдели камеру в таймлайне и двигай её в плеере — слои разъедутся по глубине. " +
                                  "Ключи ставь родным ромбом."
                        }

                        // ---- действия внизу ----
                        Row {
                            width: parent.width
                            spacing: 8
                            property real bw: (width - spacing) / 2
                            SecondayButton {
                                width: parent.bw; height: 28
                                text: "Ещё камера"
                                enabled: !pane.camBusy
                                onClicked: pane.createCam()
                            }
                            SecondayButton {
                                id: delBtn
                                width: parent.bw; height: 28
                                property bool confirmDel: false
                                text: confirmDel ? "точно удалить?" : "Удалить камеру"
                                enabled: !pane.camBusy
                                // Двухшаговое удаление вместо модалки: второй клик в течение 3с подтверждает.
                                // Удаление снимает ключи со всех ведомых дорожек — это необратимо.
                                onClicked: {
                                    if (delBtn.confirmDel) { delBtn.confirmDel = false; pane.deleteCam() }
                                    else { delBtn.confirmDel = true; delTimer.restart() }
                                }
                                Timer { id: delTimer; interval: 3000; onTriggered: delBtn.confirmDel = false }
                            }
                        }
                    }
                }
            }
        }
    }
}
