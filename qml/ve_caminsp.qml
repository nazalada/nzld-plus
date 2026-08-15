// Параметры КАМЕРЫ в родном инспекторе (справа сверху).
//
// Выделили клип камеры — обычные «Масштаб / Расположение / Повернуть / Искажение» не по делу:
// у камеры это наезд, повороты и глубина. Поэтому мы прячем родной SegmentTransformView и
// показываем вместо него свой набор — но ПОВЕРХ ЕГО ЖЕ viewModel, так что наезд, повороты и
// ключи остаются полностью родными (ромб, откат, кривые — всё работает как обычно).
//
// Повороты влево-вправо и вверх-вниз — это панорама камеры, прочитанная как разворот сцены.
// Отдельных свойств под них в CapCut нет, поэтому крутим родные X/Y, а показываем в градусах.
import QtQuick
import FusionUI
import GeneralBizUI
import FunctionPanel
import VECreator

Item {
    id: insp

    readonly property string api: "http://127.0.0.1:8756"
    property string selSegId: ""       // C++ кладёт сюда id выделенного сегмента
    property string projDir: ""
    property string camTok: ""
    property string camSeg: ""         // сегмент камеры, выделенной ПРЯМО СЕЙЧАС
    // ВСЕ камеры проекта, а не первая. Мультикамера работает, а сравнение с cameras[0] давало
    // на второй камере «выделил камеру — вижу родные свойства слоя»; совпади оно случайно —
    // показало бы параметры ЧУЖОЙ камеры.
    property var cams: []
    property real panGain: 0           // градусов на полный ход панорамы (0 = поворот выключен)
    property real camRoll: 0           // завал горизонта сцены (градусы) — строка CAMROLL в риге
    property real pivotDepth: 0.5
    property real canvasW: 1920
    property bool loading: false
    // Глубина резкости — параметр КАМЕРЫ, а не отдельная панель: физически это её диафрагма и
    // плоскость фокуса. Держим здесь же, рядом с наездом и поворотами.
    property bool dofOn: false
    property real dofFocus: 1.96
    property real dofAper: 0.3
    property var dofReady: null        // {ctl, pairs, ready} — есть ли в сцене чем размывать
    property bool dofBusy: false

    readonly property bool isCam: selSegId !== "" && selSegId === camSeg

    // Родной блок трансформации — наш сосед по родителю. Ищем по наличию его вьюмодели:
    // имя типа в рантайме обрастает суффиксом, а утиная проверка переживает обновления.
    //
    // ПЕРЕСЧИТЫВАЕМ, а не биндим один раз. Биндинг зависел бы только от insp.parent, но родной
    // блок появляется у родителя АСИНХРОННО (панель дособирается уже после того, как нас в неё
    // вставили) и пересобирается на своих поводах — например после заворачивания клипов в
    // сборные. Один промах поиска оставлял native_ = null навсегда: наши строки прятались
    // (все они visible: vm !== null), а маска при этом уже убрала родные — инспектор оказывался
    // ПУСТЫМ. Теперь ищем заново на каждом опросе, пока не найдём.
    property var native_: null
    function findNative() {
        var c = insp.parent ? insp.parent.children : []
        for (var i = 0; i < c.length; i++) {
            if (c[i] === insp) continue
            try { if (c[i].viewModel && c[i].viewModel.setRotateValue !== undefined) return c[i] } catch (e) {}
        }
        return null
    }
    function syncNative() {
        var f = findNative()
        if (f !== insp.native_) {
            insp.native_ = f
            insp.dbg(f ? ("родной блок найден за " + (new Date().getTime() - insp.born) + " мс")
                       : "родной блок пропал — ретрай")
            applyMask()
        }
    }
    onParentChanged: { insp.tries = 0; syncNative() }

    // ⭐ ПОЧЕМУ ИНСПЕКТОР «СКАКАЛ». Родной блок появляется у родителя АСИНХРОННО, уже после того
    // как нас в панель вставили, а искали мы его только при создании и раз в 2 секунды. Промах
    // при создании = до двух секунд родных свойств вместо наших, а при быстрых кликах — наших
    // не видно вообще. Лечим двумя независимыми способами, чтобы не зависеть от одного:
    //   1) НАБЛЮДАТЕЛЬ: у родителя есть сигнал childrenChanged — он стреляет ровно в тот момент,
    //      когда родной блок добавили или убрали. Реакция мгновенная и без опроса.
    //   2) РЕТРАЙ С ПОТОЛКОМ на случай, если сигнал не придёт (блок дособрался, а список детей
    //      не менялся). 120 мс × 25 = 3 секунды, дальше молчим: бесконечный опрос грел бы
    //      процессор и на клипах, у которых родного блока нет в принципе (звук, эффект).
    //      Счётчик сбрасывается сменой родителя и выделения, поэтому следующий клик снова
    //      получает полные три секунды.
    property int tries: 0
    property double born: 0
    Connections {
        target: insp.parent
        ignoreUnknownSignals: true
        function onChildrenChanged() { insp.tries = 0; insp.syncNative() }
    }
    Timer {
        interval: 120; repeat: true
        running: insp.native_ === null && insp.tries < 25
        onTriggered: { insp.tries++; insp.syncNative() }
    }
    readonly property var vm: native_ ? native_.viewModel : null
    // Менеджер ключей родного блока — через него ромбы ставят НАСТОЯЩИЕ ключи CapCut,
    // с его же кривыми. Свои кнопки ключей заводить не нужно и нельзя: они бы не знали
    // ни про группировку свойств, ни про кривые.
    readonly property var kfm: native_ ? native_.keyframeMgr : null

    width: parent ? parent.width : 0
    height: visible ? grp.totalHeight : 0
    // Показываемся ТОЛЬКО когда есть к чему привязаться. Иначе группа была бы пустой рамкой.
    visible: isCam && vm !== null

    // Пока выделена камера — обычные строки инспектора не про неё, прячем ВЕСЬ блок.
    // Запоминаем, кто был виден, чтобы вернуть ровно то же на другом клипе.
    property var hidden: []
    // ⭐ ПОЧЕМУ МИГАЛО ПРИ ИЗМЕНЕНИИ ПАРАМЕТРА. Родной блок пересобирается не только на выделение,
    // но и на правку любого значения. Прошлая маска на это отвечала так: блок умер -> vm стал null
    // -> ветка «иначе» возвращала видимость спрятанным строкам (родные ВЫСКОЧИЛИ), новый блок
    // появился -> прятали заново (наши ВЕРНУЛИСЬ). Ровно «резко появляется старое и потом обратно
    // новое», и на каждое движение ползунка.
    //
    // Теперь маску снимает только УХОД С КАМЕРЫ. Пока выделена камера, потеря родного блока —
    // это его пересборка, а не повод показывать чужие настройки: маску держим, новых соседей
    // прячем сразу, как появятся.
    function applyMask() {
        if (!insp.isCam) { unmask(); return }
        if (insp.vm === null) return   // блок пересобирается — НЕ трогаем маску, иначе мигание
        remask()
    }
    // Прячет тех соседей, кто виден ПРЯМО СЕЙЧАС. Идемпотентна: уже скрытые пропускаются, поэтому
    // её можно звать сколько угодно — и по событию, и тиком.
    function remask() {
        var c = insp.parent ? insp.parent.children : []
        var h = insp.hidden
        for (var i = 0; i < c.length; i++)
            if (c[i] !== insp && c[i].visible === true) { h.push(c[i]); c[i].visible = false }
        insp.hidden = h
    }
    function unmask() {
        for (var j = 0; j < insp.hidden.length; j++)
            try { insp.hidden[j].visible = true } catch (e) {}
        insp.hidden = []
    }
    // Родной блок мог не пересобраться, а просто вернуть себе видимость (его строки висят на
    // биндингах, а мы их гасим присваиванием). Событием это не поймать, поэтому дешёвый тик —
    // это цикл по десятку соседей, без HTTP и без обхода дерева. Работает ТОЛЬКО на камере.
    Timer { interval: 200; repeat: true; running: insp.isCam && insp.vm !== null
            onTriggered: insp.remask() }
    // Страховка от пустого инспектора: если родной блок не вернулся за полторы секунды, это уже
    // не пересборка, а отказ — показываем родные настройки. Пустая панель хуже чужих настроек.
    Timer { interval: 1500; running: insp.isCam && insp.vm === null
            onTriggered: { insp.dbg("родной блок не вернулся за 1.5 с — отдаю родные настройки"); insp.unmask() } }
    onIsCamChanged: { insp.dbg("камера выделена: " + isCam); applyMask() }
    // Раньше тут стояло `isCam = false`, а isCam — readonly: присваивание бросало исключение,
    // и applyMask() следом уже не выполнялся, то есть спрятанные строки никто не возвращал.
    // Снимаем маску напрямую.
    Component.onDestruction: unmask()

    // console.log CapCut глотает — единственный канал диагностики. Пишет /tmp/ve_qml_dbg.log.
    // Только переходы состояния, не опрос: строк должно быть по несколько на клик, а не поток.
    function dbg(m) {
        var x = new XMLHttpRequest()
        x.open("POST", insp.api + "/api/dbg")
        x.setRequestHeader("Content-Type", "application/json")
        x.send(JSON.stringify({ m: "CAMINSP " + m + " sel=" + insp.selSegId.substring(0, 8) }))
    }

    function http(method, path, body, cb) {
        var x = new XMLHttpRequest()
        x.onreadystatechange = function () {
            if (x.readyState !== XMLHttpRequest.DONE) return
            var r = null
            try { r = JSON.parse(x.responseText) } catch (e) {}
            cb(x.status === 200 ? r : null)
        }
        x.open(method, insp.api + path)
        if (body) { x.setRequestHeader("Content-Type", "application/json"); x.send(JSON.stringify(body)) }
        else x.send()
    }

    // Из списка камер выбираем ТУ, что выделена. Раньше здесь стояла cameras[0], поэтому вторая
    // камера проекта нашего инспектора не получала совсем.
    function applyCam() {
        var pick = null
        for (var i = 0; i < insp.cams.length; i++)
            if (insp.cams[i] && insp.cams[i].seg === insp.selSegId) { pick = insp.cams[i]; break }
        if (!pick) { if (insp.camSeg !== "") insp.camSeg = ""; return }
        insp.camTok = pick.tok
        insp.camSeg = pick.seg || ""
        insp.panGain = pick.pan ? (pick.pan.gain || 0) : 0
        insp.camRoll = pick.roll !== undefined ? pick.roll : 0
        insp.pivotDepth = pick.pivot !== undefined ? pick.pivot : 0.5
        var dof = pick.dof || {}
        insp.dofOn = !!dof.on
        if (dof.focus !== undefined) insp.dofFocus = dof.focus
        if (dof.aperture !== undefined) insp.dofAper = dof.aperture
        insp.dofReady = pick.dofReady !== undefined ? pick.dofReady : null
    }
    onSelSegIdChanged: { insp.tries = 0; applyCam(); syncNative() }

    function refresh() {
        if (insp.loading) return
        insp.loading = true
        http("GET", "/api/projects", null, function (ps) {
            if (!ps || !ps.length) { insp.loading = false; return }
            var pick = null
            for (var i = 0; i < ps.length; i++) if (ps[i].open) { pick = ps[i]; break }
            // Признак «открыт» — это `.locked`, а он врёт в обе стороны (см. известные грабли).
            // Раньше на протухшем замке мы просто выходили, и камера не опознавалась НИКОГДА.
            // Список идёт по времени правки, поэтому первый — почти наверняка тот, что открыт;
            // а ошибиться проектом безопасно: id сегментов уникальны, чужая камера не совпадёт.
            if (!pick) pick = ps[0]
            insp.projDir = pick.dir
            http("GET", "/api/cam?dir=" + encodeURIComponent(pick.dir), null, function (s) {
                insp.loading = false
                insp.cams = (s && s.cameras) ? s.cameras : []
                if (s && s.canvas && s.canvas.length === 2) insp.canvasW = s.canvas[0]
                applyCam()
            })
        })
    }
    // Медленный тик — только про состояние на сервере (создали/удалили камеру, DoF). Поиск
    // родного блока с него снят: им теперь занимаются наблюдатель и быстрый ретрай выше.
    Timer { interval: 2000; repeat: true; running: true; onTriggered: insp.refresh() }
    Component.onCompleted: { insp.born = new Date().getTime(); dbg("создан"); syncNative(); refresh() }

    // Ролл сцены: тем же путём, что панорама и ось облёта — сервер правит сайдкар, пересобирает
    // риг, дилиб перечитывает его по mtime. Ноль тоже отправляем: это значение, а не «нет поля».
    function setRoll(deg) {
        if (!insp.camTok) return
        insp.camRoll = deg
        http("POST", "/api/cam/roll", { dir: insp.projDir, tok: insp.camTok, deg: deg },
             function (r) {})
    }
    function setPan(g) {
        if (!insp.camTok) return
        insp.panGain = g
        http("POST", "/api/cam/pan", { dir: insp.projDir, tok: insp.camTok, gain: g }, function (r) {})
    }
    function setPivot(d) {
        if (!insp.camTok) return
        insp.pivotDepth = d
        http("POST", "/api/cam/pivot", { dir: insp.projDir, tok: insp.camTok, depth: d }, function (r) {})
    }
    function setDof(on, focus, aper) {
        if (!insp.camTok) return
        insp.dofOn = on; insp.dofFocus = focus; insp.dofAper = aper
        http("POST", "/api/cam/dof",
             { dir: insp.projDir, tok: insp.camTok, on: on, focus: focus, aperture: aper },
             function (r) { insp.refresh() })
    }
    // Подготовка МЕНЯЕТ СТРУКТУРУ таймлайна: объекты заворачиваются в сборные клипы с эффектом
    // «Размытие» — своего рендера у плагина нет, дилиб умеет только крутить уже существующий
    // эффект внутри компаунда. Поэтому это отдельное действие, а не побочный эффект галочки.
    function prepDof() {
        if (!insp.camTok || insp.dofBusy) return
        insp.dofBusy = true
        http("POST", "/api/cam/dofprep",
             { dir: insp.projDir, tok: insp.camTok, focus: insp.dofFocus, aperture: insp.dofAper },
             function (r) { insp.dofBusy = false; insp.refresh() })
    }

    // Панорама в родных единицах ↔ градусы разворота. Родное «Расположение» идёт в ПИКСЕЛЯХ
    // канваса (замерено: −576 при −0.30 на канвасе 1920), а полный ход панорамы = ширина кадра.
    function degOf(v) { return insp.panGain > 0 && insp.canvasW > 0 ? v / insp.canvasW * insp.panGain : 0 }
    function valOf(d) { return insp.panGain > 0 ? d / insp.panGain * insp.canvasW : 0 }

    PanelSettingsGroup {
        id: grp
        width: parent.width
        height: totalHeight
        title: "Камера NZLD+"
        useCheck: false; useKeyFrame: false; useReset: true
        collapsible: true
        dataHeight: box.height
        onResetBtnClicked: {
            if (insp.vm) { insp.vm.resetPosition(); insp.vm.resetRotate() }
            insp.setPan(0)
        }

        Column {
            id: box
            y: grp.dataY
            width: parent.width
            spacing: 8

            Text {
                width: parent.width; wrapMode: Text.Wrap
                color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                text: insp.panGain > 0
                      ? "Повороты ставятся ключами как обычно — ромбом справа от строки."
                      : "Включи поворот, чтобы камера разворачивала сцену, а не просто ехала вбок."
            }

            VECheckBox {
                width: parent.width; height: 22
                title: "Поворот камеры (объём)"
                checkable: true
                checked: insp.panGain > 0
                onClicked: insp.setPan(checked ? 30 : 0)
            }

            TableRowItemS {
                width: parent.width
                visible: insp.panGain > 0
                text: "Сила поворота"
                contentItem: VESpinBoxSlider {
                    height: 24
                    minValue: 5; maxValue: 60; decimals: 0
                    curValue: insp.panGain > 0 ? insp.panGain : 30
                    onValueChanged: function (v) { insp.setPan(v) }
                }
            }

            // ── повороты по двум осям: те же контролы, что у родного «Повернуть» ──
            PanelTableRowItem {
                width: parent.width
                height: 22
                visible: insp.panGain > 0
                text: "Влево / вправо"
                Row {
                    anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                    spacing: 6
                    VESpinBoxSlider {
                        width: 190; height: 24
                        minValue: -60; maxValue: 60; decimals: 1; unitChar: "°"
                        curValue: insp.vm ? insp.degOf(insp.vm.xValue) : 0
                        onValueChanged: function (v) { if (insp.vm) insp.vm.setXValue(insp.valOf(v), false, true) }
                    }
                    VEKeyFrame {
                        anchors.verticalCenter: parent.verticalCenter
                        parentViewModel: insp.kfm
                        propertyType: QmlKeyframe.KFTypePositionX
                    }
                }
            }
            PanelTableRowItem {
                width: parent.width
                height: 22
                visible: insp.panGain > 0
                text: "Вверх / вниз"
                Row {
                    anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                    spacing: 6
                    VESpinBoxSlider {
                        width: 190; height: 24
                        minValue: -60; maxValue: 60; decimals: 1; unitChar: "°"
                        curValue: insp.vm ? insp.degOf(insp.vm.yValue) : 0
                        onValueChanged: function (v) { if (insp.vm) insp.vm.setYValue(insp.valOf(v), false, true) }
                    }
                    VEKeyFrame {
                        anchors.verticalCenter: parent.verticalCenter
                        parentViewModel: insp.kfm
                        propertyType: QmlKeyframe.KFTypePositionY
                    }
                }
            }
            PanelTableRowItem {
                width: parent.width
                height: 22
                text: "Завал горизонта"
                Row {
                    anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                    spacing: 6
                    VESpinBoxSlider {
                        width: 190; height: 24
                        minValue: -180; maxValue: 180; decimals: 1; unitChar: "°"
                        curValue: insp.camRoll
                        // ⭐ РОЛЛ ИДЁТ ЧЕРЕЗ РИГ, А НЕ ЧЕРЕЗ ПОВОРОТ КЛИПА КАМЕРЫ. История вопроса:
                        //   1. Крутили родной поворот клипа (`setRotateValue`) — вызов правильный,
                        //      родная панель зовёт ровно его же, но драйв читает ролл ТОЛЬКО из
                        //      кривой ключей rz, а статический поворот клипа не смотрит вообще.
                        //      Замер: rotation = 6.0 в документе, ключей поворота ноль, картинка
                        //      не шелохнулась. Заодно это давало расхождение с родным полем.
                        //   2. Пробовали ставить ключ их менеджером (`onClickSetKeyFrame`, как за
                        //      родным ромбом, с обязательной паузой плеера). ⛔ Не работает:
                        //      237 вызовов — НОЛЬ ключей, и `addedPoint` ни разу не стал true.
                        // Поэтому ролл стал свойством СЦЕНЫ и живёт там же, где панорама, ось
                        // облёта и наклон: сайдкар → строка CAMROLL в ve_rig.txt → дилиб. Тот
                        // складывает его с кривой rz, так что кейфреймленный ролл не сломан.
                        onValueChanged: function (v) { insp.setRoll(v) }
                    }
                }
            }

            // ── наезд: РОДНАЯ строка масштаба целиком (слайдер + ромб ключа) ──
            ParamSettingSliderViewEx {
                width: parent.width
                visible: insp.vm !== null
                // Своя подпись: с выключенным uniform_scale CapCut зовёт эту строку «Масштаб в
                // ширину», что для камеры бессмысленно — у неё это наезд.
                text: "Наезд"
                parentViewModel: insp.kfm   // без него ромб ключа на строке не работает (у родной SegmentTransformView он есть)
                sliderHandleViewModel: insp.vm
                checkValueViewModel: insp.vm
                checkScaleX: true
                commonSliderInfo: insp.vm ? insp.vm.scaleX : null
                unitChar: "%"
                alignmentLimit: false
                separateEditLimit: true
            }

            // ── глубина: вокруг какого плана камера разворачивается ──
            TableRowItemS {
                width: parent.width
                text: "Глубина оси"
                contentItem: VESpinBoxSlider {
                    height: 24
                    minValue: 0; maxValue: 1; decimals: 2
                    curValue: insp.pivotDepth
                    onValueChanged: function (v) { insp.setPivot(v) }
                }
            }
            Text {
                width: parent.width; wrapMode: Text.Wrap
                color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                text: "Слои на этой глубине остаются на месте, ближние и дальние расходятся в стороны."
            }

            // ── глубина резкости: диафрагма и плоскость фокуса — параметры камеры ──
            VECheckBox {
                width: parent.width; height: 22
                title: "Глубина резкости"
                checkable: true
                checked: insp.dofOn
                onClicked: insp.setDof(checked, insp.dofFocus, insp.dofAper)
            }
            // Галочка сама по себе ничего не размоет: нужен эффект «Размытие» внутри сборного
            // клипа у каждого объекта. Пока его нет — честно говорим об этом, а не молчим.
            Text {
                width: parent.width; wrapMode: Text.Wrap
                visible: insp.dofOn && insp.dofReady !== null && !insp.dofReady.ready
                color: LVColor.State_Warning_Default; font: LVFont.SubtitleRegular11
                text: insp.dofBusy
                      ? "Готовлю сцену…"
                      : "Сцена не готова: объекты надо завернуть в сборные клипы с размытием. "
                        + "Это изменит структуру таймлайна."
            }
            PrimaryButton {
                width: parent.width; height: 28
                visible: insp.dofOn && insp.dofReady !== null && !insp.dofReady.ready && !insp.dofBusy
                text: "Подготовить сцену"
                onClicked: insp.prepDof()
            }
            // Фокус и диафрагма ЗДЕСЬ БОЛЬШЕ НЕ ЖИВУТ (2026-08-09). Их источник — отдельный
            // слой «DoF контроллер»: вертикальный масштаб = глубина, горизонтальный = диафрагма.
            // Дубли в двух местах писали в файл-фолбэк и перебивали слой.
            Text {
                width: parent.width; wrapMode: Text.Wrap
                visible: insp.dofOn && insp.dofReady !== null && insp.dofReady.ready
                color: LVColor.Text_Tertiary; font: LVFont.SubtitleRegular11
                text: "Настройка — на клипе «DoF контроллер»: масштаб по вертикали ведёт глубину, по горизонтали диафрагму. Ключи ставятся ромбом, кривые родные."
            }
        }
    }
}
