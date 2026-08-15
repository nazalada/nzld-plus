// Строка «Искажение» (corner-pin) для панели «Трансформация».
// Зеркалит родную строку «Расположение» из SegmentTransformView.qml:146-208.
// Грузится плагином через QQmlComponent::setData с url в каталоге панели, поэтому
// импорты и контекст (keyframeMgr / viewModel / QmlKeyframe / i18n) резолвятся как у своих.
import FusionUI
import FunctionPanel
import Qt5Compat.GraphicalEffects
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import VECreator
import GeneralBiz
import GeneralBizUI

PanelTableRowItem {
    id: distortItem
    // i18n.tr("pc_deformation_transform") НЕ подходит: ключ есть только в zh-Hans,
    // в ru его нет, а tinygettext на промахе вернёт сам msgid → в UI была бы строка-ключ.
    text: "Искажение"

    // АВТО-КЕЙФРЕЙМ. Плагин дёргает pluginTick из C++ (QObject::setProperty) сразу после
    // set_corner_pin. Пока ключей нет — не вмешиваемся (живой драг работает как раньше).
    // Как только ключ появился, свойством владеет он: restorePropertyValuesFromKeyframe
    // затирает наш set_corner_pin на каждом ре-конверте. Поэтому пишем ключ РОДНЫМ путём —
    // тем же вызовом, что и клик по ромбу, — и тогда restore возвращает уже НАШЕ значение.
    // ФАЗЫ. onClickSetKeyFrame — ТОГГЛ (add/remove на плейхеде), не update. Снятие ключа само
    // дёргает ре-конверт → restore затирает наш corner-pin → повторная постановка захватывала
    // уже затёртое (два одинаковых ключа). Поэтому порядок: 1) снять ключ, 2) C++ пишет
    // set_corner_pin, 3) поставить ключ — он захватит уже НАШЕ значение.
    // pluginArmed запоминает «ключи были» между фазами: после снятия addedPoint=false, и если
    // ключ был единственным, left/rightBtnActived тоже false — проверка в фазе 2 иначе сорвётся.
    // АВТО-КЕЙФРЕЙМ, как у родных свойств CapCut: если у «Искажения» УЖЕ есть ключи, а на плейхеде
    // ключа нет — драг создаёт новый. Ключ ставит сам CapCut (тот же вызов, что и клик по ромбу);
    // он захватит интерполированное значение, а верное впишет cp_write_keys сразу после.
    // Пока ключей нет вообще — не вмешиваемся: драг просто меняет статичное значение (тоже как в CapCut).
    // C++ дёргает pluginPhase через QObject::setProperty (console.log бесполезен — CapCut ставит
    // свой qInstallMessageHandler и глотает сообщения Qt).
    // АВТО-КЕЙФРЕЙМ, как у родных свойств CapCut: если у «Искажения» УЖЕ есть ключи, а на плейхеде
    // ключа нет — драг создаёт новый. Ключ ставит сам CapCut (тот же вызов, что и клик по ромбу),
    // он захватит интерполированное значение, а верное впишет cp_write_keys сразу после.
    // Пока ключей нет — не вмешиваемся: драг меняет статичное значение (тоже как в CapCut).
    // ИЗВЕСТНЫЙ ПОТОЛОК: правый нижний угол иногда отскакивает — групповой onClickSetKeyFrame
    // для корнер-пина недетерминирован (его типов нет в UI-таблицах), плюс g_playhead дрожит
    // (пишем на T1, читаем на T2). Версия cp_sync_curves лежит в ve_hook.cpp.sync_experiment.
    property int pluginPhase: 0
    onPluginPhaseChanged: {
        if (pluginPhase !== 1) return
        var vm = cornerPinKf.parentViewModel
        if (!vm) return
        var hasKeys = cornerPinKf.leftBtnActived || cornerPinKf.rightBtnActived || cornerPinKf.addedPoint
        if (hasKeys && !cornerPinKf.addedPoint) vm.onClickSetKeyFrame(cornerPinKf.keyframeData)
    }

    // Кого показывает панель. g_selected_segid ловится с шины «последним пришедшим» и скачет,
    // когда CapCut опрашивает несколько сегментов — отсюда ручки на чужом клипе.
    // Панель же знает точно. C++ читает это свойство через QObject::property.
    property string pluginMatId: cornerPinKf.keyframeData ? cornerPinKf.keyframeData.materialID : ""

    property string pluginDump: "?"     // список свойств appContext — ищем в нём имя плейхеда
    property string pluginKfDump: "?"   // то же для keyframeData (вдруг время знает он)

    Component.onCompleted: {
        var s = ""
        try { for (var k in appContext) s += k + " " } catch (e) { s = "ERR:" + e }
        distortItem.pluginDump = s
        var d = ""
        try { for (var k2 in cornerPinKf.keyframeData) d += k2 + " " } catch (e2) { d = "ERR:" + e2 }
        distortItem.pluginKfDump = d
    }

    contentItem: Item {
        height: 22

        // Ромб кейфрейма — родной компонент. propertyType у VEKeyFrame объявлен как `var`,
        // а PanelKeyframeData::updatePropertyType перегружен (QString) и (QStringList),
        // поэтому массив из 4 углов принимается так же, как [KFTypePositionX, KFTypePositionY]
        // у «Расположения». KFTypeCornerPin* — реальные CONSTANT QString-свойства QmlKeyframe [161..164].
        VEKeyFrame {
            id: cornerPinKf
            anchors {
                verticalCenter: parent.verticalCenter
                right: parent.right
            }
            parentViewModel: keyframeMgr
            propertyType: [QmlKeyframe.KFTypeCornerPinUpLeft,
                           QmlKeyframe.KFTypeCornerPinUpRight,
                           QmlKeyframe.KFTypeCornerPinDownLeft,
                           QmlKeyframe.KFTypeCornerPinDownRight]
        }
    }
}
