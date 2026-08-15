// ve_hook.dylib — инжектится в CapCut. JUMP-ДЕТУР (без brk → без Crashpad).
// Вход setKeyframe перезаписываем абсолютным переходом на наш хук; украденные инструкции
// кладём в трамплин (своя MAP_JIT-память) + переход обратно на orig+16. Хук читает this+
// аргументы, потом зовёт трамплин (= оригинал доигрывает). Только наблюдение, форвардим всегда.
#include <cstdio>
#include <cstdint>
#include <cmath>
#include <cstring>
#include <cstdarg>
#include <cstdlib>
#include <ctime>
#include <fcntl.h>
#include <unistd.h>
#include <pwd.h>
#include <pthread.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <mach-o/dyld.h>
#include <libkern/OSCacheControl.h>
#include <objc/runtime.h>
#include <objc/message.h>
#include <dispatch/dispatch.h>

// ── Каталог CapCut ───────────────────────────────────────────────────────────
// Всё, что дилиб читает и пишет — гейты, QML-морда, логи — лежит в одном каталоге.
// Единственное место, где знание о путях зашито; остальной код зовёт ud("имя").
// getpwuid, а не getenv("HOME"): у песочницы HOME подменён на контейнер приложения,
// а нам нужен настоящий домашний каталог пользователя.
static const char *ud_make(void) {
    static char b[512];
    struct passwd *pw = getpwuid(getuid());
    const char *home = (pw && pw->pw_dir && pw->pw_dir[0]) ? pw->pw_dir : getenv("HOME");
    snprintf(b, sizeof b, "%s/Movies/CapCut/User Data", home ? home : "/tmp");
    return b;
}
static const char *ud_dir(void) { static const char *base = ud_make(); return base; }

// Путь к файлу в каталоге CapCut. Кольцо буферов, потому что несколько ud() попадают
// в одно выражение. ponytail: потолок — 8 живых путей на поток одновременно; ни один
// вызывающий не держит путь дольше одного вызова open/stat/fopen.
static const char *ud(const char *name) {
    static thread_local char ring[8][512];
    static thread_local int i = 0;
    char *b = ring[i = (i + 1) & 7];
    snprintf(b, sizeof ring[0], "%s/%s", ud_dir(), name);
    return b;
}

// Подстановка @UD@ в исходнике QML на реальный каталог CapCut. Нужна потому, что морда
// грузится через setData с ПОДДЕЛЬНЫМ qrc-URL (так резолвятся их же импорты), а значит
// Qt.resolvedUrl в ней указывает в qrc, и достать домашний каталог из QML нечем.
static void qml_subst_ud(char *s, size_t cap) {
    const char *rep = ud_dir();
    size_t rl = strlen(rep), len = strlen(s);
    for (char *p; (p = strstr(s, "@UD@")) != 0; ) {
        if (len - 4 + rl >= cap) { break; }   // не влезает — оставляем как есть, увидим в ошибке QML
        memmove(p + rl, p + 4, strlen(p + 4) + 1);
        memcpy(p, rep, rl);
        len = len - 4 + rl;
    }
}

static void Lf(const char *fmt, ...) {
    char b[1024]; va_list ap; va_start(ap, fmt);
    int n = vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
    int fd = open(ud("plugin_probe.log"),
                  O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) { write(fd, b, n > 0 ? (size_t)n : 0); close(fd); }
}

// разбор libc++ std::string — АЛЬТЕРНАТИВНАЯ раскладка (данные с начала, размер в байте 23,
// старший бит байта 23 = флаг «длинная»). LONG: {char* data@0; size@8; cap@16}.
static void logString(const char *tag, uint64_t sp) {
    if (!sp) { Lf("     %s=<null>\n", tag); return; }
    const uint8_t *p = (const uint8_t *)sp;
    uint64_t size; const char *data;
    if (p[23] & 0x80) { data = *(const char *const *)(p + 0); size = *(const uint64_t *)(p + 8); }
    else { size = p[23] & 0x7F; data = (const char *)p; }
    char buf[300]; uint64_t n = size < 295 ? size : 295;
    for (uint64_t i = 0; i < n; i++) { char c = data[i]; buf[i] = (c >= 32 && c < 127) ? c : '.'; }
    buf[n] = 0;
    Lf("     %s(len=%llu)=\"%s\"\n", tag, size, buf);
}

struct Target { const char *name; const char *sym; int strX[2]; void *addr; void *tramp; volatile int cnt; };
extern "C" unsigned long long CGEventSourceFlagsState(int stateID);   // аппаратное состояние модификаторов (глобально)
static volatile long g_ready = 0;  // время окончания расстановки хуков (для отсечки загрузки)
static volatile int g_trace = 0;   // ve_cptrace.txt -> логируем КАЖДЫЙ вызов рендер/рефреш-хуков (ищем сигнал ре-рендера)
static Target g[] = {
    {"TEClip.setKeyframe",
     "_ZN10TEClipBase11setKeyframeENSt3__112basic_stringIcNS0_11char_traitsIcEENS0_9allocatorIcEEEERKS6_",
     {1, 2}, 0, 0, 0},
    {"RL.setKeyframe",
     "_ZN5vesdk25VESequenceRenderLayerImpl11setKeyframeENSt3__110shared_ptrINS_3pub8KeyframeEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE",
     {2, -1}, 0, 0, 0},
    {"Seq.setKeyframe",
     "_ZN5vesdk14VESequenceImpl11setKeyframeENSt3__110shared_ptrINS_3pub8KeyframeEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE",
     {2, -1}, 0, 0, 0},
    {"Seq.commit", "_ZN5vesdk14VESequenceImpl6commitEv", {-1, -1}, 0, 0, 0},
    {"add.CommonKeyframe",
     "_ZN12CommonClient17addCommonKeyframeENSt3__110shared_ptrIN4lyra26AddCommonKeyframeReqStructEEEl",
     {-1, -1}, 0, 0, 0},
    {"upd.CommonKeyframe",
     "_ZN12CommonClient20updateCommonKeyframeENSt3__110shared_ptrIN4lyra29UpdateCommonKeyframeReqStructEEEl",
     {-1, -1}, 0, 0, 0},
    {"insertKeyframe",  // KeyframeUtils::insertKeyframe(shptr<CommonKeyframes>, shptr<CommonKeyframe>) — БЕЗ sret, обычный C-хук
     "_ZN4lvve13KeyframeUtils14insertKeyframeENSt3__110shared_ptrINS_15CommonKeyframesEEENS2_INS_14CommonKeyframeEEE",
     {-1, -1}, 0, 0, 0},
    {"get_segment",  // DraftQuery::get_segment(shared_ptr<Draft>, id, bool) — sret; ловим живой Draft (x0)
     "_ZN4lvve10DraftQuery11get_segmentENSt3__110shared_ptrINS_5DraftEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEEb",
     {-1, -1}, 0, 0, 0},
    {"getPreviewControl",  // VEEditorImpl::getPreviewControl() — ловим ВОЗВРАТ = живой VEPreviewControlImpl* (this-адъюст их)
     "_ZN5vesdk12VEEditorImpl17getPreviewControlEv",
     {-1, -1}, 0, 0, 0},
    {"refreshCurrentFrameNoFlag",  // PlayerClient::...NoFlag — RPC ручной правки; ловим ReqStruct для реплея (рефреш превью)
     "_ZN12PlayerClient25refreshCurrentFrameNoFlagENSt3__110shared_ptrIN4lyra34RefreshCurrentFrameNoFlagReqStructEEERKNS0_8functionIFvNS1_INS2_10RespStructEEEEEEbl",
     {-1, -1}, 0, 0, 0},
    {"refreshCurrentFrame",  // PlayerClient::refreshCurrentFrame (ФЛАГОВЫЙ) — UI зовёт скорее его; ловим ReqStruct для реплея
     "_ZN12PlayerClient19refreshCurrentFrameENSt3__110shared_ptrIN4lyra28RefreshCurrentFrameReqStructEEERKNS0_8functionIFvNS1_INS2_10RespStructEEEEEEbl",
     {-1, -1}, 0, 0, 0},
    {"setCurEditSeq",  // NewVEWrapper::setCurEditSeq — ловим x0 = экранный wrapper
     "_ZN4lvve12NewVEWrapper13setCurEditSeqENSt3__110shared_ptrIN5vesdk11IVESequenceEEEb",
     {-1, -1}, 0, 0, 0},
    {"wrap.prepare",     // NewVEWrapper::prepare() — зовётся при рендере кадра -> ловим wrapper
     "_ZN4lvve12NewVEWrapper7prepareEv", {-1, -1}, 0, 0, 0},
    {"wrap.updateAvClip",  // NewVEWrapper::updateAvClip — при правках клипа -> ловим wrapper
     "_ZN4lvve12NewVEWrapper12updateAvClipENSt3__110shared_ptrIN5vesdk3pub4ClipEEEb", {-1, -1}, 0, 0, 0},
    {"onFrameRendered",  // PlayerWin::onFrameRendered(ll,int) — экранный рендер-callback, зовётся НА КАЖДЫЙ
     "_ZN4lvve9PlayerWin15onFrameRenderedExi", {-1, -1}, 0, 0, 0},  // кадр -> this(x0)=НАСТОЯЩИЙ экранный wrapper
    {"wrap.setKeyFrame",  // NewVEWrapper::setKeyFrame(shptr<Keyframe>, layerId) — ИМЕННО это зовёт хендлер команды
     "_ZN4lvve12NewVEWrapper11setKeyFrameENSt3__110shared_ptrIN5vesdk3pub8KeyframeEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE",
     {-1, -1}, 0, 0, 0},  // при ре-рендере follower'а: this(x0)=НАСТОЯЩИЙ wrapper, x1=&shptr<Keyframe>, x2=&layerId (sret!)
    {"Server.invoke",  // lyra::Server::invoke(this=server, x1=&shptr<ReqStruct>, x2=sid) — sret; перехват ВСЕХ команд (ищем transform, потом реплеим)
     "_ZN4lyra6Server6invokeENSt3__110shared_ptrINS_9ReqStructEEEl",
     {-1, -1}, 0, 0, 0},
    // ПЛЕЙХЕД. lvve::KeyframePropertyHelper::getPropertyValues(shared_ptr<Segment>,
    //          KeyframeTypeKey const&, long long time) — ВЫЧИСЛИТЕЛЬ ключей: он обязан знать момент.
    // Раскладка сверена по прологу (mov x19,x1 / mov x21,x0 / mov x20,x8): x0=&sp<Segment>,
    // x1=&KeyframeTypeKey, x2=ВРЕМЯ (мкс, те же единицы что time_offset), x8=sret(vector<double>).
    // Функция sret → только ASM-трамплин, x8 обязан выжить.
    // (Ранее пробовали convert_clip_to_ve: его double оказался НЕ временем — всегда 1.0, это ratio.)
    {"getPropertyValues",
     "_ZN4lvve22KeyframePropertyHelper17getPropertyValuesENSt3__110shared_ptrINS_7SegmentEEERKNS_15KeyframeTypeKeyEx",
     {-1, -1}, 0, 0, 0},
    // ФАБРИКА КОМАНД. ServiceApiMapping::makeReq(service, api, json) — строит СВЕЖИЙ ReqStruct.
    // Нужна, чтобы убить дёрганье «Масштаба»: сейчас живой рефреш реплеит ЧУЖУЮ пойманную команду,
    // поэтому её надо сперва поймать (и она же тащит за собой свой scale). Хукаем, чтобы разово
    // подсмотреть строки service/api — они билд-константы, дальше захардкодим и позовём сами.
    // sret (возвращает shared_ptr) -> только ASM-трамплин.
    {"makeReq",
     "_ZN4lyra17ServiceApiMapping7makeReqEPKcS2_RKN8nlohmann10basic_jsonINSt3__13mapENS5_6vectorENS5_12basic_stringIcNS5_11char_traitsIcEENS5_9allocatorIcEEEEbxydSB_NS3_14adl_serializerENS7_IhNSB_IhEEEEEE",
     {-1, -1}, 0, 0, 0},
    // ИСТОЧНИК ПРАВИЛЬНОГО shared_ptr<IVESequence>. Своего геттера нет, а сырой VESequenceImpl*
    // в setCurEditSeq класть НЕЛЬЗЯ (нужен сдвиг указателя к базе IVESequence — краш проверен).
    // Предрендер гоняется постоянно и получает уже готовый shared_ptr по const& (x1 = указатель
    // на {ptr, ctrl}) — ловим его оттуда.
    // ⭐ ИСТОЧНИК ЖИВОЙ СЕКВЕНЦИИ. commit оказался единственным местом, откуда брался g_seq, и
    // между загрузкой проекта и первым коммитом указатель ПРОТУХАЕТ (не обнуляется!) — has_vtable
    // его отвергает, и молчат все гейты сразу. getDuration зовёт сам движок, this = живая
    // секвенция, поэтому указатель обновляется постоянно и протухнуть не успевает.
    // Живёт в libcccreator.dylib, не в libvideoeditor.
    {"Seq.getDuration", "_ZN5vesdk14VESequenceImpl11getDurationEv", {-1, -1}, 0, 0, 0},
    {"pre.setSequence",
     "_ZN18TEPreRenderManager11setSequenceERKNSt3__110shared_ptrIN5vesdk11IVESequenceEEE",
     {-1, -1}, 0, 0, 0},
    // ЗАЩИТА ОТ КРАША ДВИЖКА (не наш баг, но роняет проект пользователя). applyKeyframePreset
    // проверяет на null ТОЛЬКО второй ключ и разыменовывает первый вслепую — см. hook20.
    {"applyKeyframePreset",
     "_ZN4lvve13KeyframeUtils19applyKeyframePresetENSt3__110shared_ptrINS_14CommonKeyframeEEES4_RKNS_29CommonKeyframePropertiesParamE",
     {-1, -1}, 0, 0, 0},
};
// idx 4-5,7,15 (sret) — через ASM-трамплин; idx 6 (insertKeyframe), idx 8 (RefreshCurrentFrame), idx 14 — обычные C-хуки.
// NT=17, а НЕ 19: idx 17 (QQmlComponent.setData) и idx 18 (qt_metacall) ОТКЛЮЧЕНЫ.
// hook17 парсил QByteArray по раскладке Qt5 (size@d+8, offset@d+24, str=d+offset), но здесь Qt 6.2.2,
// где QByteArray = {Data* d; char* ptr; qsizetype size} и поля offset нет вообще → str = мусор →
// strstr по нему = сегфолт. Хук спал (CapCut грузит QML скомпилированными юнитами и setData не зовёт)
// и срабатывал только на НАШ вызов setData, убивая процесс. idx 18 вдобавок бил по захардкоженному
// адресу 0x655eb60. Обоих для Route A не нужно.
static const int NT = 22;   // idx17 = плейхед, idx18 = makeReq, idx19 = Seq.getDuration (источник секвенции),
// idx20 = pre.setSequence, idx21 = защита пресета кривой

typedef void (*fwd_t)(void *, void *, void *);
static void logHex24(uint64_t p) {
    if (!p) { Lf("       hex=<null>\n"); return; }
    const uint8_t *b = (const uint8_t *)p; char h[80]; int n = 0;
    for (int i = 0; i < 24; i++) n += snprintf(h + n, sizeof(h) - n, "%02x ", b[i]);
    Lf("       obj24=%s\n", h);
}
static void capture(int i, void *x0, void *x1, void *x2) {
    if (g_trace) Lf("%ld TRACE %s this=%p x1=%p x2=%p\n", (long)time(NULL), g[i].name, x0, x1, x2);   // трейс: КАЖДЫЙ вызов
    if (!g_ready || (long)time(NULL) - g_ready < 5) return; // пропускаем загрузочную пачку
    if (g[i].cnt >= 4) return;                              // потом ловим первые 4 ручных
    g[i].cnt++;
    Lf("%ld HIT %s this=%p x1=%p x2=%p\n", (long)time(NULL), g[i].name, x0, x1, x2);
    void *xs[3] = {x0, x1, x2};
    for (int k = 0; k < 2; k++) if (g[i].strX[k] >= 0) {
        uint64_t sp = (uint64_t)xs[g[i].strX[k]];
        logString(g[i].name, sp);   // попытка декода
        logHex24(sp);               // сырые 24 байта объекта строки — для точной раскладки
    }
}
// ==== ПЕРВАЯ ЗАПИСЬ ==== один раз, после ручной правки: слэмим scale=2.0 в тот же клип.
// Строки строим вручную в АЛЬТЕРНАТИВНОЙ раскладке libc++. Зовём ОРИГИНАЛ через трамплин.
static volatile int g_wrote = 0;
static void *volatile g_seq = 0;   // живой VESequenceImpl*, кешируем из commit-хука
static volatile int g_seq_kick = 0;      // секвенцию загрузили -> надо разбудить движок, чтобы он коммитнул
static volatile long g_seq_kick_at = 0;
static volatile int g_kick_tries = 0;
static volatile int g_no_kick = 0;   // ve_no_kick.txt: выключает ОБЕ страховки (источник + пинок) — для замера «до/после»
static volatile long g_hk[24] = {0};   // счётчики: 11=setCurEditSeq 12=prepare 13=updateAvClip 14=onFrameRendered 2=Seq.setKeyframe 3=commit   // сброс перебора кандидатов на каждую загрузку  // когда загрузили: даём проекту досчитаться, потом пинаем
// ЕДИНСТВЕННАЯ точка вызова commit(). Голый ((fwd_t)g[3].tramp)(g_seq,0,0) — верный путь к
// падению: после ПЕРЕОТКРЫТИЯ проекта g_seq ещё указывает на уничтоженную секвенцию (новую мы
// узнаём только из следующего commit-хука), а commit разыменовывает её на +0x14.
// Поймано минидампом 1365735c: VESequenceImpl::commit+0x9c, адрес сбоя 0x14 — краш случился
// сразу после «пересоздал камеру → перезаход → включил глубину резкости».
static void seq_commit(void);
static void *volatile g_player = 0;   // (не исп.)
static void *volatile g_pc = 0;       // живой VEPreviewControlImpl* (из возврата getPreviewControl) — рефреш превью
static volatile int g_qml_dead = 0;   // QML-дерево пересоздано (проект переоткрыли) -> инжектнутые item'ы протухли
static void *volatile g_teclip = 0;   // рендер-TEClip follower'а (из hook0 при синке ключей на загрузке) — render_sync
static void *volatile g_rl = 0;       // VESequenceRenderLayerImpl* — адресует слои по layerId
static char g_last_layerid[48] = {0}; // последний layerId из RL.setKeyframe (спаривается с g_teclip)
static char g_layerid[48] = {0};      // layerId follower'а в рендере ("TextSticker8") — для RL.setKeyframe
static void *volatile g_req_obj = 0, *volatile g_req_ctrl = 0;   // пойманный Refresh...ReqStruct (shared_ptr)
static volatile long g_req_bool = 0, g_req_sid = 0;              // его bool + sid — для точного реплея
static void *volatile g_req_tramp = 0;   // трамплин той функции (NoFlag или флаговой), что поймала ReqStruct
static long g_ve_base = 0;                            // база libvideoeditor (g[16].addr - 0x4c8fb8) для vtable-офсетов
static long g_creator_base = 0;                       // база libVECreator
static char g_cmd_type[80] = {0};                     // имя класса ReqStruct для захвата (из ve_cpcmd.txt), напр. ScaleSegmentReqStruct
static volatile long g_cmd_off = 0;                   // ЛИБО vtable-офсет (ve_cpcmd.txt = "0x4588ee8") — для команд без своего RTTI
static void *volatile g_cmd_obj = 0, *volatile g_cmd_ctrl = 0; static volatile long g_cmd_sid = 0;
static volatile int g_cp_freeze = 0;   // ve_cpfreeze.txt: перестать перезаписывать слоты (загрузка проекта шлёт свой AddVideo)
static char g_cmd2_type[80] = {0};   // ВТОРОЕ имя из ve_cpcmd.txt («A,B»): живой заворот это ПАРА команд
static void *volatile g_cmd2_obj = 0, *volatile g_cmd2_ctrl = 0;   // пойманный transform-ReqStruct для реплея после corner-pin
// ================= LIVE-DRIVE 3D-камеры: правка слоя-камеры в CapCut -> параллакс на слоях вживую =================
#define RIG_SIGN_Y (-1.0)
#define RIG_ZREF   2.0
#define RIG_FOCAL_FRAC 1.15
#define DEG_R (3.14159265358979323846 / 180.0)
#define RIG_MAXL 48       // всего слоёв по ВСЕМ сценам (было 24 на одну)
#define RIG_MAXK 80
#define RIG_MAXS 8        // макс сцен (камер) в проекте
// easing каждого ключа камеры (render-формат): cubic -> безье-ручки vti/vi (вход) и vto/vo (выход); время µs, значение — в единицах свойства
struct KfEase { int cubic; double vti, vi, vto, vo; };
// СЦЕНА = одна камера + её кривые (px/py/sx/rz) + easing. Слои ОБЩИЕ (g_rig.L[] с тегом .scene), DoF-контроллер ГЛОБАЛЕН.
// Так persist/DoF-резолв/getseg-карта остаются плоскими и нетронутыми — шардим только камерную математику.
static struct Scene {
    char cam_id[80];
    char cam_path[256];              // УНИКАЛЬНЫЙ на сцену путь PNG слоя-камеры — иначе камеры не различить (матч по пути)
    double cam_w0;                   // target_start слоя-камеры (source_start = 0)
    // ПОВОРОТ КАМЕРЫ (эмуляция 3D). pan_rot=1 -> панорама клипа камеры читается не как СДВИГ
    // вбок, а как РАЗВОРОТ на штативе: рыскание/тангаж. Тогда каждый слой ловит свою
    // перспективу — ближний разворачивается сильнее дальнего, и сцена читается объёмной.
    int pan_rot; double pan_gain;    // градусов на единицу панорамы (1.0 = край кадра)
    // РОЛЛ СЦЕНЫ (строка CAMROLL): завал горизонта, задаётся ползунком в инспекторе камеры.
    // Отдельно от кривой rz, потому что драйв читает ролл ТОЛЬКО из кривой ключей, а поставить
    // ключ поворота из панели не выходит — их onClickSetKeyFrame на нашем keyframeData молчит.
    // СКЛАДЫВАЕТСЯ с кривой: кейфреймленный ролл продолжает работать поверх этого значения.
    double cam_roll;
    double pivot_z;                  // глубина точки, ВОКРУГ которой идёт облёт
    double pan_pivot;                // задана пользователем (0 = считать автоматически по слоям)
    void *volatile cam_teclip;       // TEClip камеры этой сцены (сравнение в hook0)
    int camn_px, camn_py, camn_sx, camn_rz;
    double cam_tpx[RIG_MAXK], cam_vpx[RIG_MAXK], cam_tpy[RIG_MAXK], cam_vpy[RIG_MAXK];
    double cam_tsx[RIG_MAXK], cam_vsx[RIG_MAXK], cam_trz[RIG_MAXK], cam_vrz[RIG_MAXK];   // rz = РОЛЛ (градусы)
    KfEase epx[RIG_MAXK], epy[RIG_MAXK], esx[RIG_MAXK], erz[RIG_MAXK];
    // Ручки кривой камеры ИЗ ДОКУМЕНТА (строки CAMEASE в риге). Стабильный источник: рендер-синк
    // отдаёт кубическую лишь эпизодически, а документ держит её всегда. Матчим по времени с
    // ключами камеры и перекрываем epx/epy/esx перед пересадкой на перспективу.
    int docn_px, docn_py, docn_sx;
    double doct_px[RIG_MAXK], doct_py[RIG_MAXK], doct_sx[RIG_MAXK];
    KfEase doce_px[RIG_MAXK], doce_py[RIG_MAXK], doce_sx[RIG_MAXK];
} g_scene[RIG_MAXS];
static int g_nscene = 0;
static struct {
    volatile int have;
    double cw, ch;
    char dof_path[256];              // ГЛОБАЛЬНЫЙ слой-контроллер DoF (один на проект): sx=диафрагма, sy=фокус
    int keepin;                      // «не выходить за рамку»: не давать слою обнажить край кадра
    int nlayer;
    struct { char id[80]; char path[256]; int scene; double z, bx, by, base_scale, base_rot, tstart, sstart, speed;
             double tiltx, tilty;    // наклон плоскости слоя в 3D (градусы): вокруг горизонтальной и вертикальной осей
             void *volatile clip; void *volatile vtable; void *volatile rec_seg; void *volatile rec_vt; } L[RIG_MAXL];
} g_rig;
static char g_clear[RIG_MAXL][80]; static volatile int g_nclear = 0;   // сегменты ОТВЯЗАННЫХ дорожек: снять ключи и забыть
static long g_rig_mt = 0;                  // mtime ve_rig.txt
static long g_cp_mt = 0;                    // mtime ve_cornerpin.txt (перспектива/corner-pin)
static volatile int g_cp_pending = 0;       // файл изменился -> применить; ретраим пока сегмент не зарезолвится
static int g_cp_tries = 0;                   // счётчик ретраев (проект грузится) — сдаёмся после ~40
static void *volatile g_dof_teclip = 0;    // TEClip слоя-контроллера DoF (сравнение в hook0) — ГЛОБАЛЕН (один на все сцены)
// Безье-ручки в ДОКУМЕНТНЫЕ ключи углов и камеры. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО: подозревается в
// краше CapCut сразу после драйва (падал через секунду после первого «CTRL created»).
// Включить для опытов: touch ~/Movies/CapCut/User Data/ve_cpease.txt
static int g_cp_ease = 0;
// Писать ли углы перспективы в ДОКУМЕНТ. ПО УМОЛЧАНИЮ НЕТ, и это защита от краша: типов
// KFTypeCornerPin* НЕТ в UI-таблицах CapCut (gBasicKeyframeTypeList), поэтому его редактор
// кривых/пресетов спотыкается о них на клипе, где они лежат — CapCut падал при «применил
// пресет кривой». Без записи в документ UI никогда их не видит. Перспектива при этом ЖИВАЯ:
// углы уходят в рендер-клип. Включить (для экспорта): touch ve_cpdoc.txt
static int g_cp_doc = 0;
static int g_kfsnap = 0;   // ve_kfsnap.txt: снимать ключ родной командой сразу после cp_apply
static int g_dofn_sx = 0, g_dofn_sy = 0;   // кэш кривых DoF-слоя: sx=диафрагма, sy=фокус (множители к gain из ve_dof.txt)
static double g_dof_tsx[RIG_MAXK], g_dof_vsx[RIG_MAXK], g_dof_tsy[RIG_MAXK], g_dof_vsy[RIG_MAXK];
static KfEase g_dof_esx[RIG_MAXK], g_dof_esy[RIG_MAXK];   // easing ключей фокуса/диафрагмы — фокус анимируют КРИВЫМИ
static volatile int g_driving = 0;         // guard: пишем сами -> не реагировать на свои setKeyframe
static volatile int g_drive_pending = 0;   // hook0 просит пересчёт -> poll диспатчит на main
static volatile int g_diag_done = 0;       // разовая диагностика резолва (сброс в load_rig на новый rig)
static inline double dabs(double x) { return x < 0 ? -x : x; }
// разобрать кривую свойства из value-JSON: "<jk>":[{"v":V,"t":T,...},...] -> ts[],vs[]; вернуть n
static int parse_curve(const char *json, const char *jk, double *ts, double *vs, KfEase *es, int max) {
    char pat[16]; snprintf(pat, sizeof pat, "\"%s\":[", jk);
    const char *p = strstr(json, pat);
    if (!p) return 0;
    p += strlen(pat);
    int n = 0;
    while (n < max) {
        const char *vp = strstr(p, "\"v\":");
        const char *tp = strstr(p, "\"t\":");
        const char *end = strchr(p, '}');
        if (!vp || !tp || !end || vp > end || tp > end) break;
        vs[n] = atof(vp + 4); ts[n] = atof(tp + 4);
        if (es) {   // easing ключа: "it":"cubic" + ручки vti/vi/vto/vo (все внутри {..} этого ключа, до end)
            const char *a; KfEase e = {0, 0, 0, 0, 0};
            a = strstr(p, "\"it\":\"cubic\""); e.cubic = (a && a < end) ? 1 : 0;
            a = strstr(p, "\"vti\":"); if (a && a < end) e.vti = atof(a + 6);
            a = strstr(p, "\"vi\":");  if (a && a < end) e.vi  = atof(a + 5);
            a = strstr(p, "\"vto\":"); if (a && a < end) e.vto = atof(a + 6);
            a = strstr(p, "\"vo\":");  if (a && a < end) e.vo  = atof(a + 5);
            es[n] = e;
        }
        n++;
        p = end + 1;
        if (*p != ',') break;   // ']' или конец
        p++;
    }
    return n;
}


// Сохранить кубическую кривую камеры при ЛИНЕЙНОМ ре-синке. CapCut при добавлении чего-либо в проект
// пересинкивает камеру в render ЛИНЕЙНОЙ формой (флэттит easing) — без этого дилиб затирал бы кривую объектов.
// Тот же счёт ключей + новая вся линейна + старая имела кубическую -> КЕЕП старую; иначе берём новую.
static void merge_ease(KfEase *dst, const KfEase *src, int n, int n_old, const char *pr) {
    int sc = 0, dc = 0;
    for (int i = 0; i < n; i++) if (src[i].cubic) sc = 1;
    for (int i = 0; i < n && i < n_old; i++) if (dst[i].cubic) dc = 1;
    if (n == n_old && n > 0 && !sc && dc) { Lf("%ld EASE preserve %s (линейный ре-синк не затирает кривую)\n", (long)time(NULL), pr); return; }
    for (int i = 0; i < n; i++) dst[i] = src[i];
}
static void test_write(void *clip) {
    if (g_wrote) return;
    if (!g_ready || (long)time(NULL) - g_ready < 5) return;
    if (!g_seq) return;            // ждём первый commit, чтобы поймать секвенцию
    g_wrote = 1;
    // key = "sx" (короткая строка: данные @0, размер в байте 23)
    unsigned char key[24]; memset(key, 0, 24); key[0] = 's'; key[1] = 'x'; key[23] = 2;
    // val = JSON (длинная строка: {char* data@0; size@8; cap|is_long@16})
    static const char *J =
        "{\"v\":\"2.0.0\",\"s\":0,\"e\":9233333,\"t\":0,\"k\":{\"sx\":[{\"v\":2.0,\"t\":0,\"h\":0,\"it\":\"linear\"}]}}";
    size_t L = strlen(J);
    char *heap = (char *)malloc(L + 1); memcpy(heap, J, L + 1);
    unsigned char val[24];
    *(char **)(val + 0) = heap;
    *(uint64_t *)(val + 8) = L;
    *(uint64_t *)(val + 16) = (uint64_t)(L + 1) | (1ULL << 63);
    Lf("%ld WRITE setKeyframe(this=%p, \"sx\", <json v=2.0>)\n", (long)time(NULL), clip);
    ((fwd_t)g[0].tramp)(clip, key, val);   // наша запись, в обход детура
    free(heap);
    seq_commit();                          // commit(секвенция) — синк UI
    Lf("%ld WRITE+COMMIT done seq=%p\n", (long)time(NULL), g_seq);
}
// весь стек вызовов над setKeyframe — где-то там UI-notify после правки
static volatile int g_bt_done = 0;
static void logBacktrace(void) {
    if (g_bt_done) return;
    if (!g_ready || (long)time(NULL) - g_ready < 5) return;
    g_bt_done = 1;
    void **fp = (void **)__builtin_frame_address(0);
    Lf("%ld BACKTRACE над setKeyframe:\n", (long)time(NULL));
    for (int i = 0; i < 14 && fp; i++) {
        void *lr = fp[1];
        if (!lr) break;
        Dl_info di;
        if (dladdr(lr, &di) && di.dli_sname)
            Lf("   #%d %p %s +0x%lx\n", i, lr, di.dli_sname, (long)((char *)lr - (char *)di.dli_saddr));
        else
            Lf("   #%d %p (no-sym)\n", i, lr);
        void **next = (void **)fp[0];
        if (next <= fp) break;
        fp = next;
    }
}
// три тонких хука (каждый знает свой индекс/трамплин), одинаковый ABI x0,x1,x2 -> void
static void readAltString(void *s, char *out, int max);   // forward (hook0 использует)
static void diag_clipid(void *clip, double peak);         // forward: ДИАГ getClipId рендер-клипа
static void *p_getFilePath = 0;   // TEClipBase::getFilePath() — путь исходника (СТАБИЛЬНЫЙ ключ матча объектов)
static void hook0(void *a, void *b, void *c) {
    capture(0, a, b, c);
    // ловим рендер-TEClip follower'а: CapCut синкает документ->рендер через TEClip.setKeyframe(this, "px/py/sx/rz", кривая)
    if (g_ready && b) {
        char pr[8] = {0}; readAltString(b, pr, sizeof pr);
        if (!strcmp(pr, "px") || !strcmp(pr, "py") || !strcmp(pr, "sx") || !strcmp(pr, "rz")) {
            if (g_teclip != a) {
                g_teclip = a;
                strncpy(g_layerid, g_last_layerid, sizeof g_layerid - 1);   // layerId предшествующего RL.setKeyframe
                Lf("%ld TECLIP captured %p prop=%s layerid=%s\n", (long)time(NULL), a, pr, g_layerid);
            }
        }
    }
    // LIVE-DRIVE МАТЧ: и КАМЕРУ, и объекты по ПУТИ ИСХОДНИКА (getFilePath) — стабильно (не зависит от position/scale/
    // rotation/persist/пересборок). Камера = уникальный прозрачный PNG (rz свободен под РОЛЛ).
    if (g_ready && g_rig.have && !g_driving && p_getFilePath && b && c) {
        char pr[8] = {0}; readAltString(b, pr, sizeof pr);
        // Ловим на синке "px" ИЛИ "ul": у слоя с перспективой позиции больше нет (её заменили
        // углы), и по одному только "px" такой клип не находился никогда — живой драйв молча
        // писал в никуда («wrote 0/3»). Путь одинаков для любого свойства.
        if (!strcmp(pr, "px") || !strcmp(pr, "ul")) {
            void *s = ((void *(*)(void *))p_getFilePath)(a);
            char fp[256] = {0}; if (s) readAltString(s, fp, sizeof fp);
            if (fp[0]) {
                // ИНВАРИАНТ «один адрес = одна роль»: CapCut может освободить клип и переиспользовать адрес под другую
                // роль (free+realloc). cam_teclip/dof_teclip не имеют vtable-стража, поэтому при КАЖДОМ матче вычищаем
                // `a` из ЧУЖИХ ролей — иначе стейл cam_teclip==a заставит parse-блок писать чужие движения в кривые камеры.
                int csm = -1;   // СЛОЙ-КАМЕРА какой-то сцены (уникальный путь PNG на сцену)
                for (int s = 0; s < g_nscene; s++)
                    if (g_scene[s].cam_path[0] && !strcmp(fp, g_scene[s].cam_path)) { csm = s; break; }
                if (csm >= 0) {
                    if (g_scene[csm].cam_teclip != a) {
                        g_scene[csm].cam_teclip = a;
                        for (int i = 0; i < g_rig.nlayer; i++) if (g_rig.L[i].clip == a) g_rig.L[i].clip = 0;   // не объект
                        for (int s = 0; s < g_nscene; s++) if (s != csm && g_scene[s].cam_teclip == a) g_scene[s].cam_teclip = 0;   // не другая камера
                        if (g_dof_teclip == a) g_dof_teclip = 0;   // не DoF
                        Lf("%ld MATCH cam(path) scene=%d clip=%p\n", (long)time(NULL), csm, a);
                    }
                } else if (g_rig.dof_path[0] && !strcmp(fp, g_rig.dof_path)) {   // СЛОЙ-КОНТРОЛЛЕР DoF (глобальный)
                    if (g_dof_teclip != a) {
                        g_dof_teclip = a;
                        for (int s = 0; s < g_nscene; s++) if (g_scene[s].cam_teclip == a) g_scene[s].cam_teclip = 0;   // не камера
                        for (int i = 0; i < g_rig.nlayer; i++) if (g_rig.L[i].clip == a) g_rig.L[i].clip = 0;   // не объект
                        Lf("%ld MATCH dof(path) clip=%p\n", (long)time(NULL), a);
                    }
                } else for (int i = 0; i < g_rig.nlayer; i++)   // СЛОЙ-ОБЪЕКТ (всегда обновляем — свежий клип после пересборки)
                    if (!strcmp(fp, g_rig.L[i].path)) {
                        if (g_rig.L[i].clip != a) {
                            g_rig.L[i].clip = a; g_rig.L[i].vtable = *(void **)a;
                            for (int s = 0; s < g_nscene; s++) if (g_scene[s].cam_teclip == a) g_scene[s].cam_teclip = 0;   // адрес переиспользован из камеры -> вычистить (иначе parse-блок испортит кривую камеры)
                            if (g_dof_teclip == a) g_dof_teclip = 0;
                            g_drive_pending = 1;   // клип объекта ПЕРЕСОБРАН (добавили медиа/эффект) -> ре-драйв, иначе живой рендер слетает в линейный
                            Lf("%ld MATCH layer%d clip=%p path=%s\n", (long)time(NULL), i, a, fp);
                        }
                        break;
                    }
            }
        }
    }
    // LIVE-DRIVE: правят СЛОЙ-КАМЕРУ КАКОЙ-ТО сцены -> кэшируем её кривые (px/py/sx/rz) в g_scene[cs], просим пересчёт
    if (g_ready && g_rig.have && !g_driving && b && c) {
        int cs = -1;
        for (int s = 0; s < g_nscene; s++) if (a == g_scene[s].cam_teclip) { cs = s; break; }
        if (cs >= 0) {
            Scene *S = &g_scene[cs];
            char pr[8] = {0}; readAltString(b, pr, sizeof pr);
            char vj[9000]; static KfEase et[RIG_MAXK];   // et = свежий easing из парса; merge_ease решает — брать или сохранить старый
            if (!strcmp(pr, "px"))      { readAltString(c, vj, sizeof vj);
                { static int n = 0; if (n < 6 && strstr(vj, "\"it\"")) { n++;
                    Lf("%ld CAMCURVE px: %.400s\n", (long)time(NULL), vj); } }
                int on = S->camn_px; S->camn_px = parse_curve(vj, "px", S->cam_tpx, S->cam_vpx, et, RIG_MAXK); merge_ease(S->epx, et, S->camn_px, on, "px"); g_drive_pending = 1; }
            else if (!strcmp(pr, "py")) { readAltString(c, vj, sizeof vj); int on = S->camn_py; S->camn_py = parse_curve(vj, "py", S->cam_tpy, S->cam_vpy, et, RIG_MAXK); merge_ease(S->epy, et, S->camn_py, on, "py"); g_drive_pending = 1; }
            else if (!strcmp(pr, "sx")) { readAltString(c, vj, sizeof vj); int on = S->camn_sx; S->camn_sx = parse_curve(vj, "sx", S->cam_tsx, S->cam_vsx, et, RIG_MAXK); merge_ease(S->esx, et, S->camn_sx, on, "sx"); g_drive_pending = 1; }
            else if (!strcmp(pr, "rz")) { readAltString(c, vj, sizeof vj); int on = S->camn_rz; S->camn_rz = parse_curve(vj, "rz", S->cam_trz, S->cam_vrz, et, RIG_MAXK); merge_ease(S->erz, et, S->camn_rz, on, "rz"); g_drive_pending = 1; }   // РОЛЛ камеры
            // ⛔ Масштаб Y камеры БОЛЬШЕ НЕ ФОКУС (2026-08-09, возврат к слою-контроллеру).
            // Здесь стояла ветка, писавшая g_dofn_sy с клипа КАМЕРЫ, а ниже такая же пишет его
            // со СЛОЯ-КОНТРОЛЛЕРА — в одну и ту же переменную. Побеждал тот, кто синкнулся
            // последним, поэтому фокус вёл себя как «крутишь, а ничего не меняется».
            // Один параметр — один источник: теперь только слой.
        }
    }
    // LIVE-DRIVE: правят СЛОЙ-КОНТРОЛЛЕР DoF -> кэшируем sx(диафрагма)/sy(фокус), просим пересчёт
    if (g_ready && g_rig.have && !g_driving && g_dof_teclip && a == g_dof_teclip && b && c) {
        char pr[8] = {0}; readAltString(b, pr, sizeof pr);
        char vj[9000];
        if (!strcmp(pr, "sx"))      { readAltString(c, vj, sizeof vj); g_dofn_sx = parse_curve(vj, "sx", g_dof_tsx, g_dof_vsx, g_dof_esx, RIG_MAXK); g_drive_pending = 1; }
        else if (!strcmp(pr, "sy")) { readAltString(c, vj, sizeof vj); g_dofn_sy = parse_curve(vj, "sy", g_dof_tsy, g_dof_vsy, g_dof_esy, RIG_MAXK); g_drive_pending = 1; }
    }
    ((fwd_t)g[0].tramp)(a, b, c);
}
static void hook1(void *a, void *b, void *c) {   // RL.setKeyframe(this, shared_ptr<Kf>, layerId) — ловим layerId (x2)
    capture(1, a, b, c);
    if (g_ready && c) readAltString(c, g_last_layerid, sizeof g_last_layerid);
    // Сам render-layer: через него можно ставить ключи ПО layerId (строке), а не по указателю
    // на клип — то есть без всей возни с протухающими указателями.
    if (a && g_rl != a) { g_rl = a; Lf("%ld RL поймана this=%p\n", (long)time(NULL), a); }
    ((fwd_t)g[1].tramp)(a, b, c);
}
static void hook2(void *a, void *b, void *c) { g_hk[2]++; capture(2, a, b, c); ((fwd_t)g[2].tramp)(a, b, c); }
// Смена секвенции = проект перезагрузили ⇒ ВСЕ пойманные рендер-указатели протухли.
// Вынесено из hook3: теперь секвенция приходит из ДВУХ мест, и оба обязаны соблюдать инвариант.
static void seq_adopt(void *a) {
    if (!a || a == g_seq) { g_seq = a ? a : g_seq; return; }
    if (g_seq) { g_teclip = 0; g_layerid[0] = 0; g_qml_dead = 1;
        Lf("%ld SEQ СМЕНИЛАСЬ %p -> %p: сбрасываю пойманные клип/wrapper + вкладку\n",
           (long)time(NULL), g_seq, a); }
    g_seq = a;
}
static void hook3(void *a, void *b, void *c) {
    g_hk[3]++;
    // СМЕНИЛАСЬ СЕКВЕНЦИЯ = проект перезагрузили -> ВСЕ пойманные рендер-указатели протухли.
    // Без этого g_teclip оставался с ПРОШЛОЙ загрузки: has_vtable проходил (освобождённая память
    // ещё похожа на объект), getClipId отдавал устаревший id, а setKeyframe в мёртвый клип
    // убивал CapCut. Это и есть тот самый «pure virtual» из старых заметок.
    seq_adopt(a);
    capture(3, a, b, c); ((fwd_t)g[3].tramp)(a, b, c);
}
// Client-команды возвращают через x8 (sret) -> обычный C-хук ломает. ASM-трамплин сохраняет
// x0-x8+x30, логирует через client_log, потом tail-jump в оригинальный трамплин (x8 цел).
static volatile int g_req_cnt[8] = {0};
extern "C" void *g_tramp4 = 0, *g_tramp5 = 0, *g_tramp7 = 0, *g_tramp_skf = 0, *g_tramp_srv = 0;
extern "C" void *g_tramp_cv = 0;      // оригинал convert_clip_to_ve
extern "C" void *g_tramp_mkr = 0;     // оригинал makeReq
extern "C" void asm_thunk_mkr(void);
extern "C" long long g_playhead = 0;  // СЫРОЕ время (x2, мкс) из KeyframePropertyHelper::getPropertyValues
// ...и ДЛЯ КАКОГО сегмента (x0 = &shared_ptr<Segment>). Движок зовёт вычислитель для многих сегментов
// и на разных временах (пре-рендер, кэш), поэтому без фильтра g_playhead дрожит: драг пишет ключ на T1,
// а co_main через тик интерполирует на T2 -> ручка отскакивает к чужому значению.
// РАЗЫМЕНОВЫВАТЬ x0 ОБЯЗАНЫ ПРЯМО В ТРАМПЛИНЕ: shared_ptr передаётся по значению => НЕтривиальный тип =>
// Itanium ABI отдаёт его КОСВЕННО, x0 = адрес ВРЕМЕННОГО В СТЕКЕ ВЫЗЫВАЮЩЕГО. Он жив только на время
// вызова; co_main читал его тиком позже с другого потока — т.е. мёртвый кадр стека. Отсюда evseg=0x0
// в каждой строке PHSEG (sane_ptr был ни при чём).
extern "C" void *g_ph_seg = 0;        // Segment*, разыменованный НА МОМЕНТ вызова (сырой, для диагностики)
extern "C" void *g_cp_seg_want = 0;   // наш сегмент; трамплин латчит время ТОЛЬКО когда совпало
extern "C" long long g_ph_ours = -1;  // время НАШЕГО сегмента. -1 = совпадения не было ни разу
extern "C" void asm_thunk_cv(void);
// Пара (время, сегмент) защёлкивается за ОДИН вызов внутри трамплина, поэтому расщепления между потоками
// нет. Фолбэк на сырое время нужен, пока совпадения не случилось: иначе g_ph_ours навсегда 0 и ВСЁ
// пишется в первый ключ («меняю на втором — применяется на первом»).
static long long cp_playhead(void) { return g_ph_ours >= 0 ? g_ph_ours : g_playhead; }
extern "C" void *g_last_seg = 0;   // последний непустой Segment, возвращённый get_segment (= выделенный follower)
static void *g_drafts[8][2]; static int g_ndrafts = 0;   // кеш РАЗЛИЧНЫХ живых Draft (root + суб-драфты)
static void readAltString(void *s, char *out, int max);   // forward
// НАСТОЯЩИЙ экранный wrapper (ловим из setKeyFrame-хука ниже; надёжнее onFrameRendered). Определён здесь,
// т.к. client_log использует его раньше по тексту.
static void *volatile g_screen_wrapper = 0;
// Живая сессия: ловим в хуке lyra::Server::invoke (idx 16) — там оба уже проходят аргументами.
// getSession — это getSession(long sid), НЕ getSession(): без живого sid цепочка бесполезна.
static void *volatile g_srv = 0;
static volatile long g_sid = 0;
static char g_selected_segid[48] = {0};   // id выделенного в CapCut сегмента
// id выделенного сегмента из GetSegmentCurrentAlgorithmReqStruct: строка std::string на ФИКС-офсете 72 (проверено дампом).
// БЕЗ сканирования офсетов — дереф только известного поля (безопасно; сканирование ронело CapCut на бэд-указателях).
static int extract_selected_segid(void *req, const char *tn, char *out, int max) {
    if (!req || !tn || !strstr(tn, "GetSegmentCurrentAlgorithm")) return 0;
    unsigned char *p = (unsigned char *)req; int off = 72;
    if (*(unsigned long *)(p + off + 8) != 36) return 0;                    // size==36 подтверждает строку-id
    unsigned long cap = *(unsigned long *)(p + off + 16);
    if (!(cap & 0x8000000000000000UL)) return 0;                           // long-строка
    char *data = *(char **)(p + off);
    if ((unsigned long)data < 0x100000000UL || (unsigned long)data > 0x7fffffffffffUL) return 0;
    for (int i = 0; i < 36; i++) { char c = data[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (c != '-') return 0; }
        else if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f'))) return 0; }
    int n = 36 < max - 1 ? 36 : max - 1; memcpy(out, data, n); out[n] = 0; return 1;
}
static int is_cstr(void *p) {   // грубо: указатель в мапнутое и первые байты печатные
    if (!p || (uintptr_t)p < 0x1000) return 0;
    const char *c = (const char *)p;
    for (int i = 0; i < 5; i++) { if (c[i] == 0) return i > 0; if (c[i] < 32 || c[i] > 126) return 0; }
    return 1;
}
static int sane_ptr(void *p);
static int has_vtable(void *obj);
// Безопасное чтение libc++-строки из ПРОИЗВОЛЬНОГО места объекта: сначала проверяем раскладку,
// и только потом разыменовываем. Без этой проверки дамп чужого поля = bus error.
static int try_read_str(const unsigned char *p, char *out, int max, unsigned long long *len_out) {
    unsigned char b23 = p[23];
    if (b23 & 0x80) {                                   // длинная: ptr@0 size@8 cap|1<<63 @16
        const char *data = *(const char *const *)p;
        unsigned long long size = *(const unsigned long long *)(p + 8);
        unsigned long long cap  = *(const unsigned long long *)(p + 16);
        if (!(cap & (1ULL << 63))) return 0;
        if (size == 0 || size > (1ULL << 26)) return 0;
        if (!sane_ptr((void *)data)) return 0;
        if (len_out) *len_out = size;
        int n = size < (unsigned long long)(max - 1) ? (int)size : max - 1;
        memcpy(out, data, n); out[n] = 0;
    } else {                                            // короткая: данные на месте, длина в байте 23
        int n = b23 & 0x7f;
        if (n == 0 || n > 22) return 0;
        if (len_out) *len_out = (unsigned long long)n;
        memcpy(out, p, n); out[n] = 0;
    }
    for (int i = 0; out[i]; i++) { unsigned char c = (unsigned char)out[i];
        if (c < 9 || (c > 13 && c < 32) || c == 127) return 0; }
    return 1;
}
// Дамп пойманного ReqStruct: строки с длинами (для draft_content_str длина — главный ответ),
// плюс правдоподобные целые. Границу держим в 0x200 — читаем поле, а не гуляем по памяти.
static void dump_req_fields(void *req) {
    if (!has_vtable(req)) { Lf("%ld REQDUMP: объект не похож на объект\n", (long)time(NULL)); return; }
    const unsigned char *p = (const unsigned char *)req;
    Lf("%ld REQDUMP obj=%p\n", (long)time(NULL), req);
    for (int off = 8; off <= 0x200; off += 8) {
        char sv[160]; unsigned long long len = 0;
        if (try_read_str(p + off, sv, sizeof sv, &len)) {
            Lf("   +%03x строка len=%llu: \"%s\"%s\n", off, len, sv, len > 150 ? " …" : "");
            continue;
        }
        long long i; memcpy(&i, p + off, 8);
        if (i > 0 && i < 100000000LL) Lf("   +%03x int=%lld\n", off, i);
    }
    // Сырые слова вокруг строковых полей: ПУСТАЯ строка это 24 нуля, и в разборе выше она
    // неотличима от отсутствия поля. Главный вопрос замера — пуст ли draft_content_str,
    // поэтому печатаем область как есть и считаем поля по схеме от якоря (name).
    for (int off = 0xe0; off < 0x220; off += 32) {
        unsigned long long w[4];
        memcpy(w, p + off, 32);
        Lf("   raw +%03x: %016llx %016llx %016llx %016llx\n", off, w[0], w[1], w[2], w[3]);
    }
}
extern "C" void client_log(long idx, void *self, void *a1, void *a2) {
    if (idx == 19) {   // makeReq: разово печатаем ВСЕ три регистра — какой из них service/api видно сразу
        static int n = 0;
        if (n < 12) { n++;
            Lf("MAKEREQ x0=%s x1=%s x2=%s\n",
               is_cstr(self) ? (char *)self : "(не строка)",
               is_cstr(a1)   ? (char *)a1   : "(не строка)",
               is_cstr(a2)   ? (char *)a2   : "(не строка)"); }
        return;
    }
    if (!g_ready || (long)time(NULL) - g_ready < 5) return;
    if (idx == 15) {   // NewVEWrapper::setKeyFrame(this=wrapper, x1=&shptr<Keyframe>, x2=&layerId)
        g_screen_wrapper = self;   // ← НАСТОЯЩИЙ экранный wrapper (надёжнее onFrameRendered)
        static volatile int skf_cnt = 0;
        if (skf_cnt >= 6) return;  // первые несколько правок хватит для анализа
        skf_cnt++;
        void *kf = a1 ? *(void **)a1 : 0;               // shared_ptr.ptr = vesdk::pub::Keyframe*
        char layer[64] = {0}; if (a2) readAltString(a2, layer, sizeof layer);
        void *seq408 = self ? *(void **)((char *)self + 0x408) : 0;
        Lf("%ld SKF wrapper=%p seq@408=%p layerId='%s' kf=%p\n", (long)time(NULL), self, seq408, layer, kf);
        if (kf) {   // сырые байты Keyframe — там id (роутинг!) + json кривой (alt-layout строки)
            const uint8_t *p = (const uint8_t *)kf;
            for (int row = 0; row < 14; row++) {
                char h[112]; int n = 0; char asci[20];
                for (int k = 0; k < 16; k++) { uint8_t b = p[row*16+k]; n += snprintf(h+n, sizeof(h)-n, "%02x ", b);
                    asci[k] = (b>=32&&b<127)?(char)b:'.'; }
                asci[16]=0;
                Lf("   kf+%02x: %s |%s|\n", row*16, h, asci);
            }
        }
        return;
    }
    if (idx == 7) {                          // get_segment: копим РАЗЛИЧНЫЕ живые Draft
        void *dp = self ? ((void **)self)[0] : 0;
        if (dp) {
            int known = 0;
            for (int i = 0; i < g_ndrafts; i++) if (g_drafts[i][0] == dp) { known = 1; break; }
            if (!known && g_ndrafts < 8) {
                g_drafts[g_ndrafts][0] = dp; g_drafts[g_ndrafts][1] = ((void **)self)[1];
                Lf("%ld DRAFT[%d] dp=%p\n", (long)time(NULL), g_ndrafts, dp);
                g_ndrafts++;
            }
        }
        return;
    }
    if (idx == 16) {   // Server::invoke(server, &req, sid): self=server, a1=&shptr<ReqStruct>, a2=sid.
        // ЖИВОЙ sid сессии: getSession(long) требует именно его. Раньше get_wrapper() подставлял 0
        // → чужая/пустая сессия. Ловим на КАЖДОМ invoke, а не только на команде из ve_cpcmd.txt.
        { static long seen[8] = {0}; static int ns = 0;   // РАЗВЕДКА: сколько сессий на шине.
          long sd = (long)a2; int known = 0;                // вложенный драфт компаунда живёт СВОЕЙ
          for (int i = 0; i < ns; i++) if (seen[i] == sd) { known = 1; break; }
          if (!known && ns < 8) { seen[ns++] = sd;
              Lf("%ld SID новый: %ld (всего сессий видно %d)\n", (long)time(NULL), sd, ns); } }
        g_sid = (long)a2; g_srv = self;
        void *req = a1 ? ((void **)a1)[0] : 0, *ctrl = a1 ? ((void **)a1)[1] : 0, *vt = req ? *(void **)req : 0;
        const char *tn = 0;   // имя класса ReqStruct из RTTI: typeinfo=*(vt-8), name=*(typeinfo+8)
        if (vt) { void *tip = ((void **)vt)[-1]; if (tip) tn = ((const char **)tip)[1]; }
        if (g_trace) { static volatile int sc = 0; if (sc < 80) { sc++; long off = (g_ve_base && vt) ? ((long)vt - g_ve_base) : -1;
            Lf("%ld SRVINVOKE off=0x%lx sid=%ld type=%s\n", (long)time(NULL), off, (long)a2, tn ? tn : "?"); } }
        if (tn && req) {   // id выделенного из GetSegmentCurrentAlgorithm (extract сам фильтрует тип + фикс-офсет)
            char sid[48]; if (extract_selected_segid(req, tn, sid, sizeof sid)) { memcpy(g_selected_segid, sid, sizeof g_selected_segid);
                static volatile int scg = 0; if (scg < 4) { scg++; Lf("%ld SELSEG=%s\n", (long)time(NULL), sid); } }
        }
        // Команду РОДНОГО трекинга по имени не поймать: своего typeinfo у неё нет, RTTI отдаёт
        // базовый lyra::ReqStruct. Опознаём по vtable-офсету (замер: 0x4588ee8, 32 вызова за трек).
        // В ve_cpcmd.txt кладём "0x4588ee8" вместо имени класса.
        long voff = (g_ve_base && vt) ? ((long)vt - g_ve_base) : -1;
        int hit = g_cmd_off ? (voff == g_cmd_off)
                            : (g_cmd_type[0] && tn && strstr(tn, g_cmd_type) != 0);
        if (!g_cp_freeze && !hit && g_cmd2_type[0] && tn && strstr(tn, g_cmd2_type) && req && ctrl) {   // ВТОРАЯ команда пары
            __atomic_fetch_add((long *)((char *)ctrl + 8), 1, __ATOMIC_RELAXED);   // retain
            void *old2 = g_cmd2_ctrl; g_cmd2_obj = req; g_cmd2_ctrl = ctrl;
            if (old2 && old2 != ctrl) __atomic_fetch_add((long *)((char *)old2 + 8), -1, __ATOMIC_RELAXED);
            { static volatile int l2 = 0; if (l2 < 2) { l2++;
                Lf("%ld CMDCAP2 type=%s req=%p sid=%ld\n", (long)time(NULL), tn, req, (long)a2);
                dump_req_fields(req); } }
        }
        if (hit && req && ctrl && !g_cp_freeze) {   // захват (последняя выигрывает)
            { static volatile int lc = 0; if (lc < 3) { lc++;
                Lf("%ld CMDCAP off=0x%lx type=%s req=%p sid=%ld\n", (long)time(NULL), voff, tn ? tn : "?", req, (long)a2);
                dump_req_fields(req); } }
            __atomic_fetch_add((long *)((char *)ctrl + 8), 1, __ATOMIC_RELAXED);   // retain
            void *oldc = g_cmd_ctrl; g_cmd_obj = req; g_cmd_ctrl = ctrl; g_cmd_sid = (long)a2;
            if (oldc && oldc != ctrl) __atomic_fetch_add((long *)((char *)oldc + 8), -1, __ATOMIC_RELAXED);   // release prev
        }
        return;
    }
    if (g_req_cnt[idx] >= 3) return;
    g_req_cnt[idx]++;
    void *req = self ? *(void **)self : 0;   // shared_ptr.ptr = ReqStruct*
    Lf("%ld CREQ %s spptr=%p id=%ld req=%p\n", (long)time(NULL), g[idx].name, self, (long)a1, req);
    if (req) {
        const uint8_t *p = (const uint8_t *)req;
        for (int row = 0; row < 12; row++) {
            char h[80]; int n = 0;
            for (int k = 0; k < 16; k++) n += snprintf(h + n, sizeof(h) - n, "%02x ", p[row * 16 + k]);
            Lf("   req+%02x: %s\n", row * 16, h);
        }
    }
}
// ТАРГЕТИНГ follower'а ПО ID (детерминистично; g_last_seg шумит — CapCut зовёт get_segment и на исходник)
static char g_want_id[80] = {0};           // disk-id follower'а из ve_curve.txt (строка SEGMENT ...)
static void *volatile g_follower_seg = 0;  // живой Segment follower'а (по совпадению id)
static char g_id_str[64][48]; static void *g_id_seg[64]; static void *g_id_vt[64]; static volatile int g_nid = 0;  // карта live id->Segment (+vtable сегмента для стража от use-after-free после компаунда)
static char g_id_path[64][256]; static unsigned char g_id_pres[64];   // путь файла сегмента (ленивый резолв) для матча ПО ПУТИ после компаунда (id меняется, путь — нет)
static void *g_id_draft[64][2];   // владелец-Draft (сабдрафт компаунда) на каждую запись карты — для DoF: get_all_segments(сабдрафт)->эффект того же объекта
static long g_id_touch[64]; static long g_touch_ctr = 0;   // «когда последний раз CapCut запросил этот сегмент» — живой рендерится=запрашивается, стейл-дубль нет (seg_by_path берёт самый свежий)
static char g_live_id[RIG_MAXL][48];   // живой video-seg-id объекта из ve_segmap.txt (пишет dof.py из файла) — резолвим ПО НЕМУ, обходя стрей-дубли того же пути
static void *volatile g_segvideo_vt = 0;   // vtable SegmentVideo — get_material зовём ТОЛЬКО на сегментах с ним (иначе на чужом типе — мусор/краш)
// зовётся из asm_thunk7 на КАЖДЫЙ get_segment: id_str = x1 (const std::string&), seg = возврат.
// Строим карту id->seg (follower запрашивается на ЗАГРУЗКЕ -> при arming найдём его в карте сразу).
extern "C" void getseg_capture(void *id_str, void *seg, void *draft_ref) {
    if (seg) g_last_seg = seg;
    if (!seg || !id_str || !g_ready) return;
    void *dp = draft_ref ? ((void **)draft_ref)[0] : 0, *dc = draft_ref ? ((void **)draft_ref)[1] : 0;   // живой Draft-владелец (root/субдрафт)
    if (dp) { int known = 0; for (int i = 0; i < g_ndrafts; i++) if (g_drafts[i][0] == dp) { known = 1; break; }
              if (!known && g_ndrafts < 8) { g_drafts[g_ndrafts][0] = dp; g_drafts[g_ndrafts][1] = dc;
                  Lf("%ld DRAFT[%d] dp=%p\n", (long)time(NULL), g_ndrafts, dp); g_ndrafts++; } }
    char id[48]; readAltString(id_str, id, sizeof(id));
    if (!id[0]) return;
    if (g_want_id[0] && strcmp(id, g_want_id) == 0) g_follower_seg = seg;   // follower прямо сейчас
    void *vt = *(void **)seg;   // seg живой (только что вернул get_segment) -> vtable читать безопасно
    for (int i = 0; i < g_nid; i++) if (strcmp(g_id_str[i], id) == 0) { g_id_seg[i] = seg; g_id_vt[i] = vt; g_id_touch[i] = ++g_touch_ctr; if (dp) { g_id_draft[i][0] = dp; g_id_draft[i][1] = dc; } return; }
    if (g_nid < 64) {
        strncpy(g_id_str[g_nid], id, 47); g_id_str[g_nid][47] = 0; g_id_seg[g_nid] = seg; g_id_vt[g_nid] = vt; g_id_pres[g_nid] = 0;
        g_id_draft[g_nid][0] = dp; g_id_draft[g_nid][1] = dc; g_id_touch[g_nid] = ++g_touch_ctr;
        Lf("%ld GETSEG id=%s seg=%p\n", (long)time(NULL), id, seg); g_nid++;
    }
}
extern "C" void asm_thunk4(void);
extern "C" void asm_thunk5(void);
extern "C" void asm_thunk7(void);
extern "C" void asm_thunk_skf(void);   // NewVEWrapper::setKeyFrame — sret-safe, ловит wrapper+kf+layerId
extern "C" void asm_thunk_srv(void);   // lyra::Server::invoke — sret-safe, перехват всех команд
__asm__(
"   .text\n   .p2align 2\n   .globl _asm_thunk4\n"
"_asm_thunk4:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #4\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _client_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_tramp4@PAGE\n   ldr  x16, [x16, _g_tramp4@PAGEOFF]\n   br   x16\n"
"   .p2align 2\n   .globl _asm_thunk5\n"
"_asm_thunk5:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #5\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _client_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_tramp5@PAGE\n   ldr  x16, [x16, _g_tramp5@PAGEOFF]\n   br   x16\n"
"   .p2align 2\n   .globl _asm_thunk_skf\n"    // NewVEWrapper::setKeyFrame (sret) — лог (this,kf,layerId), потом оригинал
"_asm_thunk_skf:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #15\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _client_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_tramp_skf@PAGE\n   ldr  x16, [x16, _g_tramp_skf@PAGEOFF]\n   br   x16\n"
// ПЛЕЙХЕД: сохраняем x2 (время) + СРАЗУ разыменовываем x0 (&shared_ptr<Segment> -> Segment*) и латчим
// время, только если движок считает НАШ сегмент. Разыменование обязано быть здесь: x0 указывает на
// временный shared_ptr в стеке вызывающего, снаружи он уже мёртв. Фильтр тоже здесь — так пара
// (время, сегмент) берётся из ОДНОГО вызова и её нечем расщепить между потоками.
// x16/x17 (IP0/IP1) — законный скретч для веника, так что x0-x8 и d0-d7 целы (функция sret, x8 обязан выжить).
"   .p2align 2\n   .globl _asm_thunk_cv\n"
"_asm_thunk_cv:\n"
"   adrp x17, _g_playhead@PAGE\n   add  x17, x17, _g_playhead@PAGEOFF\n   str  x2, [x17]\n"
"   cbz  x0, Lcv_go\n"
"   ldr  x16, [x0]\n"                                  // Segment* = *(shared_ptr<Segment>*)x0
"   adrp x17, _g_ph_seg@PAGE\n   add  x17, x17, _g_ph_seg@PAGEOFF\n   str  x16, [x17]\n"
"   cbz  x16, Lcv_go\n"
"   adrp x17, _g_cp_seg_want@PAGE\n   add  x17, x17, _g_cp_seg_want@PAGEOFF\n   ldr  x17, [x17]\n"
"   cmp  x16, x17\n   b.ne Lcv_go\n"
"   adrp x17, _g_ph_ours@PAGE\n   add  x17, x17, _g_ph_ours@PAGEOFF\n   str  x2, [x17]\n"
"Lcv_go:\n"
"   adrp x17, _g_tramp_cv@PAGE\n   ldr  x17, [x17, _g_tramp_cv@PAGEOFF]\n   br   x17\n"
"   .p2align 2\n   .globl _asm_thunk_mkr\n"   // makeReq (sret): client_log(19,x0,x1,x2), потом оригинал
"_asm_thunk_mkr:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #19\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _client_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_tramp_mkr@PAGE\n   ldr  x16, [x16, _g_tramp_mkr@PAGEOFF]\n   br   x16\n"
"   .p2align 2\n   .globl _asm_thunk_srv\n"   // lyra::Server::invoke (sret) — client_log(16,server,&req,sid), потом оригинал (x8 цел)
"_asm_thunk_srv:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #16\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _client_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_tramp_srv@PAGE\n   ldr  x16, [x16, _g_tramp_srv@PAGEOFF]\n   br   x16\n"
"   .p2align 2\n   .globl _asm_thunk7\n"      // wrap get_segment: ловим id(x2)+ВОЗВРАТ(Segment) -> getseg_capture
"_asm_thunk7:\n"
"   stp  x19, x20, [sp, #-48]!\n"
"   stp  x21, x30, [sp, #16]\n"
"   str  x22, [sp, #32]\n"                     // сохранить x22 (callee-saved) — под &shared_ptr<Draft>
"   mov  x19, x8\n"                            // x19 = буфер sret (там будет shared_ptr<Segment>)
"   mov  x20, x1\n"                            // x20 = id (x0 = &shared_ptr<Draft>, id в x1, bool в x2)
"   mov  x22, x0\n"                            // x22 = &shared_ptr<Draft> (полный: {ptr,ctrl}) для захвата
"   adrp x16, _g_tramp7@PAGE\n   ldr  x16, [x16, _g_tramp7@PAGEOFF]\n"
"   blr  x16\n"                                // оригинал -> пишет [x8], ВОЗВРАЩАЕТ x0 = буфер sret
"   mov  x21, x0\n"                            // СОХРАНИТЬ возврат (x0) — иначе вызывающий получит мусор
"   ldr  x1, [x19]\n"                          // x1 = Segment* (shared_ptr.ptr)
"   mov  x0, x20\n"                            // x0 = id ptr
"   mov  x2, x22\n"                            // x2 = &shared_ptr<Draft>
"   bl   _getseg_capture\n"                    // getseg_capture(id, seg, draft_ref)
"   mov  x0, x21\n"                            // ВОССТАНОВИТЬ возврат
"   ldr  x22, [sp, #32]\n"
"   ldp  x21, x30, [sp, #16]\n"
"   ldp  x19, x20, [sp], #48\n"
"   ret\n"
"   .p2align 2\n   .globl _call_sret2\n"      // x0=sret буфер, x1=this, x2=arg1, x3=fn
"_call_sret2:\n   mov  x8, x0\n   mov  x0, x1\n   mov  x1, x2\n   br   x3\n"
"   .p2align 2\n   .globl _call_sret3\n"      // x0=sret, x1=a0, x2=a1, x3=a2, x4=fn
"_call_sret3:\n   mov  x8, x0\n   mov  x0, x1\n   mov  x1, x2\n   mov  x2, x3\n   br   x4\n"
"   .p2align 2\n   .globl _call_sret4\n"      // x0=sret, x1=a0, x2=a1, x3=a2, x4=a3, x5=fn
"_call_sret4:\n   mov  x8, x0\n   mov  x0, x1\n   mov  x1, x2\n   mov  x2, x3\n   mov  x3, x4\n   br   x5\n"
"   .p2align 2\n   .globl _call_createpoint\n"   // x0=sret, d0=x, d1=y (double-арги уже в FP-регистрах), x1=fn
"_call_createpoint:\n   mov  x8, x0\n   br   x1\n"
);
// insertKeyframe (без sret): ловим живые CommonKeyframes(трек клипа) + CommonKeyframe
static void dumpObj(const char *tag, void *obj, int rows) {
    if (!obj) { Lf("   %s=<null>\n", tag); return; }
    const uint8_t *p = (const uint8_t *)obj;
    for (int r = 0; r < rows; r++) {
        char h[80]; int n = 0;
        for (int k = 0; k < 16; k++) n += snprintf(h + n, sizeof(h) - n, "%02x ", p[r * 16 + k]);
        Lf("   %s+%02x: %s\n", tag, r * 16, h);
    }
}
static volatile int g_ins_cnt = 0;
typedef void (*setto_t)(void *kf, const long long *t);   // CommonKeyframe::set_time_offset
typedef void (*setv_t)(void *kf, const void *vec);       // CommonKeyframe::set_values(vector<double> const&)
static setto_t p_set_time_offset = 0;                    // резолвятся в worker
static setv_t p_set_values = 0;
// easing документных ключей (этап 2): тип кривой + безье-ручки (control-точки клонированного ключа правим на месте)
static void *p_set_curveType = 0;     // CommonKeyframe::set_curveType(LVVEKeyframeCurveType const&)  — enum по ссылке
static void *p_get_left_control = 0;  // CommonKeyframe::get_left_control() const — sret shptr<CommonPoint>
static void *p_get_right_control = 0; // CommonKeyframe::get_right_control() const — sret shptr<CommonPoint>
static void *p_cp_set_x = 0, *p_cp_set_y = 0;   // CommonPoint::set_x/set_y(double const&) — время µs / значение
static void *p_str2curve = 0;         // DraftEnumTransfer::TransferStringToLVVEKeyframeCurveType(string const&) -> enum
static volatile int g_ct_freecurve = -1;   // кэш значения enum FreeCurveInOut
static void *p_seg_get_material = 0;  // SegmentVideo::get_material() const -> sret shptr<Material> (сегмент->материал) [null у query-сегментов]
static void *p_mat_get_path = 0;      // MaterialVideo::get_path() const -> sret string (материал->путь)
static void *p_matpath_by_draft = 0;  // DraftQuery::get_material_video_path_by_draft(shptr<Draft>, id) -> sret string путь (резолвит по id через draft)
static void *p_get_segs_recursive = 0;  // DraftQuery::get_segments_recursive(shptr<Draft>, LVVETrackType, bool) -> sret vector<shptr<Segment>> (все живые, вкл. субдрафты)
static void *p_get_all_segments = 0;    // DraftQuery::get_all_segments(shptr<Draft>) -> sret vector<shptr<Segment>> (сегменты ЭТОГО драфта, вкл. effect-треки)
static void *p_get_all_subdraft = 0;    // DraftQuery::GetAllSubDraft(shptr<Draft>, bool) -> sret vector<shptr<Draft>> (вернул пусто — не компаунд-сабдрафты)
static void *p_getall_comb = 0;         // DraftQuery::getAllCombinationSegment(shptr<Draft>, bool) -> sret vector<shptr<Segment>> (внешние компаунд-сегменты)
static void *p_getsubdraft = 0;         // DraftQuery::getSubDraft(shptr<Segment>) -> sret shptr<Draft> (компаунд-сегмент -> его сабдрафт)
static void *p_getseg_and_draft = 0;    // DraftQuery::get_segment_and_cur_draft(root, id, &outSeg, &outDraft, tt, mt) -> по id: сегмент + его субдрафт
static void *volatile g_segeffect_vt = 0;  // vtable SegmentVideoEffect — блюр-ключи гоним ТОЛЬКО на нём (common_keyframes на 0xa8 — базовый Segment, тот же что у видео)
static double g_dof_focus = 1.96, g_dof_aperture = 0.3;   // фокусное расст. (в единицах z) и сила; правятся из ve_dof.txt
extern "C" void call_sret2(void *sret, void *self, long a1, void *fn);
extern "C" void call_sret3(void *sret, void *a0, void *a1, void *a2, void *fn);
extern "C" void call_sret4(void *sret, void *a0, void *a1, void *a2, void *a3, void *fn);
// АВТОНОМНЫЙ захват render-TEClip follower'а по doc-id (обходит load-only g_teclip): как это делает
// vesdk::VESequenceImpl::getClipById — getNativeSequence(seq)->TESequence, потом getClipWithClipID(id).
static void *p_getNativeSequence = 0;   // VESequenceImpl::getNativeSequence() — sret shptr<TESequence>
static void *p_getClipWithClipID = 0;   // TESequence::getClipWithClipID(string&,int*,int*) — sret shptr<TEClip>
static void *p_getClipById = 0;         // TESequence::getClipById(string&) — sret shptr<TEClip> (иной резолв?)
static void *p_getClipAndSeqById = 0;   // TESequence::getClipAndSeqById(string&) — sret {clip,seq}
static void *p_vseqGetClipById = 0;     // vesdk::VESequenceImpl::getClipById(string&) — ВЫСОКОУРОВНЕВЫЙ (doc->render?)
static void *p_getClipId_TE = 0;        // TEClipBase::getClipId() const — render-id «videoN» (нестабилен)
static void *p_originID = 0;            // TEClipBase::originID() const — u64 (стабильный id origin?)
// p_getFilePath объявлен ВЫШЕ (до hook0 — там матч по пути)
static void *p_deep_copy = 0;                            // CommonKeyframe::deep_copy(bool) — sret shared_ptr
static void *p_get_segment = 0;                          // DraftQuery::get_segment(shptr<Draft>, id, bool) — sret shptr<Segment>
static void *p_get_segment_recursive = 0;                // DraftQuery::get_segment_recursive(shptr<Draft>, id) — sret
// автосоздание треков follower'а
static void *p_deepcopy_kfs = 0;      // CommonKeyframes::deep_copy(bool) — sret shptr<CommonKeyframes>
static void *p_set_property_type = 0; // CommonKeyframes::set_property_type(string&)
static void *p_set_keyframe_list = 0; // CommonKeyframes::set_keyframe_list(vector&)
static void *p_insertOrUpdate = 0;    // KeyframeUtils::insertOrUpdateKeyframes(&coll, &prop, shptr<track>)
static void *p_remove_kfs = 0;        // KeyframeUtils::removeKeyframes(&coll, &prop) — снос трека целиком
static void *p_prcache_get = 0, *p_prcache_clear = 0;   // сброс prerender-кэша -> ре-рендер превью
static void *p_refresh = 0;   // (не исп.) PlayerWin::RefreshCurrentFrame — плеер не поймать (pcrel/не зовётся)
static void *p_getEditor = 0, *p_getPreviewControl = 0, *p_refreshCF = 0;  // vesdk: g_seq->editor->preview->refresh
// РЕНДЕР-СИНК: экранный рендерер = lvve::NewVEWrapper (IS-A PlayerWin). Реплицируем ручной drag ключа:
// пушим кривую в его рендер-секвенцию через setKeyFrame -> ставит dirty[wrap+0x42e] -> НАСТОЯЩИЙ ре-рендер.
static void *p_server_instance = 0, *p_get_session = 0, *p_get_vewrapper = 0;
static void *p_wrap_setkf = 0, *p_vkf_ctor = 0, *p_vkf_set_json = 0, *p_vkf_set_id = 0, *p_wrap_refresh = 0;
static void *volatile g_wrapper = 0;
static void *volatile g_wrap_cap = 0;   // пойманный хуком setCurEditSeq правильный wrapper (см. hook11)
// g_screen_wrapper (НАСТОЯЩИЙ экранный wrapper) определён выше, перед client_log — ловим его из хука
// setKeyFrame (this) при любой правке ключа; резервно из onFrameRendered. +0x408=рендер-seq, +0x42c/e=dirty.
static void *p_wrap_prepare = 0;   // NewVEWrapper::prepare() — ре-композит кадра из рендер-seq по dirty[+0x42c]
static void *p_wrap_commit = 0;    // NewVEWrapper::commit(int) — батч-коммит (не исп. напрямую, на всякий)
static void *p_hotUpdate = 0;   // VEEditorImpl::hotUpdate() — перечитать состояние + ре-рендер (this=ed валиден)
typedef void (*iou_t)(void *, void *, void *);
static volatile int g_auto = 0;
// синтетические ПУСТЫЕ шаблоны — нужны лишь как исходник для deep_copy (тот берёт сырой this),
// поэтому позволяют авто-создать все треки БЕЗ ручного сева ключа.
typedef void (*ctorb_t)(void *, bool);
static ctorb_t p_kf_ctor = 0;      // CommonKeyframe::CommonKeyframe(bool)
static ctorb_t p_kfs_ctor = 0;     // CommonKeyframes::CommonKeyframes(bool)
static void *g_tmpl_kf = 0, *g_tmpl_kfs = 0;
static void *p_set_left_control = 0, *p_set_right_control = 0;   // CommonKeyframe::set_left/right_control(shared_ptr<CommonPoint>)
static void *p_createCommonPoint = 0;   // KeyframeUtils::createCommonPoint(x,y) -> sret shared_ptr<CommonPoint> (owned) — ручки безье
extern "C" void call_createpoint(void *sret, double x, double y, void *fn);   // x8=sret, d0=x, d1=y, br fn
static volatile long g_auto_mt = 0;   // mtime ve_curve.txt, по которому уже вооружились
static volatile int g_armed = 0;      // кривая готова, ждём осознанный выбор follower'а
static void *volatile g_arm_seg = 0;  // что было выделено в момент готовности (обычно исходник) — ждём смены
// Строим по одному пустому CommonKeyframe/CommonKeyframes. calloc с запасом: объекты наши,
// живут всю сессию, не освобождаем и в документ не отдаём (только клонируем из них).
static void seed_templates(void) {
    if (g_tmpl_kf || !p_kf_ctor || !p_kfs_ctor) return;
    void *kf = calloc(1, 4096);  p_kf_ctor(kf, true);
    void *kfs = calloc(1, 4096); p_kfs_ctor(kfs, true);   // bool — калибровка: true=с id, если что false
    g_tmpl_kf = kf; g_tmpl_kfs = kfs;
    Lf("%ld TEMPLATES kf=%p kfs=%p\n", (long)time(NULL), kf, kfs);
}
// читает libc++ std::string (alt-layout) в C-строку
static void readAltString(void *s, char *out, int max) {
    out[0] = 0; if (!s) return;
    const uint8_t *p = (const uint8_t *)s; uint64_t size; const char *data;
    if (p[23] & 0x80) { data = *(const char *const *)p; size = *(const uint64_t *)(p + 8); }
    else { size = p[23] & 0x7F; data = (const char *)p; }
    uint64_t n = size < (uint64_t)(max - 1) ? size : (uint64_t)(max - 1);
    memcpy(out, data, n); out[n] = 0;
}
static char g_written[12][40]; static int g_nwritten = 0;
static int alreadyWritten(const char *p) {
    for (int i = 0; i < g_nwritten; i++) if (strcmp(g_written[i], p) == 0) return 1;
    return 0;
}
static void markWritten(const char *p) {
    if (g_nwritten < 12) { strncpy(g_written[g_nwritten], p, 39); g_written[g_nwritten][39] = 0; g_nwritten++; }
}
// Пишет секцию `prop` из ve_curve.txt в трек a (клоны шаблона kf). Формат файла:
//   KFTypePositionX
//   <время_µs> <значение>
//   ...
//   KFTypePositionY
//   ...
// Вторая величина ОПЦИОНАЛЬНА: у ключей углов corner-pin значение — пара (x, y), и длина
// вектора берётся по числу фактически разобранных чисел. Одна компонента идёт прежним путём.
static void writeCurveForProp(void *a, void *kf, const char *prop) {
    FILE *f = fopen(ud("ve_curve.txt"), "r");
    if (!f) { Lf("%ld CURVE-FILE: нет файла\n", (long)time(NULL)); return; }
    char line[256]; int inSection = 0, ok = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "KFType", 6) == 0) {          // заголовок секции
            char sec[64] = {0}; sscanf(line, "%63s", sec);
            inSection = (strcmp(sec, prop) == 0);
            continue;
        }
        if (!inSection) continue;
        long long t; double v, v2 = 0;
        int nf = sscanf(line, "%lld %lf %lf", &t, &v, &v2);
        if (nf < 2) continue;
        int nv = (nf >= 3) ? 2 : 1;   // пара — ключ угла corner-pin; одна компонента — всё остальное
        void *clone_sp[2] = {0, 0};
        call_sret2(clone_sp, kf, 1, p_deep_copy);
        void *nk = clone_sp[0]; if (!nk) continue;
        p_set_time_offset(nk, &t);
        double vbuf[2] = { v, v2 };
        struct { double *b, *e, *c; } vec = { vbuf, vbuf + nv, vbuf + nv };
        p_set_values(nk, &vec);
        ((fwd_t)g[6].tramp)(a, clone_sp, 0);
        ok++;
    }
    fclose(f);
    Lf("%ld CURVE-FILE prop=%s записал %d ключей\n", (long)time(NULL), prop, ok);
}
static void readSegmentId(char *out, int max) {
    out[0] = 0;
    FILE *f = fopen(ud("ve_curve.txt"), "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "SEGMENT ", 8) == 0) { char id[64] = {0}; sscanf(line + 8, "%63s", id);
            strncpy(out, id, max - 1); out[max - 1] = 0; break; }
    }
    fclose(f);
}
// строит libc++ std::string (alt-layout) из C-строки в 24-байтный obj (длинная строка -> heapbuf)
static void makeAltStr(const char *s, unsigned char obj[24], char *heapbuf, int heapmax) {
    memset(obj, 0, 24);
    size_t len = strlen(s);
    if (len <= 22) { memcpy(obj, s, len); obj[23] = (unsigned char)len; }
    else { int n = len < (size_t)(heapmax - 1) ? (int)len : heapmax - 1;
        memcpy(heapbuf, s, n); heapbuf[n] = 0;
        *(char **)(obj + 0) = heapbuf; *(uint64_t *)(obj + 8) = (uint64_t)n;
        *(uint64_t *)(obj + 16) = (uint64_t)n | (1ULL << 63); }
}
static int sane_ptr(void *p);      // форварды: определены ниже (943)
static int has_vtable(void *obj);
// ОБРАТНАЯ к makeAltStr: читает libc++ std::string (alt-layout) в C-строку.
// short: данные@0, длина в байте 23 БЕЗ сдвига; long: ptr@0, size@8, cap|1<<63 @16
// ⇒ признак long = старший бит байта 23 (это MSB слова cap на +16).
static void readAltStr(const unsigned char *o, char *out, int max) {
    int n = 0;
    if (o[23] & 0x80) {
        const char *d = *(const char **)o;
        long L = (long)*(const uint64_t *)(o + 8);
        if (sane_ptr((void *)d) && L > 0)
            for (long i = 0; i < L && n < max - 1; i++) out[n++] = d[i];
    } else {
        long L = o[23];
        for (long i = 0; i < L && i < 23 && n < max - 1; i++) out[n++] = (char)o[i];
    }
    out[n] = 0;
}

// ЗАМЕР: какие клипы и под какими id реально держит РЕНДЕР-секвенция.
// Отвечает сразу на всё: наш id там есть (значит виноват вызов/обёртка) / id другие (значит
// нужен маппинг документ→рендер) / клипов нет вовсе (follower не доехал до рендера).
// getAllClips(ETETrackType): x8=sret std::vector<shared_ptr<TEClip>>{begin,end,cap}, x0=this, x1=type
// getClipId() const:         x8=sret std::string, x0=clip
static void *p_getAllClips = 0, *p_getClipId = 0;
static void dump_render_clips(void *nat, const char *want) {
    if (!p_getAllClips || !p_getClipId) {
        Lf("%ld CLIPS: нет символов getAllClips=%p getClipId=%p\n", (long)time(NULL), p_getAllClips, p_getClipId);
        return;
    }
    if (!has_vtable(nat)) { Lf("%ld CLIPS: nat невалиден\n", (long)time(NULL)); return; }
    // Типы 0/1 — единственные, что знает getClipWithClipID, но getAllClips может знать больше:
    // в рендере id вида "video5" (НЕ UUID), а на загрузке мелькали "extSticker11…" ⇒ стикеры где-то
    // есть. Перебираем шире. Неизвестный тип уходит в их же ветку с логом getTracksWithType -> пусто.
    for (long tt = 0; tt <= 7; tt++) {
        void *vec[3] = {0, 0, 0};
        call_sret2(vec, nat, tt, p_getAllClips);
        void **b = (void **)vec[0], **e = (void **)vec[1];
        long n = (sane_ptr(b) && sane_ptr(e) && e > b) ? (e - b) / 2 : 0;   // shared_ptr = 16б = 2 слова
        if (n <= 0) continue;                                              // пустые типы не засоряют лог
        Lf("%ld CLIPS type=%ld count=%ld\n", (long)time(NULL), tt, n);
        for (long i = 0; i < n && i < 40; i++) {
            void *clip = b[i * 2];
            if (!has_vtable(clip)) { Lf("   [%ld] clip=%p vtable BAD\n", i, clip); continue; }
            unsigned char s[24]; memset(s, 0, sizeof s);
            call_sret2(s, clip, 0, p_getClipId);
            char id[96]; readAltStr(s, id, sizeof id);
            Lf("   [%ld] clip=%p id=%s%s\n", i, clip, id,
               (want && !strcmp(id, want)) ? "   <== ЭТО НАШ" : "");
        }
    }
}

// читает список property-типов (KFType-заголовки) из ve_curve.txt
static void readProps(char props[8][40], int *n) {
    *n = 0;
    FILE *f = fopen(ud("ve_curve.txt"), "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f) && *n < 8) {
        if (strncmp(line, "KFType", 6) == 0) { char sec[40] = {0}; sscanf(line, "%39s", sec);
            strncpy(props[*n], sec, 39); props[*n][39] = 0; (*n)++; }
    }
    fclose(f);
}
// Создаёт ВСЕ треки follower'а из ve_curve.txt: на каждое свойство клонирует kfs-шаблон, заливает
// кривую (клоны kf-шаблона), цепляет к коллекции сегмента (seg+0xa8). ТОЛЬКО главный поток.
// callback для refreshCurrentFrame (int(*)(void*,int,long long,float,std::string&)) — no-op, чтобы не NULL
static int refresh_cb(void *ctx, int a, long long b, float c, void *str) { return 0; }
static void post_refresh_keys(void);   // форвард: пост клавиш шага кадра (определён ниже, после Cocoa-хелперов)
// ===== РЕНДЕР-СИНК: реплицируем ручной drag ключа через NewVEWrapper::setKeyFrame =====
// живой экранный wrapper: Server::instance() -> getSession() -> getVeWrapper() (последние два — sret shared_ptr)
static int has_vtable(void *obj);   // форвард: определён ниже (896)
static void *get_wrapper(void) {
    if (g_wrap_cap) return g_wrap_cap;   // пойманный хуком setCurEditSeq = ПРАВИЛЬНЫЙ экранный wrapper
    if (g_wrapper) return g_wrapper;
    if (g_screen_wrapper) return g_screen_wrapper;   // пойманный на setKeyFrame — тоже настоящий экранный
    // Логируем КАЖДЫЙ шаг: раньше вся цепочка молча схлопывалась в "wrapper=0", и это выглядело
    // как «не тот wrapper», хотя на деле dlsym не нашёл getSession (мангл был с () вместо (long)).
    static int lg = 0; int v = (lg < 6) ? ++lg : 0;
    if (!(p_server_instance && p_get_session && p_get_vewrapper)) {
        if (v) Lf("%ld WRAP символы: inst=%p getSession=%p getVeWrapper=%p\n", (long)time(NULL),
                  p_server_instance, p_get_session, p_get_vewrapper);
        return 0;
    }
    void *srv = g_srv ? g_srv : ((void *(*)())p_server_instance)();   // живой Server* из invoke надёжнее
    if (!srv) { if (v) Lf("%ld WRAP srv=0\n", (long)time(NULL)); return 0; }
    void *sess_sp[2] = {0, 0};
    // getSession(long): x8=sret(shared_ptr<Session>), x0=this, x1=sid — сверено по дизассемблеру
    // 0x4c9dbc: mov x21,x1 / mov x22,x0 / mov x20,x8. Раньше сюда шёл 0 вместо живого sid.
    call_sret2(sess_sp, srv, g_sid, p_get_session);
    if (!sess_sp[0]) { if (v) Lf("%ld WRAP getSession(sid=%ld)=0 srv=%p\n", (long)time(NULL), g_sid, srv); return 0; }
    void *wrap_sp[2] = {0, 0};
    call_sret2(wrap_sp, sess_sp[0], 0, p_get_vewrapper);   // getVeWrapper(): x8=sret, x0=this (тоже сверено)
    void *w = wrap_sp[0];
    if (v) Lf("%ld WRAP цепочка: srv=%p sid=%ld sess=%p wrap=%p\n", (long)time(NULL), srv, g_sid, sess_sp[0], w);
    // curEditSeq лежит на +0x408 — СВЕРЕНО по дизассемблеру setCurEditSeq @0x2a9d4f8:
    //   2a9d560  str x9,[x19,#0x408]   (x19=this, shared_ptr: 0x408=ptr, 0x410=ctrl)
    // НО: setCurEditSeq ЗАХУКАН И НИ РАЗУ НЕ СТРЕЛЯЕТ ⇒ поле не заполняется НИКОГДА ⇒ старая
    // проверка «seq>4GB иначе не тот wrapper» не могла пройти в принципе и глушила цепочку
    // навсегда. Её писали, когда getSession получал sid=0 и отдавал пустую сессию.
    // Теперь sid живой ⇒ принимаем любой wrapper с валидной vtable, seq логируем как диагностику.
    void *seq = (w && has_vtable(w)) ? *(void **)((char *)w + 0x408) : 0;
    if (has_vtable(w)) {
        if (v) Lf("%ld WRAP принят wrap=%p curEditSeq(+0x408)=%p\n", (long)time(NULL), w, seq);
        g_wrapper = w; return w;
    }
    Lf("%ld WRAP chain bad: wrap=%p (vtable BAD)\n", (long)time(NULL), w);
    return 0;   // без vtable звать по нему нельзя — краш
}
// KFType* -> короткий render-ключ (проверено историческим TEClip.setKeyframe: px/py/sx/rz)
static const char *jsonkey_for(const char *prop) {
    if (!strcmp(prop, "KFTypePositionX")) return "px";
    if (!strcmp(prop, "KFTypePositionY")) return "py";
    if (!strcmp(prop, "KFTypeScaleX"))   return "sx";
    if (!strcmp(prop, "KFTypeRotation")) return "rz";
    return 0;
}
// собирает render-JSON кривой свойства из ve_curve.txt (схема как в историческом test_write)
static int build_render_json(const char *prop, const char *jk, char *out, int cap) {
    FILE *f = fopen(ud("ve_curve.txt"), "r");
    if (!f) return 0;
    char line[256]; int inSec = 0, pts = 0; long long firstT = 0, lastT = 0;
    static char pb[7000]; int pn = 0; pb[0] = 0;
    while (fgets(line, sizeof line, f)) {
        if (!strncmp(line, "KFType", 6)) { char s[64] = {0}; sscanf(line, "%63s", s); inSec = !strcmp(s, prop); continue; }
        if (!inSec) continue;
        long long t; double v; if (sscanf(line, "%lld %lf", &t, &v) != 2) continue;
        if (pts == 0) firstT = t; lastT = t;
        pn += snprintf(pb + pn, sizeof(pb) - pn, "%s{\"v\":%g,\"t\":%lld,\"h\":0,\"it\":\"linear\"}", pts ? "," : "", v, t);
        pts++;
        if (pn > (int)sizeof(pb) - 64) break;
    }
    fclose(f);
    if (!pts) return 0;
    snprintf(out, cap, "{\"v\":\"2.0.0\",\"s\":%lld,\"e\":%lld,\"t\":0,\"k\":{\"%s\":[%s]}}", firstT, lastT, jk, pb);
    return 1;
}
// Пишем кривые в РЕНДЕР через тот же путь, что CapCut использует сам: TEClipBase::setKeyframe(followerTEClip,
// "px/py/sx/rz", curveJSON) (g[0].tramp — исторически живьём работал), потом Seq.commit(g_seq) — флеш.
// g_teclip ловится в hook0 при синке ключей follower'а на загрузке; g_seq — рендер-секвенция из hook3.
// helper: указатель похож на валидный (в загруженный образ/кучу, не мусор/мелочь)
static int sane_ptr(void *p) { return p && (uintptr_t)p > 0x100000000ULL && ((uintptr_t)p & 7) == 0; }
static int has_vtable(void *obj) { return sane_ptr(obj) && sane_ptr(*(void **)obj); }
static void seq_commit(void) {
    if (g[3].tramp && has_vtable(g_seq)) ((fwd_t)g[3].tramp)(g_seq, 0, 0);
}
// ДИАГ: id рендер-клипа через TEClipBase::getClipId() — это doc-id (стабилен → матчим по нему) или render-«videoNN»?
static void diag_clipid(void *clip, double peak) {
    static int dumps = 0;
    if (dumps >= 14 || !has_vtable(clip)) return;
    dumps++;
    char cid[96] = "?";
    if (p_getClipId_TE) { unsigned char sb[24]; memset(sb, 0, 24); call_sret2(sb, clip, 0, p_getClipId_TE); readAltString(sb, cid, sizeof cid); }
    unsigned long long oid = p_originID ? ((unsigned long long (*)(void *))p_originID)(clip) : 0;
    char fp[160] = "?";
    if (p_getFilePath) { void *s = ((void *(*)(void *))p_getFilePath)(clip); if (s) readAltString(s, fp, sizeof fp); }
    Lf("%ld CLIPID clip=%p peak=%.3f clipId=%s originID=%llx path=%s\n", (long)time(NULL), clip, peak, cid, oid, fp);
}
static void replay_cmd(void);   // форвард: реплей пойманной РОДНОЙ команды (определён ниже)

// МЯГКАЯ ПЕРЕЗАГРУЗКА СЕКВЕНЦИИ — «переоткрытие проекта» внутри процесса, без закрытия.
// NewVEWrapper::setCurEditSeq(shared_ptr<IVESequence>, bool) — тем же вызовом CapCut заряжает
// секвенцию в рендер при загрузке проекта. Скармливаем ему ТУ ЖЕ секвенцию (она лежит в самом
// wrapper'е: +0x408 = ptr, +0x410 = ctrl) -> движок пересобирает рендер из ДОКУМЕНТА, а значит
// видит наши свежие кейфреймы.
// ABI сверен по дизассемблеру @0x2a9d4f8:
//   2a9d54c  ldp x9,x8,[x21]   -> x1 = УКАЗАТЕЛЬ на shared_ptr{ptr,ctrl} (передача косвенная)
//   2a9d55c  ldadd             -> retain делает САМ callee, нам добавлять ссылку НЕ НАДО
//   2a9d560  str x9,[x19,#0x408] + release старого на +0x410 (тот же объект -> net 0)
static void *p_setCurEditSeq = 0;
static void *p_rl_updateClipInfo = 0;
extern void *p_seg_get_clip;      // SegmentVideo::get_clip (объявлен ниже)
extern void *p_seg_get_clip_st;   // SegmentSticker / SegmentImageSticker / SegmentText   // VESequenceRenderLayerImpl::updateClipInfo(shared_ptr<pub::Clip>)

// РОДНОЙ ПУТЬ ДОКУМЕНТ->РЕНДЕР ДЛЯ ОДНОГО КЛИПА, БЕЗ УКАЗАТЕЛЕЙ НА РЕНДЕР-КЛИПЫ.
// Render-layer адресует слои по layerId-СТРОКЕ и умеет «обнови вот этот клип»: updateClipInfo
// принимает КЛИП ИЗ ДОКУМЕНТА (тот самый, куда мы только что записали кейфреймы) и сам
// пересобирает его в рендере. Ни g_teclip, ни его протухания, ни крашей.
// shared_ptr передаётся ПО ЗНАЧЕНИЮ (нетривиальный тип) => косвенно: x1 = указатель на {ptr,ctrl}.
// seg+0x190 = &shared_ptr<Clip> — это уже используется рядом (p_seg_get_clip).
// RL ловится хуком только когда CapCut сам синкает ключи — а на свежем слое их ещё нет
// (замкнутый круг). Поэтому ищем его САМИ: объект render-layer лежит внутри VESequenceImpl,
// опознаём по vtable (символ __ZTVN5vesdk25VESequenceRenderLayerImplE экспортирован).
// vptr указывает на vtable+16 (после offset-to-top и typeinfo) — так их кладёт Itanium ABI.
static void *find_rl(void) {
    if (g_rl) return g_rl;
    if (!has_vtable(g_seq)) return 0;
    void *vt = dlsym(RTLD_DEFAULT, "_ZTVN5vesdk25VESequenceRenderLayerImplE");
    if (!vt) { Lf("%ld RLFIND нет символа vtable\n", (long)time(NULL)); return 0; }
    void *want = (char *)vt + 16;
    for (int off = 0; off < 0x800; off += 8) {   // поле-указатель внутри секвенции
        void *cand = *(void **)((char *)g_seq + off);
        if (!sane_ptr(cand)) continue;
        if (*(void **)cand == want) {
            g_rl = cand;
            Lf("%ld RLFIND найден rl=%p (seg+0x%x)\n", (long)time(NULL), cand, off);
            return cand;
        }
    }
    Lf("%ld RLFIND не найден в пределах 0x800\n", (long)time(NULL));
    return 0;
}

static int rl_update_clip(void *seg) {
    if (!find_rl() || !p_rl_updateClipInfo || !has_vtable(seg)) return 0;
    // Тип сегмента заранее не известен -> пробуем оба геттера, берём первый вменяемый.
    void *clipref = 0;
    void *getters[2] = {p_seg_get_clip_st, p_seg_get_clip};
    for (int i = 0; i < 2 && !clipref; i++) {
        if (!getters[i]) continue;
        void *r = ((void *(*)(void *))getters[i])(seg);
        if (r && sane_ptr(*(void **)r)) clipref = r;
    }
    if (!clipref) { Lf("%ld RLUPDATE клип не достался (st=%p vid=%p)\n", (long)time(NULL),
                       p_seg_get_clip_st, p_seg_get_clip); return 0; }
    void *arg[2] = {((void **)clipref)[0], ((void **)clipref)[1]};   // копия shared_ptr
    if (arg[1]) __atomic_fetch_add((long *)((char *)arg[1] + 8), 1, __ATOMIC_RELAXED);   // by-value -> callee заберёт ссылку
    ((void (*)(void *, void *))p_rl_updateClipInfo)(g_rl, arg);
    return 1;
}
// {ptr, ctrl} готового shared_ptr<IVESequence>, пойманного хуком предрендера (hook19)
static void *volatile g_seq_sp[2] = {0, 0};
static void seq_reload(void) {
    void *w = get_wrapper();
    if (!has_vtable(w) || !p_setCurEditSeq) {
        Lf("%ld SEQRELOAD нет: wrapper=%p fn=%p\n", (long)time(NULL), w, p_setCurEditSeq);
        return;
    }
    void **cur = (void **)((char *)w + 0x408);   // {ptr, ctrl} живой секвенции
    void *arg[2] = {cur[0], cur[1]};             // копия shared_ptr; retain сделает callee
    if (!sane_ptr(arg[0])) {
        // curEditSeq пуст (его заполняет сам setCurEditSeq, а он до нас не вызывался).
        // Отдаём ЖИВУЮ секвенцию из хука с НУЛЕВЫМ блоком управления: дизассемблер показывает
        // `cbz x8, …` перед retain — при нулевом ctrl callee retain пропускает, т.е. принимает
        // невладеющий shared_ptr. Владение остаётся у CapCut, мы ничего не освобождаем.
        // Берём shared_ptr, пойманный из предрендера — там указатель УЖЕ приведён к базе
        // IVESequence. Сырой g_seq (VESequenceImpl*) сюда класть нельзя: при множественном
        // наследовании нужен сдвиг к базе, и без него CapCut падает (проверено).
        if (!sane_ptr(g_seq_sp[0])) { Lf("%ld SEQRELOAD нет пойманного shared_ptr\n", (long)time(NULL)); return; }
        arg[0] = g_seq_sp[0]; arg[1] = g_seq_sp[1];
        Lf("%ld SEQRELOAD беру пойманный ptr=%p ctrl=%p\n", (long)time(NULL), arg[0], arg[1]);
    }
    Lf("%ld SEQRELOAD -> setCurEditSeq(w=%p seq=%p)\n", (long)time(NULL), w, arg[0]);
    ((void (*)(void *, void *, long))p_setCurEditSeq)(w, arg, 1);
    Lf("%ld SEQRELOAD готово\n", (long)time(NULL));
}

static void render_sync(char props[][40], int np) {
    Lf("%ld RENDER-SYNC enter g_seq=%p g_teclip=%p(НЕ_исп) wrapper=%p want=%s\n",
       (long)time(NULL), g_seq, g_teclip, g_screen_wrapper, g_want_id);
    // Kill-switch: если создан файл ve_no_render.txt — рендер-пуш пропускаем (ключи в документе уже есть).
    // Позволяет юзеру мгновенно отключить рендер-часть без пересборки, если она вдруг снова падает.
    { struct stat ks; if (stat(ud("ve_no_render.txt"), &ks) == 0) {
        Lf("%ld RENDER-SYNC отключён (ve_no_render.txt) — только документ\n", (long)time(NULL)); return; } }
    if (!has_vtable(g_seq)) { Lf("%ld RENDER-SYNC: g_seq невалиден -> пропуск\n", (long)time(NULL)); return; }
    // СВЕЖИЙ render-TEClip follower'а ПО doc-id — тем же путём, что VESequenceImpl::getClipById
    // (getNativeSequence -> TESequence::getClipWithClipID(id)). НЕ используем g_teclip: он с ЗАГРУЗКИ
    // (может быть чужим клипом и/или протухнуть за время трека -> это и был краш "pure virtual").
    // Ключуется НЕ по doc-id / клипа нет -> вернёт null -> просто без рендер-пуша (без краша). Ref не рел.
    unsigned char idobj[24]; char idheap[64]; makeAltStr(g_want_id, idobj, idheap, sizeof idheap);
    void *teclip = 0, *nat_sp[2] = {0, 0}, *clip_sp[2] = {0, 0};
    if (p_getNativeSequence && p_getClipWithClipID && g_want_id[0]) {
        call_sret2(nat_sp, g_seq, 0, p_getNativeSequence);                    // shptr<TESequence>
        Lf("%ld RENDER-SYNC got-native nat=%p\n", (long)time(NULL), nat_sp[0]);   // диагностика: сюда дошли -> getNativeSequence ок
        if (has_vtable(nat_sp[0])) call_sret4(clip_sp, nat_sp[0], idobj, 0, 0, p_getClipWithClipID);
        teclip = clip_sp[0];
        Lf("%ld RENDER-SYNC teclip-by-id nat=%p teclip=%p (vtable=%s)\n",
           (long)time(NULL), nat_sp[0], teclip, has_vtable(teclip) ? "ok" : "BAD");
        // Промах по id -> перечисляем, что рендер РЕАЛЬНО держит. Один замер отвечает:
        // наш id есть (виноват вызов) / id другие (нужен маппинг) / клипов нет (follower не в рендере).
        if (!has_vtable(teclip)) { static int once = 0; if (!once) { once = 1; dump_render_clips(nat_sp[0], g_want_id); } }
    }
    // ФОЛБЭК НА ПОЙМАННЫЙ КЛИП. Замер доказал: поиск по doc-id обречён — рендер ключует клипы
    // СВОИМИ id (getAllClips дал ровно один клип "video5"), а стикера в TESequence нет НИ НА ОДНОМ
    // из типов 0..7. Единственный живой handle на стикер — g_teclip, пойманный хуком
    // TEClip::setKeyframe на синке документ->рендер, вместе с layerId (в замере: "TextSticker8").
    // ОСТОРОЖНО: прошлая попытка использовать его дала "pure virtual" на ПРОТУХШЕМ указателе,
    // поэтому: has_vtable + читаем идентичность (getClipId/getFilePath) ДО записи — если упадём,
    // в логе останется, на каком шаге. Мгновенно отключается файлом ve_no_render.txt.
    // ⚠️ ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ — небезопасен принципиально, а не по недосмотру.
    // g_teclip — ПОСЛЕДНИЙ пойманный рендер-клип, кем бы он ни был. Убедиться, что это клип
    // ИМЕННО нашего follower'а, нечем: документный UUID и рендерный id (TextSticker8) не связаны.
    // Два краша подряд: (1) переоткрыли проект -> клип пересоздан; (2) слой УДАЛИЛИ и добавили
    // новый -> клип освобождён, а проект не перезагружался, так что защита по смене секвенции
    // не сработала. has_vtable такое не ловит: освобождённая память ещё похожа на объект.
    // Надёжный путь — переоткрытие проекта: CapCut синкает документ->рендер сам (это делает
    // run_track, увидев ve_render_status.txt = 0). Пуш оставлен для опытов: touch ve_render_push.txt.
    if (access(ud("ve_render_push.txt"), F_OK) == 0
        && !has_vtable(teclip) && has_vtable(g_teclip)) {
        void *cand = g_teclip;
        char cid[96] = {0}, fp[256] = {0};
        if (p_getClipId) { unsigned char s[24]; memset(s, 0, sizeof s);
            call_sret2(s, cand, 0, p_getClipId); readAltStr(s, cid, sizeof cid); }
        if (p_getFilePath) { void *s = ((void *(*)(void *))p_getFilePath)(cand);
            if (s) readAltString(s, fp, sizeof fp); }
        Lf("%ld RENDER-SYNC фолбэк: g_teclip=%p layerid=%s clipId=%s file=%s\n",
           (long)time(NULL), cand, g_layerid, cid, fp);
        teclip = cand;
    }
    int synced = 0;
    if (has_vtable(teclip)) {   // только СВЕЖИЙ валидный клип follower'а
        for (int i = 0; i < np; i++) {
            const char *jk = jsonkey_for(props[i]);
            if (!jk) continue;
            char json[8192];
            if (!build_render_json(props[i], jk, json, sizeof json)) continue;
            char *jheap = (char *)malloc(strlen(json) + 8); unsigned char jobj[24];
            makeAltStr(json, jobj, jheap, (int)strlen(json) + 8);
            unsigned char kobj[24]; char kheap[8]; makeAltStr(jk, kobj, kheap, sizeof kheap);
            // ПОШАГОВО: пуш в клип стикера уронил CapCut внутри его же prepare(). Логируем ДО и ПОСЛЕ
            // каждого свойства — в логе останется, на каком ключе умерли. Ограничитель ve_render_one.txt
            // пушит только первое свойство (px) — минимальный эксперимент.
            Lf("%ld PUSH-> key=%s clip=%p json=%zuб\n", (long)time(NULL), jk, teclip, strlen(json));
            ((fwd_t)g[0].tramp)(teclip, kobj, jobj);   // TEClip.setKeyframe(freshTeclip, "px", curveJSON)
            Lf("%ld PUSH-ok key=%s\n", (long)time(NULL), jk);
            if (access(ud("ve_render_one.txt"), F_OK) == 0) {
                free(jheap); synced++; Lf("%ld PUSH: стоп после первого (ve_render_one.txt)\n", (long)time(NULL)); break; }
            free(jheap);
            synced++;
        }
        seq_commit();   // Seq.commit -> флеш модели
    }
    // Экранный wrapper: dirty + prepare() (ре-композит пикселей). Только если пойман ВАЛИДНЫЙ wrapper.
    // РАНЬШЕ здесь было просто g_screen_wrapper — а он ставится ТОЛЬКО в хуке
    // NewVEWrapper::setKeyFrame, который при обычной работе не стреляет ⇒ всегда 0 ⇒ весь этот
    // блок молча пропускался, и превью не обновлялось. get_wrapper() сам вернёт g_screen_wrapper,
    // если тот пойман, иначе построит цепочку Server::instance -> getSession(sid) -> getVeWrapper.
    void *wrapper = get_wrapper();
    if (p_wrap_prepare && sane_ptr(wrapper) && has_vtable(*(void **)((char *)wrapper + 0x408))) {
        *((volatile unsigned char *)wrapper + 0x42c) = 1;
        *((volatile unsigned short *)((char *)wrapper + 0x42e)) = 0x0101;
        ((void (*)(void *))p_wrap_prepare)(wrapper);
        Lf("%ld RENDER-SYNC wrapper-prepare %p\n", (long)time(NULL), wrapper);
    }
    // Пуш в рендер-клип выключен (провалидировать указатель нечем) -> пробуем РОДНОЙ путь:
    // повторяем ту самую команду, которой CapCut двигает слой по СВОЕМУ треку. Замер показал:
    // их трекинг не трогает TEClip::setKeyframe вообще, а шлёт 32 такие команды + Seq.commit.
    // Ловится она по vtable-офсету (ve_cpcmd.txt = 0x4588ee8) — своего RTTI у неё нет.
    // ❌ РЕПЛЕЙ ИХ КОМАНДЫ НЕ ГОДИТСЯ — проверено контрольным замером: команда несёт СВОЙ payload
    // (результат ИХ трекинга), поэтому её повтор применяет их данные, а не наши кривые. Трек по
    // другому объекту не сдвинул стикер ни на пиксель. Чтобы воспользоваться их путём, команду надо
    // СОБРАТЬ со своими данными (ServiceApiMapping::makeReq), а не повторять чужую.
    // Оставляем честный статус: 0 -> Python переоткроет проект, превью будет верным.
    // Мягкая перезагрузка секвенции: движок пересобирает рендер из документа, где уже лежат
    // наши свежие кейфреймы. Гейт ve_no_reload.txt — мгновенно отключить, если начнёт мешать.
    // ЭКСПЕРИМЕНТЫ С ЖИВЫМ РЕФРЕШЕМ ВЫКЛЮЧЕНЫ ПО УМОЛЧАНИЮ (MVP = авто-перезаход).
    // Живой путь так и не найден: setCurEditSeq требует правильный shared_ptr<IVESequence>
    // (сырой VESequenceImpl* роняет), а render-layer к моменту записи ещё не пойман.
    // Включить для опытов: touch ve_live_refresh.txt
    if (access(ud("ve_live_refresh.txt"), F_OK) == 0) {
        int ok = rl_update_clip(g_follower_seg);
        Lf("%ld RLUPDATE follower=%p rl=%p fn=%p -> %s\n", (long)time(NULL), g_follower_seg, g_rl,
           p_rl_updateClipInfo, ok ? "ОТПРАВЛЕНО" : "не вышло");
        if (ok) synced = 77;   // 77 = отдали родному пути -> Python не переоткрывает проект
    }
    // Статус для Python: сколько свойств реально доехало в рендер. 0 = клип follower'а не пойман
    // (слой ни разу не трогали родными средствами) -> превью останется старым, и run_track
    // добьёт результат переоткрытием проекта. ponytail: файл вместо канала — гейты тут уже везде.
    { FILE *sf = fopen(ud("ve_render_status.txt"), "w");
      if (sf) { fprintf(sf, "%d\n", synced); fclose(sf); } }
    Lf("%ld RENDER-SYNC готово: TEClip-push x%d teclip=%p wrapper=%p\n",
       (long)time(NULL), synced, teclip, wrapper);
}

// ---- LIVE-DRIVE: функции (используют makeAltStr/call_sret/g_seq/getClipWithClipID — определены выше) ----
static void load_rig(void) {   // читает ve_rig.txt в g_scene[]/g_rig (без доступа к модели — безопасно на bg-потоке)
    FILE *f = fopen(ud("ve_rig.txt"), "r");
    if (!f) { g_rig.have = 0; g_nscene = 0; return; }
    char line[512], dofpath_s[256] = {0}; double cw = 0, ch = 0; int nl = 0; int keepin = 0;
    struct { char id[80]; char path[256]; double w0; int rot; double gain, pivot, roll; } sc[RIG_MAXS]; int ns = 0, cur = -1;   // ns=накопитель сцен, cur=индекс текущей валидной сцены (-1 если нет/переполнение/парс-фейл)
    // CAMEASE: ручки кривой камеры из документа, по сценам. Static (не стек): большие массивы.
    static int cam_docn[RIG_MAXS][3];                    // [сцена][px|py|sx] — число ключей
    static double cam_doct[RIG_MAXS][3][RIG_MAXK];       // времена
    static KfEase cam_doce[RIG_MAXS][3][RIG_MAXK];       // ручки
    memset(cam_docn, 0, sizeof cam_docn);
    struct { char id[80]; char path[256]; int scene; double z, bx, by, bs, br, ts, ss, sp, tx, ty; } tmp[RIG_MAXL];
    char clr[RIG_MAXL][80]; int ncl = 0;   // CLEAR: сегменты отвязанных дорожек — с них снимаем ключи
    while (fgets(line, sizeof line, f)) {
        if (!strncmp(line, "CANVAS ", 7)) sscanf(line + 7, "%lf %lf", &cw, &ch);
        else if (!strncmp(line, "DOF ", 4)) {   // DOF <id> <путь-PNG> — ГЛОБАЛЬНЫЙ слой-контроллер (последний выигрывает)
            char dofid[80]; int u = 0;
            if (sscanf(line + 4, "%79s %n", dofid, &u) == 1) {
                const char *p = line + 4 + u; while (*p == ' ') p++;
                strncpy(dofpath_s, p, sizeof(dofpath_s) - 1); dofpath_s[sizeof(dofpath_s) - 1] = 0;
                char *e = strchr(dofpath_s, '\n'); if (e) *e = 0; e = strchr(dofpath_s, '\r'); if (e) *e = 0;
            }
        }
        else if (!strncmp(line, "CAMERA ", 7)) {   // CAMERA <id> <w0> <путь-PNG> — ОТКРЫВАЕТ новую сцену (или дропает свои слои если нет места/парс-фейл)
            cur = -1;
            if (ns < RIG_MAXS) {
                // Обнуляем ВСЕ поля сцены: sc — локальный массив без инициализации, а pivot и roll
                // задаются только своими строками (PAN/CAMROLL). Без сброса сцена, у которой такой
                // строки нет, получила бы мусор со стека — то есть случайный завал горизонта.
                sc[ns].id[0] = 0; sc[ns].path[0] = 0; sc[ns].w0 = 0; sc[ns].rot = 0; sc[ns].gain = 0;
                sc[ns].pivot = 0; sc[ns].roll = 0; int u = 0;
                if (sscanf(line + 7, "%79s %lf %n", sc[ns].id, &sc[ns].w0, &u) == 2) {
                    const char *p = line + 7 + u; while (*p == ' ') p++;
                    strncpy(sc[ns].path, p, sizeof(sc[ns].path) - 1); sc[ns].path[sizeof(sc[ns].path) - 1] = 0;
                    char *e = strchr(sc[ns].path, '\n'); if (e) *e = 0; e = strchr(sc[ns].path, '\r'); if (e) *e = 0;
                    cur = ns; ns++;
                }
            }
        }
        else if (!strncmp(line, "PAN ", 4) && cur >= 0) {   // PAN <0|1> <градусов на единицу> [глубина оси]
            int m = 0; double g = 0, pv = 0;
            int k = sscanf(line + 4, "%d %lf %lf", &m, &g, &pv);
            if (k >= 2) { sc[cur].rot = m; sc[cur].gain = g; sc[cur].pivot = (k >= 3) ? pv : 0.0; }
        }
        else if (!strncmp(line, "CAMROLL ", 8) && cur >= 0) {   // CAMROLL <градусы> — завал горизонта сцены
            sc[cur].roll = atof(line + 8);
        }
        else if (!strncmp(line, "CLEAR ", 6) && ncl < RIG_MAXL) {   // CLEAR <segid> — дорожку ОТВЯЗАЛИ: снять с сегмента наши кривые
            if (sscanf(line + 6, "%79s", clr[ncl]) == 1) ncl++;
        }
        else if (!strncmp(line, "KEEPIN ", 7)) {   // KEEPIN 0|1 — «не выходить за рамку» (галочка в панели)
            int v = 0; if (sscanf(line + 7, "%d", &v) == 1) keepin = v;
        }
        else if (!strncmp(line, "LAYER ", 6) && nl < RIG_MAXL && cur >= 0) {   // цепляется к ТЕКУЩЕЙ валидной сцене cur
            tmp[nl].sp = 1.0; tmp[nl].br = 0.0; tmp[nl].path[0] = 0; tmp[nl].tx = tmp[nl].ty = 0.0; int used = 0;
            if (sscanf(line + 6, "%79s %lf %lf %lf %lf %lf %lf %lf %lf %n", tmp[nl].id, &tmp[nl].z, &tmp[nl].bx,
                       &tmp[nl].by, &tmp[nl].bs, &tmp[nl].br, &tmp[nl].ts, &tmp[nl].ss, &tmp[nl].sp, &used) == 9) {
                const char *p = line + 6 + used; while (*p == ' ') p++;   // путь — остаток строки (может с пробелами)
                strncpy(tmp[nl].path, p, sizeof(tmp[nl].path) - 1); tmp[nl].path[sizeof(tmp[nl].path) - 1] = 0;
                char *e = strchr(tmp[nl].path, '\n'); if (e) *e = 0;
                e = strchr(tmp[nl].path, '\r'); if (e) *e = 0;
                tmp[nl].scene = cur;
                nl++;
            }
        }
        // TILT <id-слоя> <вокруг горизонтали> <вокруг вертикали> — ОТДЕЛЬНОЙ строкой, потому что
        // у LAYER в хвосте путь (он может содержать пробелы), дописать туда числа нельзя.
        // Старые риги без TILT читаются как есть: наклон остаётся нулевым.
        else if (!strncmp(line, "TILT ", 5)) {
            char lid[80]; double ax, ay;
            if (sscanf(line + 5, "%79s %lf %lf", lid, &ax, &ay) == 3)
                for (int i = 0; i < nl; i++)
                    if (!strcmp(tmp[i].id, lid)) { tmp[i].tx = ax; tmp[i].ty = ay; break; }
        }
        // CAMEASE <px|py|sx> t,c,vti,vi,vto,vo;t,c,...; — ручки кривой камеры из документа.
        else if (!strncmp(line, "CAMEASE ", 8) && cur >= 0) {
            char key[4] = {0}; const char *p = line + 8;
            if (sscanf(p, "%3s", key) == 1) {
                int pi = key[0] == 'p' ? (key[1] == 'x' ? 0 : 1) : 2;   // px|py|sx
                p += strlen(key); while (*p == ' ') p++;
                int m = 0;
                while (*p && m < RIG_MAXK) {
                    long long t; int c; double vti, vi, vto, vo;
                    if (sscanf(p, "%lld,%d,%lf,%lf,%lf,%lf", &t, &c, &vti, &vi, &vto, &vo) == 6) {
                        cam_doct[cur][pi][m] = (double)t;
                        cam_doce[cur][pi][m].cubic = c;
                        cam_doce[cur][pi][m].vti = vti; cam_doce[cur][pi][m].vi = vi;
                        cam_doce[cur][pi][m].vto = vto; cam_doce[cur][pi][m].vo = vo;
                        m++;
                    }
                    const char *sc2 = strchr(p, ';'); if (!sc2) break; p = sc2 + 1;
                }
                cam_docn[cur][pi] = m;
            }
        }
    }
    fclose(f);
    // CLEAR публикуем ДО раннего выхода: отвязка ПОСЛЕДНЕЙ дорожки даёт nl==0, а ключи снять всё равно надо
    for (int i = 0; i < ncl; i++) { strncpy(g_clear[i], clr[i], 79); g_clear[i][79] = 0; }
    g_nclear = ncl;
    if (ns == 0 || cw < 1 || ch < 1 || nl == 0) { g_rig.have = 0; g_nscene = 0; return; }
    g_rig.cw = cw; g_rig.ch = ch; g_rig.keepin = keepin;
    strncpy(g_rig.dof_path, dofpath_s, 255); g_rig.dof_path[255] = 0;
    g_dof_teclip = 0; g_dofn_sx = 0; g_dofn_sy = 0;   // сброс кэша DoF-слоя на новый rig
    // Кривые камеры СОХРАНЯЕМ, если сцена та же (совпал путь PNG). Панель переписывает риг на
    // каждое движение ползунка глубины; со сбросом кэша следующий за этим ре-драйв выходил бы
    // вхолостую (camn_px==0), и глубина не применялась бы до тех пор, пока юзер не тронет камеру.
    struct Scene old[RIG_MAXS]; int nold = g_nscene;
    for (int i = 0; i < nold && i < RIG_MAXS; i++) old[i] = g_scene[i];
    for (int s = 0; s < ns; s++) {
        int keep = -1;
        for (int o = 0; o < nold && o < RIG_MAXS; o++)
            if (old[o].cam_path[0] && !strcmp(old[o].cam_path, sc[s].path)) { keep = o; break; }
        if (keep >= 0) g_scene[s] = old[keep];            // та же камера — кривые и клип живут дальше
        else { g_scene[s].cam_teclip = 0;
               g_scene[s].camn_px = g_scene[s].camn_py = g_scene[s].camn_sx = g_scene[s].camn_rz = 0; }
        strncpy(g_scene[s].cam_id, sc[s].id, 79); g_scene[s].cam_id[79] = 0;
        strncpy(g_scene[s].cam_path, sc[s].path, 255); g_scene[s].cam_path[255] = 0;
        g_scene[s].cam_w0 = sc[s].w0;
        g_scene[s].pan_rot = sc[s].rot; g_scene[s].pan_gain = sc[s].gain; g_scene[s].pan_pivot = sc[s].pivot; g_scene[s].cam_roll = sc[s].roll;
        // Ручки кривой камеры из документа -> в сцену (стабильный источник easing для перспективы).
        g_scene[s].docn_px = cam_docn[s][0]; g_scene[s].docn_py = cam_docn[s][1]; g_scene[s].docn_sx = cam_docn[s][2];
        for (int i = 0; i < RIG_MAXK; i++) {
            g_scene[s].doct_px[i] = cam_doct[s][0][i]; g_scene[s].doce_px[i] = cam_doce[s][0][i];
            g_scene[s].doct_py[i] = cam_doct[s][1][i]; g_scene[s].doce_py[i] = cam_doce[s][1][i];
            g_scene[s].doct_sx[i] = cam_doct[s][2][i]; g_scene[s].doce_sx[i] = cam_doce[s][2][i];
        }
    }
    // Пойманные рендер-клипы слоёв тоже ПЕРЕЖИВАЮТ перезагрузку рига (матч по пути исходника):
    // клип ловится хуком только когда CapCut синкает ключи слоя, т.е. раз при открытии. Обнуляя
    // его на каждую смену глубины, мы получали `wrote 0/3` — документ обновлён, картинка стоит.
    struct { char path[256]; void *clip, *vt; } oldl[RIG_MAXL]; int noldl = g_rig.nlayer;
    for (int i = 0; i < noldl && i < RIG_MAXL; i++) {
        strncpy(oldl[i].path, g_rig.L[i].path, 255); oldl[i].path[255] = 0;
        oldl[i].clip = g_rig.L[i].clip; oldl[i].vt = g_rig.L[i].vtable;
    }
    g_rig.nlayer = nl;
    for (int i = 0; i < nl; i++) { strncpy(g_rig.L[i].id, tmp[i].id, 79); g_rig.L[i].id[79] = 0;
        strncpy(g_rig.L[i].path, tmp[i].path, 255); g_rig.L[i].path[255] = 0;
        g_rig.L[i].scene = tmp[i].scene;
        g_rig.L[i].z = tmp[i].z; g_rig.L[i].bx = tmp[i].bx; g_rig.L[i].by = tmp[i].by;
        g_rig.L[i].base_scale = tmp[i].bs; g_rig.L[i].base_rot = tmp[i].br;
        g_rig.L[i].tstart = tmp[i].ts; g_rig.L[i].sstart = tmp[i].ss;
        g_rig.L[i].speed = tmp[i].sp > 0 ? tmp[i].sp : 1.0;
        g_rig.L[i].tiltx = tmp[i].tx; g_rig.L[i].tilty = tmp[i].ty;
        g_rig.L[i].clip = 0; g_rig.L[i].vtable = 0; g_rig.L[i].rec_seg = 0; g_rig.L[i].rec_vt = 0;
        for (int o = 0; o < noldl && o < RIG_MAXL; o++)   // тот же исходник -> возвращаем пойманный клип
            if (oldl[o].path[0] && !strcmp(oldl[o].path, g_rig.L[i].path)) {
                g_rig.L[i].clip = oldl[o].clip; g_rig.L[i].vtable = oldl[o].vt; break;
            } }
    g_diag_done = 0;
    g_nscene = ns;      // публикуем ПОСЛЕДним (после g_scene[] и L[]) — чтобы drive не прочитал новый счётчик при старых данных
    g_rig.have = 1;
    Lf("%ld RIG loaded scenes=%d layers=%d cw=%.0f ch=%.0f\n", (long)time(NULL), ns, nl, cw, ch);
}
// ЖИВОЙ ПУТЬ КАМЕРЫ из панели (ve_campath.txt). Раньше позу камеры можно было менять только
// правкой закрытого проекта — то есть «подвигать камеру» во время работы было НЕЛЬЗЯ. Теперь
// панель пишет ключи сюда, мы кладём их прямо в кэш кривых сцены (те же px/py/sx/rz, что даёт
// hook0 из рендер-кривой) и просим ре-драйв: параллакс едет мгновенно, проект не закрывается.
// Формат: `CAM <путь-PNG-камеры>` затем строки `K <t_src_us> <px> <py> <sx> <rz>`.
static long g_campath_mt = 0;
static void load_campath(void) {
    FILE *f = fopen(ud("ve_campath.txt"), "r");
    if (!f) return;
    char line[512], png[256] = {0};
    static double t[RIG_MAXK], px[RIG_MAXK], py[RIG_MAXK], sx[RIG_MAXK], rz[RIG_MAXK];
    int n = 0;
    while (fgets(line, sizeof line, f)) {
        if (!strncmp(line, "CAM ", 4)) {
            strncpy(png, line + 4, sizeof(png) - 1); png[sizeof(png) - 1] = 0;
            char *e = strpbrk(png, "\r\n"); if (e) *e = 0;
        } else if (!strncmp(line, "K ", 2) && n < RIG_MAXK) {
            if (sscanf(line + 2, "%lf %lf %lf %lf %lf", &t[n], &px[n], &py[n], &sx[n], &rz[n]) == 5) n++;
        }
    }
    fclose(f);
    if (!png[0] || n < 1) return;
    for (int s = 0; s < g_nscene; s++) {
        if (strcmp(g_scene[s].cam_path, png)) continue;
        struct Scene *S = &g_scene[s];
        for (int i = 0; i < n; i++) {
            S->cam_tpx[i] = S->cam_tpy[i] = S->cam_tsx[i] = S->cam_trz[i] = t[i];
            S->cam_vpx[i] = px[i]; S->cam_vpy[i] = py[i]; S->cam_vsx[i] = sx[i]; S->cam_vrz[i] = rz[i];
            S->epx[i].cubic = S->epy[i].cubic = S->esx[i].cubic = S->erz[i].cubic = 0;
        }
        S->camn_px = S->camn_py = S->camn_sx = S->camn_rz = n;
        g_drive_pending = 1;
        Lf("%ld CAMPATH сцена=%d ключей=%d\n", (long)time(NULL), s, n);
        return;
    }
}

static void *resolve_clip(const char *id) {   // свежий TEClip по doc-id (как в render_sync). ТОЛЬКО main-поток
    if (!(p_getNativeSequence && p_getClipWithClipID) || !has_vtable(g_seq)) return 0;
    unsigned char idobj[24]; char idheap[96]; makeAltStr(id, idobj, idheap, sizeof idheap);
    void *nat_sp[2] = {0, 0}, *clip_sp[2] = {0, 0};
    call_sret2(nat_sp, g_seq, 0, p_getNativeSequence);
    if (!has_vtable(nat_sp[0])) return 0;
    call_sret4(clip_sp, nat_sp[0], idobj, 0, 0, p_getClipWithClipID);
    return has_vtable(clip_sp[0]) ? clip_sp[0] : 0;
}
static double curve_at(const double *ts, const double *vs, int n, double t) {
    if (n <= 0) return 0.0;
    if (t <= ts[0]) return vs[0];
    if (t >= ts[n - 1]) return vs[n - 1];
    for (int i = 1; i < n; i++) if (t <= ts[i]) { double dt = ts[i] - ts[i - 1]; double u = dt > 1 ? (t - ts[i - 1]) / dt : 0; return vs[i - 1] + (vs[i] - vs[i - 1]) * u; }
    return vs[n - 1];
}
// Значение кривой С УЧЁТОМ безье-ручек ключей. curve_at() линеен намеренно: для параллакса
// easing не сэмплируется, а ПЕРЕСАЖИВАЕТСЯ на объектные ключи (scale_ease) — CapCut сам его
// проигрывает. У фокуса так нельзя: блюр это нелинейная функция от фокуса, ручки на неё не
// переносятся. Значит фокус надо честно ВЫЧИСЛЯТЬ по кривой.
// Ручки CapCut заданы АБСОЛЮТНЫМИ СМЕЩЕНИЯМИ от своего ключа (x — микросекунды, y — единицы
// свойства), а не долями интервала. Нормируем на dt/dv сами. Проверено по драфту: PositionX
// (dt=1283333, dv=+0.911) и ScaleX (dt=500000, dv=+0.507) дают ОДИН пресет 0.88/0.14 — совпасть
// это может только у абсолютных. vti/vi — вход в ключ i (left_control), vto/vo — выход из i-1.
static double bez_at(const double *ts, const double *vs, const KfEase *es, int n, double t) {
    if (n <= 0) return 0.0;
    if (t <= ts[0]) return vs[0];
    if (t >= ts[n - 1]) return vs[n - 1];
    for (int i = 1; i < n; i++) {
        if (t > ts[i]) continue;
        double dt = ts[i] - ts[i - 1];
        if (dt <= 1) return vs[i];
        double u = (t - ts[i - 1]) / dt;
        if (!es || (!es[i - 1].cubic && !es[i].cubic)) return vs[i - 1] + (vs[i] - vs[i - 1]) * u;
        // сегмент как кубический безье в (время, значение): P0=ключ i-1, P3=ключ i,
        // P1 = выход из i-1 (vto/vo), P2 = вход в i (vti/vi). Ищем параметр s по времени.
        if (es[i - 1].vto == 0 && es[i].vti == 0) return vs[i - 1] + (vs[i] - vs[i - 1]) * u;
        double dv = vs[i] - vs[i - 1];
        int flat = (dv > -1e-12 && dv < 1e-12);   // значение не меняется — y не определён, да и не нужен
        double x1 = es[i - 1].vto / dt,       y1 = flat ? 0.0 : es[i - 1].vo / dv;
        double x2 = 1.0 + es[i].vti / dt,     y2 = flat ? 1.0 : 1.0 + es[i].vi / dv;
        // Кламп ТОЛЬКО по времени: ось x обязана быть монотонной, иначе Ньютон разойдётся
        // (в драфте встречаются аномальные ручки, напр. 1+lc.x/dt = 2.32). y не клампим — безье
        // законно перелетает за ключ, это и есть «упругая» плавность.
        if (x1 < 0) x1 = 0; if (x1 > 1) x1 = 1;
        if (x2 < 0) x2 = 0; if (x2 > 1) x2 = 1;
        double s = u;
        for (int k = 0; k < 12; k++) {   // Ньютон по x(s)=u; 12 итераций хватает с запасом
            double m = 1 - s;
            double x = 3 * m * m * s * x1 + 3 * m * s * s * x2 + s * s * s - u;
            double d = 3 * m * m * x1 + 6 * m * s * (x2 - x1) + 3 * s * s * (1 - x2);
            if (d < 1e-9 && d > -1e-9) break;
            double ns = s - x / d;
            if (ns < 0) ns = 0; if (ns > 1) ns = 1;
            if (ns - s < 1e-7 && s - ns < 1e-7) { s = ns; break; }
            s = ns;
        }
        double m = 1 - s;
        double y = 3 * m * m * s * y1 + 3 * m * s * s * y2 + s * s * s;
        return vs[i - 1] + (vs[i] - vs[i - 1]) * y;
    }
    return vs[n - 1];
}

// Слить отсортированные наборы времён в один без дублей. Нужно блюру: его сетка обязана
// содержать И времена камеры, И времена ключей фокуса, иначе ключ фокуса, поставленный не
// в момент ключа камеры, в выборку просто не попадает и анимация фокуса теряется.
static int merge_times(const double *const *src, const int *cnt, int nsrc, double *out, int max) {
    int idx[4] = {0, 0, 0, 0}, n = 0;
    if (nsrc > 4) nsrc = 4;
    while (n < max) {
        double best = 0; int have = 0;
        for (int s = 0; s < nsrc; s++)
            if (idx[s] < cnt[s] && (!have || src[s][idx[s]] < best)) { best = src[s][idx[s]]; have = 1; }
        if (!have) break;
        for (int s = 0; s < nsrc; s++) while (idx[s] < cnt[s] && src[s][idx[s]] <= best) idx[s]++;
        out[n++] = best;
    }
    return n;
}

// Имена свойств углов. Объявлены здесь, а не у corner-pin API ниже: наклон плоскости пишет
// их из drive_layers, который расположен выше.
static const char *kCP[4] = {"KFTypeCornerPinUpLeft", "KFTypeCornerPinUpRight",
                             "KFTypeCornerPinDownLeft", "KFTypeCornerPinDownRight"};

// «НЕ ВЫХОДИТЬ ЗА РАМКУ» для четырёхугольника. Развернув камеру или наклонив плоскость, мы
// перестаём накрывать кадр — по углам открываются чёрные клинья. Растягиваем четырёхугольник
// вокруг центра ровно настолько, чтобы все четыре угла кадра оказались внутри: пускаем луч из
// центра в угол кадра и смотрим, где он пересекает границу.
static void cp_grow_to_frame(double *out, double ccx, double ccy) {
    double need = 1.0;
    const int o[4] = {0, 1, 3, 2};   // обход по периметру
    for (int c = 0; c < 4; c++) {
        double px = (c & 1) ? 1.0 : -1.0, py = (c & 2) ? 1.0 : -1.0;
        double rx = px - ccx, ry = py - ccy;
        if (sqrt(rx * rx + ry * ry) < 1e-9) continue;
        double best = 0;
        for (int e = 0; e < 4; e++) {
            int i = o[e], j = o[(e + 1) & 3];
            double ax2 = out[i * 2] - ccx, ay2 = out[i * 2 + 1] - ccy;
            double bx2 = out[j * 2] - ccx, by2 = out[j * 2 + 1] - ccy;
            double den = rx * (ay2 - by2) - ry * (ax2 - bx2);
            if (den > -1e-12 && den < 1e-12) continue;
            double t = (ax2 * (ay2 - by2) - ay2 * (ax2 - bx2)) / den;   // |пересечение| вдоль луча
            double u = (rx * ay2 - ry * ax2) / den;                     // положение на ребре
            if (t > 0 && u >= -1e-9 && u <= 1.0 + 1e-9 && t > best) best = t;
        }
        if (best > 1e-9 && 1.0 / best > need) need = 1.0 / best;
    }
    if (need <= 1.0) return;
    for (int k = 0; k < 4; k++) {
        out[k * 2] = ccx + (out[k * 2] - ccx) * need;
        out[k * 2 + 1] = ccy + (out[k * 2 + 1] - ccy) * need;
    }
}

// ПРОЕКЦИЯ СЛОЯ КАМЕРОЙ: четыре угла в NDC на один момент времени (out[8] = UL,UR,LL,LR).
//
// Обычный слой — биллборд: проецируем ЦЕНТР и умножаем на масштаб. Наклонённый слой так считать
// нельзя, у его углов РАЗНАЯ глубина, и именно из-за этого возникает перспектива.
//
// Разворачиваем ту же формулу, что и для центра. Базовые bx/by — экранные пиксели покоя, значит
// мировая координата точки на глубине z равна X·z/f. Угол со смещением (u,v) экранных пикселей
// после поворота плоскости на ax (вокруг горизонтали) и ay (вокруг вертикали):
//     мировое смещение по горизонтали  u·cos(ay),  по вертикали  v·cos(ax)
//     смещение по ГЛУБИНЕ              u·sin(ay) + v·sin(ax)
// и дальше обычная перспектива с делением на (z_угла − cz). При нулевом наклоне выражение
// сворачивается ровно в (bx·z − f·cx)/d — то есть биллборд остаётся частным случаем.
static void layer_corners_canvas(int li, const struct Scene *S, double cxi, double cyi, double czi,
                                 double rolli, double f, double hw, double hh, double *out,
                                 double yaw, double pitch) {
    (void)S;
    const double DEG = 3.14159265358979323846 / 180.0;
    double z = g_rig.L[li].z, bs = g_rig.L[li].base_scale;
    double bx = g_rig.L[li].bx, by = g_rig.L[li].by;
    double ax = g_rig.L[li].tiltx * DEG, ay = g_rig.L[li].tilty * DEG;
    double br = (g_rig.L[li].base_rot + rolli) * DEG;   // собственный поворот слоя + ролл камеры
    double hwl = hw * bs, hhl = hh * bs;                // полуразмеры слоя в экранных пикселях покоя
    double cyw = cos(yaw * DEG), syw = sin(yaw * DEG), cpt = cos(pitch * DEG), spt = sin(pitch * DEG);
    const double sx4[4] = {-1, 1, -1, 1}, sy4[4] = {-1, -1, 1, 1};   // UL, UR, LL, LR
    for (int k = 0; k < 4; k++) {
        double u = sx4[k] * hwl, v = sy4[k] * hhl;
        if (br != 0.0) { double c = cos(br), s = sin(br), ru = u * c - v * s; v = u * s + v * c; u = ru; }
        double wu = u * cos(ay), wv = v * cos(ax);
        // МИРОВАЯ точка угла. Базовые координаты — экранные пиксели покоя, поэтому мир = X·z/f.
        double Xw = (bx + wu) * z / f;
        double Yw = (by + wv) * z / f;
        double Zw = z + (u * sin(ay) + v * sin(ax)) * z / f;   // наклон самой плоскости (если есть)
        // В систему КАМЕРЫ: сначала сдвиг, потом ПОВОРОТ камеры (рыскание, затем тангаж).
        // Это и делает эмуляцию 3D: камера не просто едет вбок, а разворачивается, и каждый
        // слой ловит свою перспективу — ближний сильнее, дальний слабее.
        double px1 = Xw - cxi, py1 = Yw - cyi, pz1 = Zw - czi;
        double px2 = cyw * px1 - syw * pz1, pz2 = syw * px1 + cyw * pz1;
        double py3 = cpt * py1 - spt * pz2,  pz3 = spt * py1 + cpt * pz2;
        if (pz3 < 1e-3) pz3 = 1e-3;   // угол ушёл за камеру — прижимаем, а не делим на ноль
        out[k * 2] = (f * px2 / pz3) / hw;
        // Y ВНИЗ, без RIG_SIGN_Y. У corner-pin своя система координат: NDC от центра кадра,
        // ось Y направлена ВНИЗ (проверено планарным трекером — compute_cornerpin_ml отдаёт
        // именно так, и там углы ложатся правильно). Это НЕ то же, что KFTypePositionY,
        // где Y вверх — оттуда и берётся SIGN_Y в остальном коде. С флипом фон в плеере
        // вставал зеркально (поймано глазами на демо-сцене).
        out[k * 2 + 1] = (f * py3 / pz3) / hh;
    }
    // Центр слоя (как у биллборда) — вокруг него и нормируем, и дорастаем.
    double dc0 = z - czi; if (dc0 < 1e-3) dc0 = 1e-3;
    double cx0 = ((bx * z - f * cxi) / dc0) / hw, cy0 = ((by * z - f * cyi) / dc0) / hh;
    if (yaw != 0.0 || pitch != 0.0) {
        // ПОВОРОТ КАМЕРЫ нормировать по площади НЕЛЬЗЯ: изменение размера при развороте — это
        // и есть эффект, ради которого всё делается. Но «не выходить за рамку» остаётся в силе:
        // отвернувшись, камера иначе покажет пустоту за краем слоя.
        if (g_rig.keepin) cp_grow_to_frame(out, cx0, cy0);
        return;
    }
    if (ax == 0.0 && ay == 0.0) return;
    // НОРМИРОВКА РАЗМЕРА. Честная перспектива при наклоне тянет ближний край к камере, и слой
    // раздувается: на 40° он вылезал за кадр вдвое (замерено). Пользователь просил НАКЛОНИТЬ
    // плоскость, а не подъехать к ней — поэтому оставляем форму (сходящиеся края), но
    // возвращаем прежнюю площадь. Масштабируем вокруг центра биллборда одним множителем:
    // разными по осям — получилось бы искажение, а не поворот.
    double dc = z - czi; if (dc < 1e-3) dc = 1e-3;
    double ccx = ((bx * z - f * cxi) / dc) / hw, ccy = ((by * z - f * cyi) / dc) / hh;   // Y вниз, как у углов
    double sc = z / dc;
    double a_flat = (2.0 * hwl * sc / hw) * (2.0 * hhl * sc / hh);
    double a_tilt = 0;   // площадь четырёхугольника по формуле шнурков, порядок обхода UL,UR,LR,LL
    { const int o[4] = {0, 1, 3, 2};
      for (int k = 0; k < 4; k++) { int i = o[k], j = o[(k + 1) & 3];
          a_tilt += out[i * 2] * out[j * 2 + 1] - out[j * 2] * out[i * 2 + 1]; }
      a_tilt = a_tilt < 0 ? -a_tilt : a_tilt; a_tilt *= 0.5; }
    if (a_tilt < 1e-9) return;
    double kk = sqrt(a_flat / a_tilt);
    for (int k = 0; k < 4; k++) {
        out[k * 2] = ccx + (out[k * 2] - ccx) * kk;
        out[k * 2 + 1] = ccy + (out[k * 2 + 1] - ccy) * kk;
    }
    if (g_rig.keepin) cp_grow_to_frame(out, ccx, ccy);
}

// Всё выше считается в КАНВАСНЫХ NDC, а движку corner-pin нужен NDC СОБСТВЕННОЙ КОРОБКИ слоя.
// ЗАМЕРЕНО на экране (2026-08-06): слой с clip-масштабом 0.366 и статичным квадом [-0.9..-0.1] лёг
// в x 719..786 px — ровно туда, куда предсказывает коробка слоя (718..786), а не канвас (565..754).
// Совпадают эти две системы только при base_scale ≈ 1, поэтому на полноэкранных слоях перспектива
// камеры выглядела верной, а слой с масштабом 0.37 отрисовывался втрое меньше положенного.
// Конверсию делаем ОТДЕЛЬНОЙ обёрткой: у функции выше три разные точки выхода, и вписывать
// пересчёт в каждую — верный способ однажды пропустить одну.
// ponytail: полуразмер коробки = hw*base_scale, то есть материал считается заполняющим канвас при
// scale=1. У материала с другим соотношением сторон перспектива выйдет чуть слабее по одной оси —
// лечится передачей aspect слоя в риг, если станет заметно.
static void layer_corners(int li, const struct Scene *S, double cxi, double cyi, double czi,
                          double rolli, double f, double hw, double hh, double *out,
                          double yaw, double pitch) {
    layer_corners_canvas(li, S, cxi, cyi, czi, rolli, f, hw, hh, out, yaw, pitch);
    double bs = g_rig.L[li].base_scale;
    if (!(bs > 1e-6)) return;                       // вырожденный масштаб — оставляем как есть
    double cxn = g_rig.L[li].bx / hw, cyn = g_rig.L[li].by / hh;   // центр коробки, канвасные NDC
    for (int k = 0; k < 4; k++) {
        out[k * 2]     = (out[k * 2] - cxn) / bs;
        out[k * 2 + 1] = (out[k * 2 + 1] - cyn) / bs;
    }
}

// Ручки кривой камеры ИЗ ДОКУМЕНТА, выровненные по временам ключей камеры (ct[0..n]). Документ
// стабилен (рендер-синк отдаёт кубическую лишь эпизодически), поэтому это основной источник
// easing для перспективы. Матч по времени; нет совпадения -> линейный ключ. 0 = документа нет.
static const KfEase *cam_doc_ease(const struct Scene *S, int pi, const double *ct, int n, KfEase *out) {
    int dn = pi == 0 ? S->docn_px : pi == 1 ? S->docn_py : S->docn_sx;
    const double *dt = pi == 0 ? S->doct_px : pi == 1 ? S->doct_py : S->doct_sx;
    const KfEase *de = pi == 0 ? S->doce_px : pi == 1 ? S->doce_py : S->doce_sx;
    if (dn <= 0) return 0;
    for (int i = 0; i < n; i++) {
        out[i].cubic = 0; out[i].vti = out[i].vi = out[i].vto = out[i].vo = 0;
        for (int j = 0; j < dn; j++)
            if (dt[j] - ct[i] < 1.0 && ct[i] - dt[j] < 1.0) { out[i] = de[j]; break; }
    }
    return out;
}

// easing камеры -> объект-пространство: время (vti/vto) общее, копируем; значение (vi/vo) масштабируем objΔ/камΔ на каждом сегменте
static void scale_ease(const KfEase *ce, const double *cv, const double *ov, KfEase *oe, int n) {
    for (int i = 0; i < n; i++) {
        oe[i].cubic = ce[i].cubic;
        oe[i].vti = ce[i].vti; oe[i].vto = ce[i].vto;
        double cin  = (i > 0)     ? cv[i] - cv[i - 1] : 0.0, oin  = (i > 0)     ? ov[i] - ov[i - 1] : 0.0;
        double cout = (i < n - 1) ? cv[i + 1] - cv[i] : 0.0, oout = (i < n - 1) ? ov[i + 1] - ov[i] : 0.0;
        oe[i].vi = (fabs(cin)  > 1e-9) ? ce[i].vi * oin  / cin  : 0.0;
        oe[i].vo = (fabs(cout) > 1e-9) ? ce[i].vo * oout / cout : 0.0;
    }
}

// Поза камеры в ПРОИЗВОЛЬНЫЙ момент, С УЧЁТОМ КРИВОЙ. Кривая (ручки безье) берётся из ДОКУМЕНТА
// (cam_doc_ease), а не из рендер-синка — документ стабилен. bez_at сэмплирует кривую в точке t.
// Нужна, потому что углы перспективы движку можно слать ТОЛЬКО линейными ключами (парный "cubic"
// роняет рендер — поймано крашем). Поэтому кривую проявляем через ПЛОТНУЮ сетку линейных ключей,
// а форму каждого сегмента даёт эта функция.
struct CamPose { double cx, cy, cz, roll, yaw, pitch; };
static CamPose cam_pose_at(const struct Scene *S, double t, double f, double hw, double hh, double z_min) {
    CamPose p = {0, 0, 0, 0, 0, 0};
    static KfEase bx[RIG_MAXK], by[RIG_MAXK], bs[RIG_MAXK];
    const KfEase *ex = cam_doc_ease(S, 0, S->cam_tpx, S->camn_px, bx); if (!ex) ex = S->epx;
    const KfEase *ey = cam_doc_ease(S, 1, S->cam_tpy, S->camn_py, by); if (!ey) ey = S->epy;
    const KfEase *es = cam_doc_ease(S, 2, S->cam_tsx, S->camn_sx, bs); if (!es) es = S->esx;
    double pxv = S->camn_px > 0 ? bez_at(S->cam_tpx, S->cam_vpx, ex, S->camn_px, t) : 0.0;
    double pyv = S->camn_py > 0 ? bez_at(S->cam_tpy, S->cam_vpy, ey, S->camn_py, t) : 0.0;
    double sxv = S->camn_sx > 0 ? bez_at(S->cam_tsx, S->cam_vsx, es, S->camn_sx, t) : 1.0;
    if (sxv < 0.05) sxv = 0.05;
    // + cam_roll: ролл сцены из рига СКЛАДЫВАЕТСЯ с кейфреймленным, а не заменяет его.
    p.roll = (S->camn_rz > 0 ? curve_at(S->cam_trz, S->cam_vrz, S->camn_rz, t) : 0.0) + S->cam_roll;
    if (S->pan_rot && S->pan_gain > 0) {
        double ty = -pxv * S->pan_gain * DEG_R, tp = pyv * S->pan_gain * DEG_R, zp = S->pivot_z;
        p.yaw = ty / DEG_R; p.pitch = tp / DEG_R;
        p.cx = -zp * sin(ty) * cos(tp); p.cy = zp * sin(tp); p.cz = zp * (1.0 - cos(ty) * cos(tp));
    } else {
        p.cx = -pxv * RIG_ZREF * hw / f; p.cy = pyv * RIG_ZREF * hh / f;
    }
    p.cz += z_min * (1.0 - 1.0 / sxv);
    return p;
}

// Сетка времён для углов перспективы. Есть кривая у камеры (кубический ключ в документе) — дробим
// каждый интервал, иначе линейные углы соединились бы прямыми и форма кривой пропала. Прямые
// участки не дробим.
static int persp_grid(const struct Scene *S, const double *tt, int n, double *out, int max) {
    int any = 0;
    for (int i = 0; i < S->docn_px; i++) if (S->doce_px[i].cubic) any = 1;
    for (int i = 0; i < S->docn_py; i++) if (S->doce_py[i].cubic) any = 1;
    for (int i = 0; i < S->docn_sx; i++) if (S->doce_sx[i].cubic) any = 1;
    if (!S->docn_px && !S->docn_py && !S->docn_sx) {   // документа нет — смотрим рендер-синк
        for (int i = 0; i < S->camn_px; i++) if (S->epx[i].cubic) any = 1;
        for (int i = 0; i < S->camn_sx; i++) if (S->esx[i].cubic) any = 1;
    }
    // Дробим АДАПТИВНО: занимаем весь бюджет max, а не жёсткие x7. При 4 ключах это 79 точек
    // вместо 22 (хорда с ~45 px падает до ~20 px), а при 13+ ключах — наоборот, дробим реже и
    // хвост таймлайна перестаёт обрезаться по max. 26 — потолок, дальше выигрыш уже незаметен.
    int split = (n > 1) ? (max - 1) / (n - 1) : 1;
    if (split > 26) split = 26;
    if (split < 1) split = 1;
    int m = 0;
    for (int i = 0; i < n && m < max; i++) {
        out[m++] = tt[i];
        if (!any || i + 1 >= n) continue;
        for (int q = 1; q < split && m < max; q++) out[m++] = tt[i] + (tt[i + 1] - tt[i]) * q / (double)split;
    }
    return m;
}

// Кривая ПАРНОГО свойства (угол corner-pin): значение — [x,y]. Именно так CapCut сам пушит углы
// в рендер-клип ключами "ul"/"ur"/"dl"/"dr" — поймано в логе setKeyframe. Это и есть живой канал
// перспективы: тот же путь, что у px/py, поэтому картинка меняется без перезагрузки проекта.
static int build_curve_arrays2(const char *jk, const double *ts, const double *xy, const KfEase *oe, int n, char *out, int cap) {
    if (n <= 0) return 0;
    static char pb[7000]; int pn = 0; pb[0] = 0;
    for (int i = 0; i < n; i++) {
        if (oe && oe[i].cubic)   // ручки безье в том же формате, что у одиночных свойств
            pn += snprintf(pb + pn, sizeof(pb) - pn,
                "%s{\"v\":[%g,%g],\"t\":%lld,\"h\":0,\"it\":\"cubic\",\"vti\":%g,\"vi\":%g,\"vto\":%g,\"vo\":%g}",
                i ? "," : "", xy[i * 2], xy[i * 2 + 1], (long long)ts[i],
                oe[i].vti, oe[i].vi, oe[i].vto, oe[i].vo);
        else
            pn += snprintf(pb + pn, sizeof(pb) - pn, "%s{\"v\":[%g,%g],\"t\":%lld,\"h\":0,\"it\":\"linear\"}",
                           i ? "," : "", xy[i * 2], xy[i * 2 + 1], (long long)ts[i]);
        if (pn > (int)sizeof(pb) - 128) break;
    }
    snprintf(out, cap, "{\"v\":\"2.0.0\",\"s\":%lld,\"e\":%lld,\"t\":0,\"k\":{\"%s\":[%s]}}",
             (long long)ts[0], (long long)ts[n - 1], jk, pb);
    return 1;
}
static int build_curve_arrays(const char *jk, const double *ts, const double *vs, const KfEase *oe, int n, char *out, int cap) {
    if (n <= 0) return 0;
    static char pb[7000]; int pn = 0; pb[0] = 0;
    for (int i = 0; i < n; i++) {
        if (oe && oe[i].cubic)   // eased ключ: переносим безье-ручки (render-формат)
            pn += snprintf(pb + pn, sizeof(pb) - pn,
                "%s{\"v\":%g,\"t\":%lld,\"h\":0,\"it\":\"cubic\",\"vti\":%g,\"vi\":%g,\"vto\":%g,\"vo\":%g}",
                i ? "," : "", vs[i], (long long)ts[i], oe[i].vti, oe[i].vi, oe[i].vto, oe[i].vo);
        else
            pn += snprintf(pb + pn, sizeof(pb) - pn, "%s{\"v\":%g,\"t\":%lld,\"h\":0,\"it\":\"linear\"}", i ? "," : "", vs[i], (long long)ts[i]);
        if (pn > (int)sizeof(pb) - 128) break;
    }
    snprintf(out, cap, "{\"v\":\"2.0.0\",\"s\":%lld,\"e\":%lld,\"t\":0,\"k\":{\"%s\":[%s]}}", (long long)ts[0], (long long)ts[n - 1], jk, pb);
    return 1;
}
static void *seg_by_id(const char *id) {   // документный сегмент по doc-id, СО СТРАЖЕМ vtable:
    for (int i = 0; i < g_nid; i++) if (strcmp(g_id_str[i], id) == 0) {   // после компаунда сегмент освобождается,
        void *seg = g_id_seg[i];                                          // указатель в карте висит -> запись = use-after-free = краш
        if (seg && has_vtable(seg) && *(void **)seg == g_id_vt[i]) return seg;   // vtable цел и совпал -> сегмент жив
        return 0;                                                                // сменился/невалиден -> НЕ писать (стейл)
    }
    return 0;
}
// документный сегмент ПО ПУТИ файла: компаунд меняет id вложенного объекта, но путь стабилен. Резолвим сегмент->материал->путь
// лениво и ТОЛЬКО у видео-сегментов (g_segvideo_vt), кэшируем. ТОЛЬКО main-поток (зовёт API CapCut).
static void *seg_by_path(const char *path) {   // сегмент по пути файла: работает и для объектов ВНУТРИ компаундов (id меняется, путь стабилен)
    if (!path || !path[0] || !p_seg_get_material || !p_mat_get_path || !g_segvideo_vt) return 0;
    for (int i = g_nid - 1; i >= 0; i--) {   // от СВЕЖИХ к старым: текущий сегмент выигрывает над стейлом
        void *seg = g_id_seg[i];
        if (!seg || !has_vtable(seg) || *(void **)seg != g_id_vt[i]) continue;   // стейл-по-vtable -> мимо
        if (!g_id_pres[i]) {   // путь ПРЯМО с живого сегмента (кэш): get_material=&shptr@seg+0x1b8 -> ptr -> get_path=&string@mat+0x50
            g_id_pres[i] = 1; g_id_path[i][0] = 0;
            if (*(void **)seg == g_segvideo_vt) {   // только SegmentVideo (иначе +0x1b8 — чужое поле)
                void *matref = ((void *(*)(void *))p_seg_get_material)(seg);   // &shared_ptr<MaterialVideo>
                void *mat = matref ? *(void **)matref : 0;                     // MaterialVideo*
                if (mat && has_vtable(mat)) { void *s = ((void *(*)(void *))p_mat_get_path)(mat); if (s) readAltString(s, g_id_path[i], sizeof g_id_path[i]); }
            }
        }
        if (g_id_path[i][0] && !strcmp(g_id_path[i], path)) return seg;
    }
    return 0;
}
// ДОКУМЕНТНЫЕ ключи свойства в сегмент (на таймлайне, персист) = auto_apply/writeCurveForProp БЕЗ ve_curve.txt и
// БЕЗ render_sync (визуал даёт live-drive; render_sync — крашивший кусок). times в SOURCE-времени этого сегмента.
// ФОЛБЭК для СВЕЖЕ завёрнутых объектов (их вложенный сегмент не попал в getseg-карту — CapCut создаёт его мимо
// get_segment): перечислить ВСЕ живые сегменты (вкл. субдрафты) через get_segments_recursive и найти по пути.
// Кэш на слой -> перечисляем раз на заворачивание, не каждый драйв. За флагом ve_recursive.txt. ТОЛЬКО main-поток.
static void *seg_recursive(int li, const char *path) {
    { struct stat ks; if (stat(ud("ve_no_recursive.txt"), &ks) == 0) return 0; }   // кил-свитч (по умолчанию ВКЛ; отключить — создать ve_no_recursive.txt)
    void *rs = g_rig.L[li].rec_seg;
    if (rs && has_vtable(rs) && *(void **)rs == g_rig.L[li].rec_vt) return rs;   // кэш слоя жив
    g_rig.L[li].rec_seg = 0;
    if (!p_get_segs_recursive || g_ndrafts < 1 || !path[0] || !g_segvideo_vt) return 0;
    void *sp_draft[2] = { g_drafts[0][0], g_drafts[0][1] };   // root draft shared_ptr {ptr,ctrl}
    void *vec[3] = {0, 0, 0};                                  // sret vector<shptr<Segment>> {begin,end,cap}
    call_sret4(vec, (void *)sp_draft, (void *)0, (void *)1, 0, p_get_segs_recursive);   // x1=tracktype 0, x2=recurse 1
    void *found = 0; int cnt = 0;
    for (char *e = (char *)vec[0]; e && e + 0x10 <= (char *)vec[1]; e += 0x10) {   // элемент = shptr<Segment> (16B)
        cnt++;
        void *seg = *(void **)e;
        if (!seg || !has_vtable(seg) || *(void **)seg != g_segvideo_vt) continue;
        void *matref = ((void *(*)(void *))p_seg_get_material)(seg);
        void *mat = matref ? *(void **)matref : 0;
        if (mat && has_vtable(mat)) { void *s = ((void *(*)(void *))p_mat_get_path)(mat); char p[256] = {0};
            if (s) readAltString(s, p, sizeof p);
            if (p[0] && !strcmp(p, path)) { found = seg; break; } }
    }
    Lf("%ld RECURSIVE li=%d cnt=%d found=%p path=%s\n", (long)time(NULL), li, cnt, found, path);   // ДИАГ
    if (found) { g_rig.L[li].rec_seg = found; g_rig.L[li].rec_vt = *(void **)found; }
    // ponytail: vec + shared_ptr'ы не освобождаем — утечка ограничена (перечисляем раз на слой при протухании кэша)
    return found;
}
static volatile int g_ctrl_diag = 0;   // одноразовый лог: несут ли клоны control-точки
// nv = значений на точку (1 по умолчанию — все старые вызовы). Корнер-пину нужно 2: пара [x,y].
static void replay_cmd(void);    // форвард: реплей пойманной родной команды (определён ниже)
static void seq_refresh(void);   // форвард: взвод грязных флагов + commit (определён ниже)
// n == 0 => прикрепить ПУСТОЙ трек = СНЯТЬ ключи со свойства (отвязка дорожки от камеры).
static void doc_write_curves(void *seg, const char *prop, const double *times, const double *vals, const KfEase *oe, int n, int nv = 1) {
    if (!seg || !g_tmpl_kf || !g_tmpl_kfs || n < 0) return;
    // Страж use-after-free: применяя пресет кривой, CapCut ПЕРЕСОЗДАЁТ keyframe-коллекцию, и наш
    // кэшированный сегмент устаревает. Запись в `seg+0xa8` мёртвого объекта = SIGBUS (поймано
    // крашем при «применил пресет на новые точки»). has_vtable отсеивает освобождённую память.
    if (!has_vtable(seg)) return;
    if (!(p_deep_copy && p_deepcopy_kfs && p_set_property_type && p_set_keyframe_list && p_insertOrUpdate
          && p_set_time_offset && p_set_values)) return;
    void *coll = (char *)seg + 0xa8;
    void *kfs_sp[2] = {0, 0};
    call_sret2(kfs_sp, g_tmpl_kfs, 1, p_deepcopy_kfs);      // клон CommonKeyframes-трека
    if (!kfs_sp[0]) return;
    char ph[64]; unsigned char pobj[24]; makeAltStr(prop, pobj, ph, sizeof ph);
    ((iou_t)p_set_property_type)(kfs_sp[0], pobj, 0);
    void *empty[3] = {0, 0, 0};
    ((iou_t)p_set_keyframe_list)(kfs_sp[0], empty, 0);
    for (int i = 0; i < n; i++) {
        void *kf_sp[2] = {0, 0};
        call_sret2(kf_sp, g_tmpl_kf, 1, p_deep_copy);      // клон CommonKeyframe (как writeCurveForProp)
        if (!kf_sp[0]) continue;
        long long t = (long long)times[i];
        p_set_time_offset(kf_sp[0], &t);
        double vbuf[4];
        for (int k = 0; k < nv && k < 4; k++) vbuf[k] = vals[i * nv + k];
        struct { double *b, *e, *c; } vec = { vbuf, vbuf + nv, vbuf + nv };
        p_set_values(kf_sp[0], &vec);
        // easing: eased ключ -> curveType=FreeCurveInOut + безье-ручки в control-точки клона (время=vti/vto, значение=vi/vo)
        if (oe && oe[i].cubic && g_ct_freecurve >= 0 && p_set_curveType && p_createCommonPoint && p_set_left_control && p_set_right_control) {
            int ct = g_ct_freecurve;
            ((void (*)(void *, const int *))p_set_curveType)(kf_sp[0], &ct);
            // СОЗДАЁМ owned control-точки с тангенсами (createCommonPoint) и привязываем — get_left_control у клона был null,
            // set_x/y писать некуда -> кривая оставалась ПРЯМОЙ в документе (рендер брал тангенсы из JSON, потому там кривая была).
            void *lsp[2] = {0, 0}, *rsp[2] = {0, 0};
            call_createpoint(lsp, oe[i].vti, oe[i].vi, p_createCommonPoint);
            call_createpoint(rsp, oe[i].vto, oe[i].vo, p_createCommonPoint);
            if (lsp[0]) ((void (*)(void *, void *))p_set_left_control)(kf_sp[0], lsp);
            if (rsp[0]) ((void (*)(void *, void *))p_set_right_control)(kf_sp[0], rsp);
            if (!g_ctrl_diag) { g_ctrl_diag = 1; Lf("%ld CTRL created left=%p right=%p (нужны !=0)\n", (long)time(NULL), lsp[0], rsp[0]); }
            // ponytail: локальную ссылку lsp/rsp не освобождаем -> течёт 1 ref на точку/драйв. Ограничено (драйвы не непрерывны).
        }
        ((fwd_t)g[6].tramp)(kfs_sp, kf_sp, 0);             // insertKeyframe(коллекция-клон, ключ-клон)
    }
    ((iou_t)p_insertOrUpdate)(coll, pobj, kfs_sp);         // прикрепить трек к сегменту (идемпотентно = заменяет)
}

// Снести трек ключей свойства ЦЕЛИКОМ. doc_write_curves(...,n=0) так не умеет: он всё равно
// цепляет пустой трек через insertOrUpdate, и в родном редакторе кривых остаётся МЁРТВАЯ строка —
// свойство есть, точек нет, правка не делает ничего. clearEmptyKeyframes такие треки тоже не
// снимает (проверено замером: прибрано=3, а в драфте по-прежнему 4 пустых группы на слой).
// removeKeyframes берёт ту же коллекцию seg+0xa8, что и insertOrUpdate, только без 3-го аргумента.
static void doc_drop_curves(void *seg, const char *prop) {
    if (!seg || !p_remove_kfs || !has_vtable(seg)) return;
    void *coll = (char *)seg + 0xa8;
    char ph[64]; unsigned char pobj[24]; makeAltStr(prop, pobj, ph, sizeof ph);
    ((void (*)(void *, void *))p_remove_kfs)(coll, pobj);
}

static int layer_by_path(const char *p) {   // путь исходника -> индекс слоя в риге (-1 если нет)
    if (!p || !p[0]) return -1;
    for (int li = 0; li < g_rig.nlayer; li++) if (!strcmp(g_rig.L[li].path, p)) return li;
    return -1;
}
// ve_segmap.txt ("<video_seg_id>\t<path>") -> g_live_id[слой]: живой id объекта из файла (обход стрей-дублей пути)
static void load_live_ids(void) {
    for (int li = 0; li < g_rig.nlayer; li++) g_live_id[li][0] = 0;
    FILE *f = fopen(ud("ve_segmap.txt"), "r");
    if (!f) return;
    char line[600];
    while (fgets(line, sizeof line, f)) {
        char *tab = strchr(line, '\t'); if (!tab) continue;
        *tab = 0; char *pth = tab + 1; char *nl = strpbrk(pth, "\r\n"); if (nl) *nl = 0;
        int li = layer_by_path(pth);
        if (li >= 0) { strncpy(g_live_id[li], line, 47); g_live_id[li][47] = 0; }
    }
    fclose(f);
}

// DoF ВКЛ только при наличии ve_dof.txt (в нём опц. "focus aperture"). По умолчанию off — не мешаем.
static int dof_enabled(void) {
    struct stat ks; if (stat(ud("ve_dof.txt"), &ks) != 0) return 0;
    FILE *f = fopen(ud("ve_dof.txt"), "r");
    if (f) { double a, b; if (fscanf(f, "%lf %lf", &a, &b) == 2) { g_dof_focus = a; g_dof_aperture = b; } fclose(f); }
    return 1;
}

// ЧЕСТНЫЙ счётчик записи (определён ниже, рядом с cp_collect — там же живут аксессоры).
// Нужен потому, что doc_write_curves об отказе МОЛЧИТ, а drive_blur считал попытки, а не успехи:
// в логе стояло blur=3, при этом в модели CapCut не оказывалось ни одного ключа (замер 2026-08-09).
static int kf_count_prop(void *seg, const char *prop);
// ЖИВОЙ динамический блюр: blur(t)=aperture*|d_i(t)-focus|, d_i=z_i-cz(t) -> ключи effects_adjust_blur
// в effect-сегмент компаунда. common_keyframes у SegmentVideoEffect на 0xa8 (базовый Segment) — doc_write_curves как есть.
static void *g_dof_eseg[RIG_MAXL], *g_dof_evt[RIG_MAXL];   // кэш effect-сегмента на слой (валид. по vtable)
static int g_dof_nd = -1, g_dof_tick = 0, g_dof_incomplete = 1;   // incomplete: не все пары из карты найдены -> ре-резолв каждый драйв (эффекты попадают в getseg-карту не сразу)

// перечислить ВСЕ сабдрафты от root -> в каждом спарить (видео->слой по пути, эффект) -> закэшировать на слой.
// GetAllSubDraft видит все компаунды независимо от захвата через get_segment (в отличие от g_drafts).
static void dof_resolve(void) {
    for (int li = 0; li < g_rig.nlayer; li++) { g_dof_eseg[li] = 0; g_dof_evt[li] = 0; }
    if (!p_get_all_subdraft || !p_get_all_segments || g_ndrafts < 1) return;
    // ve_dof_map.txt (пишет dof.py из файла проекта): "<effect_id>\t<path>" — надёжная пара объект↔эффект,
    // т.к. draft-API не резолвит фото-компаунды. effect_id стабилен и надёжно попадает в getseg-карту (эффекты рендерятся).
    static char effid[RIG_MAXL][48];
    for (int k = 0; k < RIG_MAXL; k++) effid[k][0] = 0;
    FILE *mf = fopen(ud("ve_dof_map.txt"), "r");
    int nmap = 0, nlines = 0;   // nmap — пар, чей ПУТЬ нашёлся в риге; nlines — строк в карте
    if (mf) {
        char line[600];
        while (fgets(line, sizeof line, mf)) {
            char *tab = strchr(line, '\t'); if (!tab) continue;
            *tab = 0; char *pth = tab + 1; char *nl = strpbrk(pth, "\r\n"); if (nl) *nl = 0;
            nlines++;
            int lj = layer_by_path(pth);
            if (lj >= 0) { strncpy(effid[lj], line, 47); effid[lj][47] = 0; nmap++; }
        }
        fclose(mf);
    }
    int nres = 0;
    for (int li = 0; li < g_rig.nlayer; li++) {   // слой -> его effect_id -> ищем в getseg-карте по id
        if (!effid[li][0]) continue;
        for (int i = g_nid - 1; i >= 0; i--) {
            if (strcmp(g_id_str[i], effid[li])) continue;
            void *seg = g_id_seg[i];
            if (seg && has_vtable(seg) && *(void **)seg == g_id_vt[i] && *(void **)seg == g_segeffect_vt) {
                g_dof_eseg[li] = seg; g_dof_evt[li] = *(void **)seg; nres++;
            }
            break;
        }
    }
    // Сравниваем со СТРОКАМИ карты, а не с nmap. Иначе «0 из 0» (пути слоёв ещё не совпали —
    // например риг пересобирали, пока в нём стояли прокси-сегменты без путей) давало incomplete=0,
    // резолв больше не повторялся, и блюр молча оставался нулевым до перезапуска CapCut.
    g_dof_incomplete = (nres < nlines);
    Lf("%ld DOF resolve(map-file): слоёв=%d из %d пар (строк=%d, карта=%d)\n", (long)time(NULL), nres, nmap, nlines, g_nid);
    // ponytail: subvec/segvec + shared_ptr'ы не освобождаем — течёт, ограничено (резолвим по протуханию/раз в 32 драйва)
}

static void dof_resolve_if_needed(void) {   // резолв effect-сегментов ПЛОСКО (по всем слоям) — ОДИН раз за драйв (не посценно: не тормошим getseg-карту N раз)
    int need = g_dof_incomplete || (g_dof_nd != g_ndrafts) || ((++g_dof_tick & 31) == 0);
    for (int li = 0; !need && li < g_rig.nlayer; li++) { void *e = g_dof_eseg[li]; if (e && (!has_vtable(e) || *(void **)e != g_dof_evt[li])) need = 1; }
    if (need) { g_dof_nd = g_ndrafts; dof_resolve(); }
}
static int drive_blur(int scene, const double *cz, const double *tt, int n) {   // блюр слоёв ОДНОЙ сцены по ЕЁ cz; вернёт число записанных
    int wrote = 0;
    // ГЛУБИНА СЦЕНЫ — делитель диафрагмы. Без него формула не переживала родной ползунок:
    // он в ПРОЦЕНТАХ, тянуть до 200–300 % естественно, а это диафрагма 2–3. Замер 2026-08-09
    // (слой стоял на 176 %/287 %, глубины z=1/3/5): ближний слой давал 3.3, дальний 3.7 —
    // оба сидели на клампе 1.0, и ползунок переставал что-либо менять. Снаружи это выглядело
    // как «работает пять секунд, потом щёлк». Нормируем на разброс глубин: при 100 % слой на
    // дальнем краю сцены получает ровно максимум, весь ход ползунка остаётся рабочим.
    double zlo = 0, zhi = 0; int nz = 0;
    for (int li = 0; li < g_rig.nlayer; li++) {
        if (g_rig.L[li].scene != scene) continue;
        double z = g_rig.L[li].z;
        if (!nz++) { zlo = zhi = z; } else { if (z < zlo) zlo = z; if (z > zhi) zhi = z; }
    }
    double zspan = (zhi - zlo) > 1e-6 ? (zhi - zlo) : 1.0;   // одна плоскость -> делить не на что
    for (int li = 0; li < g_rig.nlayer; li++) {
        if (g_rig.L[li].scene != scene) continue;   // слой не этой сцены — его блюр посчитает своя сцена
        void *eseg = g_dof_eseg[li];
        if (!eseg || !has_vtable(eseg) || *(void **)eseg != g_dof_evt[li]) continue;   // нет эффекта у слоя / протух
        double z = g_rig.L[li].z;
        static double vb[RIG_MAXK], ob[RIG_MAXK];
        for (int i = 0; i < n; i++) {
            // ОБА ПАРАМЕТРА — С ОТДЕЛЬНОГО СЛОЯ-КОНТРОЛЛЕРА (решение Назара 2026-08-09, возврат
            // к версии, которая «работала как часы»): вертикальный масштаб = ГЛУБИНА (плоскость
            // фокуса), горизонтальный = ДИАФРАГМА. Ключи ставятся родным ромбом на этих строках,
            // поэтому bez_at, а не curve_at: линейная выборка съела бы кривые пользователя.
            // Нет слоя/кривой (его ещё не вживили) -> константы из ve_dof.txt, прежнее поведение.
            double focus = g_dofn_sy > 0 ? bez_at(g_dof_tsy, g_dof_vsy, g_dof_esy, g_dofn_sy, tt[i])
                                         : g_dof_focus;
            double aper  = g_dofn_sx > 0 ? bez_at(g_dof_tsx, g_dof_vsx, g_dof_esx, g_dofn_sx, tt[i])
                                         : g_dof_aperture;
            double b = aper * dabs((z - cz[i]) - focus) / zspan;   // cz[i] — ЭТОЙ сцены (плоскость фокуса едет со своей камерой)
            vb[i] = b < 0 ? 0 : (b > 1 ? 1 : b);
            ob[i] = tt[i];   // effect-время = timeline-время (компаунд от t=0, скорость 1)
        }
        doc_write_curves(eseg, "effects_adjust_blur", ob, vb, 0, n);
        // ЧИТАЕМ ОБРАТНО. doc_write_curves об отказе молчит, а прежний счётчик считал ВЫЗОВЫ:
        // лог показывал blur=3, а в сохранённой модели CapCut ключей не было ни одного
        // (замер 2026-08-09: драйв при силе 0.02 -> на диске прежние значения).
        int back = kf_count_prop(eseg, "effects_adjust_blur");
        if (back > 0) wrote++;
        { static int nlog = 0; if (nlog < 64) { nlog++;
            Lf("%ld DOF write слой=%d послано=%d в_модели=%d eseg=%p vt=%p\n",
               (long)time(NULL), li, n, back, eseg, has_vtable(eseg) ? *(void **)eseg : 0); } }
    }
    return wrote;
}

// ОТВЯЗКА дорожки от камеры: снять НАШИ кривые с её сегментов.
// Своя анимация слоя при привязке умерла (так решено) — восстанавливать нечего, просто чистим.
// Идёт ДО guard'а g_rig.have: отвязка последней дорожки оставляет риг без слоёв, а чистить всё равно надо.
//
// ДВА ШАГА, И ВТОРОЙ ОБЯЗАТЕЛЕН. Пустой трек (n=0) — структура, которой CapCut НЕ создаёт никогда
// (замер: 0 пустых из 135 реальных треков в проектах юзера), и restore мог бы разыменовать первый
// ключ вслепую. Поэтому сразу отдаём сегмент ИХ ЖЕ уборщику `KeyframeUtils::clearEmptyKeyframes`,
// который выкидывает пустые треки → сегмент остаётся ровно в той форме, что делает сам движок.
static void *p_clear_empty_kf = 0;
static void make_leaky_sp(void *obj, void **sp2);   // форвард: определён ниже (corner-pin)
// Включить corner-pin на сегменте и выставить 8 углов. Форвард: тело у corner-pin API ниже.
// Наклон плоскости обязан звать это ПЕРЕД записью кривых углов: у свежего клипа поле corner_pin
// пустое, и документные ключи углов в такой сегмент просто не приживаются (проверено — молча
// не сохранялись). Функция же и метит клип грязным, чтобы commit пересобрал рендер.
static void cp_apply(void *seg, const double *c);
static void cp_disable(void *seg);   // выключить corner-pin (перспектива больше не нужна)

// Кому мы ставили углы наклона. Нужно, чтобы при выключении наклона снять ИМЕННО свои углы:
// у слоя может быть чужой corner-pin (планарный трекинг) — его трогать нельзя.
static char g_tilted_ids[RIG_MAXL][80]; static int g_ntilted = 0;
static void tilt_remember(const char *id) {
    for (int i = 0; i < g_ntilted; i++) if (!strcmp(g_tilted_ids[i], id)) return;
    if (g_ntilted < RIG_MAXL) { strncpy(g_tilted_ids[g_ntilted], id, 79); g_tilted_ids[g_ntilted++][79] = 0; }
}
static int tilt_forget(const char *id) {   // 1 = мы этот слой наклоняли, углы наши
    for (int i = 0; i < g_ntilted; i++) if (!strcmp(g_tilted_ids[i], id)) {
        g_tilted_ids[i][0] = 0; memmove(&g_tilted_ids[i], &g_tilted_ids[i + 1], (g_ntilted - i - 1) * 80);
        g_ntilted--; return 1;
    }
    return 0;
}
static void drive_clears(void) {
    if (!g_nclear) return;
    { struct stat ks; if (stat(ud("ve_no_clear.txt"), &ks) == 0) {
          g_nclear = 0; Lf("%ld CLEAR отключён (ve_no_clear.txt)\n", (long)time(NULL)); return; } }   // кил-свитч: путь новый
    static const char *P[4] = {"KFTypePositionX", "KFTypePositionY", "KFTypeScaleX", "KFTypeRotation"};
    int done = 0, tidy = 0;
    g_driving = 1;
    seed_templates();
    for (int i = 0; i < g_nclear; i++) {
        void *seg = seg_by_id(g_clear[i]);
        if (!seg || !has_vtable(seg)) continue;
        // Сносим трек целиком. Раньше здесь было «обнулить (n=0), потом позвать clearEmptyKeyframes»,
        // но замер показал, что их уборщик такие треки НЕ снимает: он отчитывался об успехе, а в
        // драфте оставались 4 пустые группы на слой — и в редакторе кривых висели мёртвые строки.
        for (int k = 0; k < 4; k++) { doc_drop_curves(seg, P[k]); tidy++; }
        done++;
    }
    g_nclear = 0;
    g_driving = 0;
    if (done) { seq_refresh(); replay_cmd(); }   // подтянуть очищенный документ в рендер (тот же путь, что у corner-pin)
    Lf("%ld CLEAR снял ключи с %d сегментов (прибрано треков: %d)\n", (long)time(NULL), done, tidy);
}

static void drive_layers(void) {   // пересчёт параллакса из кэша кривых КАЖДОЙ сцены + живая запись. ТОЛЬКО main-поток
    { struct stat ks; if (stat(ud("ve_no_drive.txt"), &ks) == 0) return; }   // kill-switch
    drive_clears();
    if (!g_rig.have || !has_vtable(g_seq)) return;
    int any = 0; for (int s = 0; s < g_nscene; s++) if (g_scene[s].camn_px >= 1) { any = 1; break; }   // хоть одна камера захвачена?
    if (!any) return;
    double f = RIG_FOCAL_FRAC * g_rig.ch, hw = g_rig.cw / 2.0, hh = g_rig.ch / 2.0;
    g_driving = 1;   // guard СКОБИТ ВЕСЬ мультисценовый проход: снять между сценами -> hook0 перезапишет кэш недодрайвленной сцены
    // персистим документные ключи КАЖДЫЙ драйв: тогда документ == render-drive всегда, спорить нечему
    int persist_now = 0;
    { struct stat ks; if (stat(ud("ve_no_persist.txt"), &ks) != 0) {
          persist_now = 1; seed_templates(); load_live_ids(); } }   // живые id объектов из ve_segmap.txt (обход стрей-дублей)
    int dof_on = persist_now && dof_enabled() && g_segeffect_vt;
    if (dof_on) dof_resolve_if_needed();   // резолв effect-сегментов ОДИН раз (плоско по всем сценам)
    static double cx[RIG_MAXK], cy[RIG_MAXK], cz[RIG_MAXK], tt[RIG_MAXK], roll[RIG_MAXK];
    static double yaw[RIG_MAXK], pitch[RIG_MAXK];   // разворот камеры (штатив): рыскание и тангаж
    int wrote = 0, persisted = 0, eased = 0, bypath = 0, blur = 0, tilted = 0, cpeased = 0, tidied = 0;
    // Кил-свитч уборки пустых треков: читаем ОДИН раз за драйв, а не на каждый слой.
    int no_tidy = 0;
    { struct stat ts; if (stat(ud("ve_no_tidy.txt"), &ts) == 0) no_tidy = 1; }
    for (int s = 0; s < g_nscene; s++) {   // === КАЖДАЯ СЦЕНА === (строго последовательно на main -> статик-scratch переиспользуется без гонки)
        struct Scene *S = &g_scene[s];
        int n = S->camn_px; if (n < 1) continue; if (n > RIG_MAXK) n = RIG_MAXK;
        double z_min = 1e9;   // ближайший слой ЭТОЙ сцены (общий z_min смешал бы глубины -> кривой dolly у всех сцен)
        for (int li = 0; li < g_rig.nlayer; li++) if (g_rig.L[li].scene == s && g_rig.L[li].z < z_min) z_min = g_rig.L[li].z;
        if (z_min > 1e8) z_min = RIG_ZREF;   // нет слоёв -> нейтрально
        { // точка облёта — середина между ближним и дальним слоем сцены: она остаётся на месте,
          // а всё ближе/дальше расходится в стороны. Это и читается как объём.
          double z_max = 0; for (int li = 0; li < g_rig.nlayer; li++)
              if (g_rig.L[li].scene == s && g_rig.L[li].z > z_max) z_max = g_rig.L[li].z;
          S->pivot_z = S->pan_pivot > 0 ? S->pan_pivot
                       : (z_max > 0 ? (z_min + z_max) * 0.5 : RIG_ZREF); }
        for (int i = 0; i < n; i++) {   // поза камеры этой сцены на временах её ключей px
            double t = S->cam_tpx[i];
            double pxv = S->cam_vpx[i];
            double pyv = S->camn_py > 0 ? curve_at(S->cam_tpy, S->cam_vpy, S->camn_py, t) : 0.0;
            double sxv = S->camn_sx > 0 ? curve_at(S->cam_tsx, S->cam_vsx, S->camn_sx, t) : 1.0;   // sx=1 -> без зума (cz=0)
            if (sxv < 0.05) sxv = 0.05;
            tt[i] = t;
            if (S->pan_rot && S->pan_gain > 0) {
                // ОБЛЁТ: панорама читается как разворот камеры ВОКРУГ точки в глубине сцены.
                //
                // Почему не «штатив на месте»: все слои растянуты на кадр, значит с любой глубины
                // видны под ОДНИМ углом — чистый поворот исказил бы их одинаково, и объёма бы не
                // возникло (поймано тестом). Настоящую объёмность даёт разворот вокруг точки:
                // слои на её глубине стоят, ближние уезжают в одну сторону, дальние — в другую,
                // и каждый ловит свою перспективу.
                double ty = -pxv * S->pan_gain * DEG_R, tp = pyv * S->pan_gain * DEG_R;
                double zp = S->pivot_z;
                yaw[i] = ty / DEG_R; pitch[i] = tp / DEG_R;
                cx[i] = -zp * sin(ty) * cos(tp);
                cy[i] =  zp * sin(tp);
                cz[i] =  zp * (1.0 - cos(ty) * cos(tp));
            } else {
                cx[i] = -pxv * RIG_ZREF * hw / f;
                cy[i] = pyv * RIG_ZREF * hh / f;
                cz[i] = 0.0;
                yaw[i] = pitch[i] = 0.0;
            }
            cz[i] += z_min * (1.0 - 1.0 / sxv);   // наезд асимптотит к z_min ЭТОЙ сцены (поверх облёта)
            roll[i] = (S->camn_rz > 0 ? curve_at(S->cam_trz, S->cam_vrz, S->camn_rz, t) : 0.0) + S->cam_roll;
        }
        for (int li = 0; li < g_rig.nlayer; li++) {
            if (g_rig.L[li].scene != s) continue;   // слой не этой сцены — его двигает своя камера
            double bx = g_rig.L[li].bx, by = g_rig.L[li].by, z = g_rig.L[li].z, bs = g_rig.L[li].base_scale;
            double br = g_rig.L[li].base_rot, sp = g_rig.L[li].speed, tl0 = S->cam_w0 - g_rig.L[li].tstart;
            static double vx[RIG_MAXK], vy[RIG_MAXK], vs[RIG_MAXK], vyr[RIG_MAXK], ot[RIG_MAXK], vr[RIG_MAXK];
            for (int i = 0; i < n; i++) {   // считаем ВСЕГДА (нужно и рендеру, и документу)
                double d = z - cz[i]; if (d < 1e-3) d = 1e-3;
                double pxp = (bx * z - f * cx[i]) / d, pyp = (by * z - f * cy[i]) / d, sc = z / d;
                if (roll[i] != 0.0) {   // РОЛЛ: вращаем позицию слоя вокруг центра на угол ролла
                    double rr = roll[i] * 3.14159265358979323846 / 180.0, cs = cos(rr), sn = sin(rr);
                    double rx = pxp * cs - pyp * sn, ry = pxp * sn + pyp * cs;
                    pxp = rx; pyp = ry;
                }
                vx[i] = pxp / hw; vy[i] = RIG_SIGN_Y * pyp / hh; vs[i] = bs * sc;
                // «НЕ ВЫХОДИТЬ ЗА РАМКУ» (галочка в панели): камера может увести слой так, что
                // у края кадра появится дыра. Держим слой так, чтобы он перекрывал канвас:
                // допустимый сдвиг = сколько слой выступает за края при текущем масштабе.
                // Ограничиваем ПОСЛЕ проекции — тогда параллакс сохраняется, а дыра не появляется.
                if (g_rig.keepin) {
                    double half = vs[i] * 0.5;              // полуразмер слоя в долях канваса
                    double lim = half > 0.5 ? (half - 0.5) * 2.0 : 0.0;   // запас хода в NDC
                    if (vx[i] >  lim) vx[i] =  lim;
                    if (vx[i] < -lim) vx[i] = -lim;
                    if (vy[i] >  lim) vy[i] =  lim;
                    if (vy[i] < -lim) vy[i] = -lim;
                }
                vyr[i] = -vy[i];   // рендер "py" инвертирован по Y относительно документного KFTypePositionY
                vr[i] = br + roll[i];
                ot[i] = g_rig.L[li].sstart + (tl0 + tt[i]) * sp;   // источник-время слоя (со скоростью клипа)
            }
            // Нужна ли перспектива этому слою: развёрнута камера или наклонена его плоскость.
            int want_cp_live = S->pan_rot || g_rig.L[li].tiltx != 0.0 || g_rig.L[li].tilty != 0.0;
            // РЕНДЕР (живой визуал) — только если рендер-клип ВАЛИДЕН (vtable-страж от use-after-free)
            void *clip = g_rig.L[li].clip;
            if (has_vtable(clip) && *(void **)clip == g_rig.L[li].vtable) {
                const char *keys[5] = {"px", "py", "sx", "sy", "rz"};
                const double *vals[5] = {vx, vyr, vs, vs, vr};
                const KfEase *ce[5] = {S->epx, S->epy, S->esx, S->esx, S->erz};   // easing ЭТОЙ сцены
                const double *cv[5] = {S->cam_vpx, S->cam_vpy, S->cam_vsx, S->cam_vsx, S->cam_vrz};
                const int    cc[5]  = {S->camn_px, S->camn_py, S->camn_sx, S->camn_sx, S->camn_rz};
                // При перспективе положение/масштаб слоя целиком описаны УГЛАМИ. Слать заодно
                // px/py/sx было бы двойным преобразованием — ровно на этом кадр ужимался вдвое.
                // Когда углы НЕ идут в документ, там остаются обычные ключи параллакса, и CapCut
                // сам синкает их в рендер. Тогда px/py/sx мы не шлём (иначе дубль), а углы даём
                // ЛОКАЛЬНЫМИ — поверх уже применённого трансформа.
                int nk = want_cp_live ? 0 : 5;   // rz пишем ВСЕГДА: vr=base_rot затирает застрявшую rotation при отсутствии ролла
                static KfEase oe[RIG_MAXK];
                for (int k = 0; k < nk; k++) {
                    char json[8192];
                    const KfEase *use = 0;
                    if (cc[k] == n) { scale_ease(ce[k], cv[k], vals[k], oe, n); use = oe; for (int i = 0; i < n; i++) if (oe[i].cubic) { eased++; break; } }
                    if (!build_curve_arrays(keys[k], tt, vals[k], use, n, json, sizeof json)) continue;
                    char *jheap = (char *)malloc(strlen(json) + 8); unsigned char jobj[24];
                    makeAltStr(json, jobj, jheap, (int)strlen(json) + 8);
                    unsigned char kobj[24]; char kh[8]; makeAltStr(keys[k], kobj, kh, sizeof kh);
                    ((fwd_t)g[0].tramp)(clip, kobj, jobj);   // TEClip.setKeyframe(clipСлоя, "px", curveJSON)
                    free(jheap);
                }
                if (want_cp_live) {
                    // ЖИВАЯ ПЕРСПЕКТИВА: четыре угла тем же каналом, что и параллакс. Имена ключей
                    // ("ul"/"ur"/"dl"/"dr", значение парой) подсмотрены у самого CapCut в логе.
                    // Углы ВСЕГДА ЛИНЕЙНЫЕ: парный "cubic" роняет рендер (поймано крашем). Кривую
                    // камеры проявляем через ПЛОТНУЮ сетку (persp_grid) — поза на каждой точке
                    // считается по кривой из документа (cam_pose_at), а форма получается частыми
                    // линейными сегментами.
                    static const char *rk[4] = {"ul", "ur", "dl", "dr"};
                    static double cq[RIG_MAXK * 2], pt[RIG_MAXK];
                    int pn = persp_grid(S, tt, n, pt, RIG_MAXK);
                    if (pn > n) cpeased += 4;   // сетка дробилась = кривая проявляется
                    for (int k = 0; k < 4; k++) {
                        for (int i = 0; i < pn; i++) {
                            double q[8];
                            CamPose cp = cam_pose_at(S, pt[i], f, hw, hh, z_min);
                            layer_corners(li, S, cp.cx, cp.cy, cp.cz, cp.roll, f, hw, hh, q, cp.yaw, cp.pitch);
                            cq[i * 2] = q[k * 2]; cq[i * 2 + 1] = q[k * 2 + 1];
                        }
                        char json[8192];
                        if (!build_curve_arrays2(rk[k], pt, cq, 0, pn, json, sizeof json)) continue;
                        char *jh = (char *)malloc(strlen(json) + 8); unsigned char jo[24];
                        makeAltStr(json, jo, jh, (int)strlen(json) + 8);
                        unsigned char ko[24]; char kh[8]; makeAltStr(rk[k], ko, kh, sizeof kh);
                        ((fwd_t)g[0].tramp)(clip, ko, jo);
                        free(jh);
                    }
                }
                wrote++;
            } else { g_rig.L[li].clip = 0; }   // висячий -> сброс (перезахватится на px-синке слоя)
            // ДОКУМЕНТ (персист) — по doc-id, НЕ зависит от рендер-клипа (плоский резолв: getseg-карта scene-агностична)
            if (persist_now) {
                void *byid = seg_by_id(g_rig.L[li].id);
                void *bymap = byid ? 0 : (g_live_id[li][0] ? seg_by_id(g_live_id[li]) : 0);   // живой id из ve_segmap.txt — точный матч, обходит стрей-дубли пути
                void *bypth = (byid || bymap) ? 0 : seg_by_path(g_rig.L[li].path);   // id устарел (объект ушёл в компаунд) -> по ПУТИ
                void *byrec = (byid || bymap || bypth) ? 0 : seg_recursive(li, g_rig.L[li].path);   // свеже завёрнутые: нет в getseg-карте -> полный обход
                void *dseg = byid ? byid : (bymap ? bymap : (bypth ? bypth : byrec));
                if (bymap || bypth || byrec) bypath++;
                if (dseg) {
                    static KfEase oex[RIG_MAXK], oey[RIG_MAXK], oes[RIG_MAXK], oer[RIG_MAXK];
                    KfEase *ex = 0, *ey = 0, *es = 0, *er = 0;
                    if (S->camn_px == n) { scale_ease(S->epx, S->cam_vpx, vx, oex, n); ex = oex; }
                    if (S->camn_py == n) { scale_ease(S->epy, S->cam_vpy, vy, oey, n); ey = oey; }
                    if (S->camn_sx == n) { scale_ease(S->esx, S->cam_vsx, vs, oes, n); es = oes; }
                    if (S->camn_rz == n) { scale_ease(S->erz, S->cam_vrz, vr, oer, n); er = oer; }
                    // Углы нужны -> позицию/масштаб/поворот НЕ пишем. Движок СКЛАДЫВАЕТ corner-pin
                    // с трансформом клипа, а не заменяет его: при обоих сразу слой съезжал и
                    // сжимался (замерено — на середине панорамы кадр ужимался вдвое). Четыре
                    // угла и так описывают положение целиком. Так же устроен планарный трекинг.
                    int cp_now = S->pan_rot || g_rig.L[li].tiltx != 0.0 || g_rig.L[li].tilty != 0.0;
                    // Трек СНОСИМ, а не обнуляем. Обнулённый (n=0) трек остаётся в документе как
                    // свойство без единой точки — в родном редакторе кривых это живая строка
                    // «Масштаб/x/y», по которой пользователь щёлкает, а она ничего не делает:
                    // положение слоя целиком описано углами corner-pin. Мёртвый UI хуже
                    // отсутствующего. Кил-свитч ve_no_tidy.txt возвращает старое поведение
                    // (обнулять, не сносить) — на случай если захват рендер-клипа, который живёт
                    // на синке "px"/"ul", вдруг держался за пустой px-трек: тогда DRIVE начнёт
                    // писать 0/N, и выключить можно без пересборки.
                    static const char *PCLR[4] = {"KFTypePositionX", "KFTypePositionY",
                                                  "KFTypeScaleX", "KFTypeRotation"};
                    if (cp_now) {
                        if (no_tidy) {
                            for (int k = 0; k < 4; k++) doc_write_curves(dseg, PCLR[k], 0, 0, 0, 0);
                        } else {
                            for (int k = 0; k < 4; k++) doc_drop_curves(dseg, PCLR[k]);
                            tidied++;
                        }
                    } else {
                        doc_write_curves(dseg, "KFTypePositionX", ot, vx, ex, n);
                        doc_write_curves(dseg, "KFTypePositionY", ot, vy, ey, n);
                        doc_write_curves(dseg, "KFTypeScaleX", ot, vs, es, n);   // ТОЛЬКО ScaleX: uniform_scale.on сам тянет Y
                        doc_write_curves(dseg, "KFTypeRotation", ot, vr, er, n);
                    }
                    // НАКЛОН ПЛОСКОСТИ (D19): плоский слой перестаёт быть биллбордом — четыре его
                    // угла проецируются камерой ПО ОТДЕЛЬНОСТИ, каждый со своей глубиной. Отсюда
                    // настоящая перспектива: дальний край сходится. Пишем corner-pin теми же
                    // документными кривыми — тот путь, что уже доказан планарным трекером.
                    // Углы нужны, когда слой перестаёт быть прямоугольником: либо камера
                    // РАЗВЁРНУТА (эмуляция 3D — тогда искажаются ВСЕ слои сцены), либо у самого
                    // слоя задан наклон плоскости.
                    int want_cp = S->pan_rot || g_rig.L[li].tiltx != 0.0 || g_rig.L[li].tilty != 0.0;
                    // Перестали быть нужны — снять НАШИ углы, иначе слой навсегда остался бы кривым.
                    // Помним, кому мы их ставили: у слоя может быть чужой corner-pin (планарный
                    // трек), и стирать его нельзя.
                    if (!want_cp && tilt_forget(g_rig.L[li].id)) {
                        for (int k = 0; k < 4; k++) doc_write_curves(dseg, kCP[k], 0, 0, 0, 0);
                        cp_disable(dseg);
                        Lf("%ld ПЕРСПЕКТИВА снята со слоя %s\n", (long)time(NULL), g_rig.L[li].id);
                    }
                    if (want_cp) {
                        tilt_remember(g_rig.L[li].id);
                        // СНАЧАЛА включить corner-pin на сегменте: у клипа без него поле пустое,
                        // и документные ключи углов туда не приживаются (молча пропадали).
                        // Значения берём на первом времени — дальше их переопределят кривые.
                        double q0[8];
                        layer_corners(li, S, cx[0], cy[0], cz[0], roll[0], f, hw, hh, q0, yaw[0], pitch[0]);
                        cp_apply(dseg, q0);
                        // ДОКУМЕНТ = ровно то же, что в живом рендере: та же плотная сетка ЛИНЕЙНЫХ
                        // ключей по кривой из документа. Никаких безье-ручек (createCommonPoint) —
                        // именно они роняли CapCut. Поэтому экспорт совпадёт с превью и не крашит.
                        static double cvx[RIG_MAXK * 2], dpt[RIG_MAXK], dot[RIG_MAXK];
                        int dpn = persp_grid(S, tt, n, dpt, RIG_MAXK);
                        for (int i = 0; i < dpn; i++)
                            dot[i] = g_rig.L[li].sstart + (tl0 + dpt[i]) * sp;   // источник-время слоя
                        for (int k = 0; k < 4; k++) {
                            for (int i = 0; i < dpn; i++) {
                                double q[8];
                                CamPose cp = cam_pose_at(S, dpt[i], f, hw, hh, z_min);
                                layer_corners(li, S, cp.cx, cp.cy, cp.cz, cp.roll, f, hw, hh, q, cp.yaw, cp.pitch);
                                cvx[i * 2] = q[k * 2]; cvx[i * 2 + 1] = q[k * 2 + 1];
                            }
                            doc_write_curves(dseg, kCP[k], dot, cvx, 0, dpn, 2);
                        }
                        tilted++;
                    }
                    persisted++;
                }
            }
        }
        // ПЕРСИСТ САМОЙ КАМЕРЫ — ТОЛЬКО пока в документе НЕТ её кривой (docn_px==0). Как только у
        // камеры появились ключи (питон пишет их при создании, а пользователь правит родным
        // редактором), персист ВРЕДЕН: он писал ease из рендер-синка поверх документа и КАЖДЫЙ
        // драйв сплющивал кривую в прямую — та самая «появилась на секунду и скинулась». Кривую
        // камеры теперь ведёт документ (CAMEASE), дилиб её не трогает.
        if (persist_now && S->cam_id[0] && S->docn_px == 0) {
            void *cs = seg_by_id(S->cam_id);
            if (cs) {
                doc_write_curves(cs, "KFTypePositionX", S->cam_tpx, S->cam_vpx, 0, S->camn_px);
                doc_write_curves(cs, "KFTypePositionY", S->cam_tpy, S->cam_vpy, 0, S->camn_py);
                doc_write_curves(cs, "KFTypeScaleX", S->cam_tsx, S->cam_vsx, 0, S->camn_sx);
            }
        }
        if (dof_on) {
            // Сетка блюра = времена камеры ∪ времена ключей фокуса/диафрагмы (+ дробление
            // безье-участков). Без объединения ключ фокуса, поставленный НЕ в момент ключа
            // камеры, в выборку не попадал — анимация фокуса просто не работала. Параллакс
            // считаем на СВОЕЙ сетке `tt` и не трогаем: там easing пересаживается по индексу
            // (scale_ease + проверка cc[k]==n), лишние точки сломали бы кривые камеры.
            static double bt[RIG_MAXK], bcz[RIG_MAXK], sub[RIG_MAXK];
            const double *srcs[3] = {tt, g_dof_tsy, g_dof_tsx};
            const int cnts[3] = {n, g_dofn_sy, g_dofn_sx};
            int bn = merge_times(srcs, cnts, 3, bt, RIG_MAXK);
            int sn = 0;   // дробление: между ключами фокуса с кривой линейный отрезок соврал бы
            for (int i = 0; i < bn && sn < RIG_MAXK; i++) {
                sub[sn++] = bt[i];
                if (i + 1 >= bn || g_dofn_sy < 2) continue;
                int eased_gap = 0;
                for (int k = 1; k < g_dofn_sy; k++)
                    if (g_dof_tsy[k - 1] <= bt[i] && g_dof_tsy[k] >= bt[i + 1]
                        && (g_dof_esy[k].cubic || g_dof_esy[k - 1].cubic)) { eased_gap = 1; break; }
                if (!eased_gap) continue;
                for (int q = 1; q <= 3 && sn < RIG_MAXK; q++)
                    sub[sn++] = bt[i] + (bt[i + 1] - bt[i]) * q / 4.0;
            }
            for (int i = 0; i < sn; i++) bcz[i] = curve_at(tt, cz, n, sub[i]);
            blur += drive_blur(s, bcz, sub, sn);   // блюр слоёв этой сцены по ЕЁ cz
        }
    }
    if (wrote) seq_refresh();   // ОДИН раз после всех сцен: флаги + commit (голый commit молча выходил)
    g_driving = 0;
    Lf("%ld DRIVE scenes=%d wrote %d/%d layers (eased=%d, поПути=%d, blur=%d, перспектива=%d, кривых=%d, прибрано=%d)%s\n", (long)time(NULL), g_nscene, wrote, g_rig.nlayer,
       eased, bypath, blur, tilted, cpeased, tidied, persist_now ? (persisted ? " +ДОК-ключи" : " +док:сегментов нет") : "");
}
static void drive_layers_main(void *_) { drive_layers(); }

// ==== CORNER-PIN (перспектива) — живой драйв без переоткрытия ====
// Строим lvve::CornerPin{ CornerPinInfo(8 углов) } и вешаем на живой SegmentVideo::set_corner_pin, потом commit -> рендер.
// Координаты — нормированные UV [0,1], верх-слева; единичный квадрат = без искажения (UL0,0 UR1,0 LL0,1 LR1,1).
static void *p_cpinfo_ctor = 0, *p_cp_ctor = 0, *p_seg_set_cornerpin = 0;
static void *p_cp_set_info = 0, *p_cp_set_enable = 0;
static void *p_seg_get_cornerpin = 0, *p_cp_get_info = 0;   // read-back: что РЕАЛЬНО лежит в сегменте
// (старое) Правка ключа НА МЕСТЕ. Ключ на плейхеде создаёт сам CapCut (ромб) — нам остаётся починить его
// значения. Так не нужен ни ридер кривой, ни свой список ключей, ни клон-шаблоны doc_write_curves.
// ponytail: обновляем только точку РОВНО на плейхеде; если её нет — молча ничего (юзер жмёт ромб).
//           Ключ на другом времени не двигаем — потолок известный, upgrade = вставка точки.
static void *p_seg_get_ckfs = 0, *p_kfs_get_ptype = 0, *p_kfs_get_list = 0, *p_kf_get_toff = 0, *p_kf_get_values = 0;
struct VecD { void *b, *e, *c; };   // libc++ vector<T>: {begin,end,cap}

// Сколько ключей ЛЕЖИТ на сегменте по имени свойства. Тот же обход, что у cp_collect, но без
// фильтра по углам. -1 = спросить нечем (нет аксессоров/сегмент мёртв), 0 = трека нет.
// Читаем СРАЗУ ПОСЛЕ записи: это единственный способ отличить «записали» от «позвали запись».
static int kf_count_prop(void *seg, const char *prop) {
    if (!(p_seg_get_ckfs && p_kfs_get_ptype && p_kfs_get_list)) return -1;
    if (!has_vtable(seg)) return -1;
    VecD *ckfs = (VecD *)((void *(*)(void *))p_seg_get_ckfs)(seg);
    if (!ckfs || !sane_ptr(ckfs->b)) return -1;
    for (void **sp = (void **)ckfs->b; sp < (void **)ckfs->e; sp += 2) {
        if (!has_vtable(*sp)) continue;
        char pt[64] = {0}; readAltString(((void *(*)(void *))p_kfs_get_ptype)(*sp), pt, sizeof pt);
        if (strcmp(pt, prop)) continue;
        VecD *l = (VecD *)((void *(*)(void *))p_kfs_get_list)(*sp);
        if (!l || !sane_ptr(l->b)) return 0;
        return (int)(((void **)l->e - (void **)l->b) / 2);
    }
    return 0;
}

// Все get_* — field-getter'ы: возвращают УКАЗАТЕЛЬ на поле, не значение.
static int cp_write_keys(void *seg, const double *c, long long t) {
    if (!(p_seg_get_ckfs && p_kfs_get_ptype && p_kfs_get_list && p_kf_get_toff && p_set_values)) {
        static int w = 0; if (!w) { w = 1; Lf("CPKEYS нет символов: ckfs=%p pt=%p list=%p toff=%p sv=%p\n",
            p_seg_get_ckfs, p_kfs_get_ptype, p_kfs_get_list, p_kf_get_toff, (void*)p_set_values); }
        return 0; }
    if (!has_vtable(seg)) return 0;
    // seg+0xa8 = shared_ptr<NodeArray<CommonKeyframes>>; геттер сам разыменовывает и отдаёт
    // вектор внутри (ldr x8,[x0,#0xa8]; add x0,x8,#0x30).
    VecD *ckfs = (VecD *)((void *(*)(void *))p_seg_get_ckfs)(seg);   // vector<shared_ptr<CommonKeyframes>>
    if (!ckfs || !sane_ptr(ckfs->b)) return 0;
    int hit = 0;
    static long dumped = 0;   // дамп раз в секунду: у каких углов есть ключ на плейхеде
    { long nw = (long)time(NULL); if (nw != dumped) { dumped = nw;
        Lf("CPDUMP кривых=%ld playhead=%lld\n", ((char *)ckfs->e - (char *)ckfs->b) / 16, g_playhead);
        for (void **sp = (void **)ckfs->b; sp < (void **)ckfs->e; sp += 2) {
            if (!has_vtable(*sp)) continue;
            char pt[64] = {0}; readAltString(((void *(*)(void *))p_kfs_get_ptype)(*sp), pt, sizeof pt);
            VecD *l = (VecD *)((void *(*)(void *))p_kfs_get_list)(*sp);
            char tb[160] = {0}; int o = 0;
            if (l && sane_ptr(l->b))
                for (void **kp = (void **)l->b; kp < (void **)l->e && o < 140; kp += 2) {
                    if (!has_vtable(*kp)) continue;
                    long long *t = (long long *)((void *(*)(void *))p_kf_get_toff)(*kp);
                    o += snprintf(tb + o, sizeof tb - o, "%lld ", t ? *t : -1);
                }
            Lf("CPDUMP   [%s] времена: %s\n", pt, tb);
        }
    } }
    for (void **sp = (void **)ckfs->b; sp < (void **)ckfs->e; sp += 2) {   // shared_ptr = {obj,ctrl}
        void *kfs = *sp;
        if (!has_vtable(kfs)) continue;
        char pt[64] = {0};
        readAltString(((void *(*)(void *))p_kfs_get_ptype)(kfs), pt, sizeof pt);
        int idx = -1;
        for (int i = 0; i < 4; i++) if (!strcmp(pt, kCP[i])) { idx = i; break; }
        if (idx < 0) continue;
        VecD *list = (VecD *)((void *(*)(void *))p_kfs_get_list)(kfs);    // vector<shared_ptr<CommonKeyframe>>
        if (!list || !sane_ptr(list->b)) continue;
        for (void **kp = (void **)list->b; kp < (void **)list->e; kp += 2) {
            void *kf = *kp;
            if (!has_vtable(kf)) continue;
            long long *toff = (long long *)((void *(*)(void *))p_kf_get_toff)(kf);
            if (!toff || *toff != t) continue;                            // только точка на плейхеде
            double v[2] = { c[idx * 2], c[idx * 2 + 1] };                 // пара [x,y] этого угла
            VecD vec = { v, v + 2, v + 2 };
            p_set_values(kf, &vec);
            { static long lt = 0; long nw = (long)time(NULL); if (nw != lt || idx == 3) { lt = nw;
                VecD *rb = (VecD *)((void *(*)(void *))p_kf_get_values)(kf);   // читаем ОБРАТНО: легла ли?
                Lf("CPSET idx=%d [%s] want=%.4f,%.4f -> got=%.4f,%.4f kf=%p\n", idx, pt, v[0], v[1],
                   (rb && sane_ptr(rb->b)) ? ((double*)rb->b)[0] : -999,
                   (rb && sane_ptr(rb->b)) ? ((double*)rb->b)[1] : -999, kf); } }
            hit++;
        }
    }
    return hit;
}
static void *g_cp_seg = 0;   // последний сегмент, в который писали (для read-back перед захватом)
// Оба геттера — field-getter'ы (возвращают &shared_ptr), поэтому разыменовываем дважды.
// CornerPinInfo: 8 doubles на +0x30 (UL_x,UL_y,UR_x,UR_y,LL_x,LL_y,LR_x,LR_y).
// Углы НА ВРЕМЕНИ t из ключей. Поле corner_pin для этого не годится: restore кладёт интерполяцию
// куда-то в рендер, а в документном сегменте остаётся ПОСЛЕДНЕЕ записанное значение — из-за этого
// ручки замирали на последнем ключе вместо того, чтобы ехать по анимации.
// ponytail: линейная интерполяция. Безье/easing игнорим — для корнер-пина кривые в UI всё равно
//           пока не работают (тип не в UI-таблицах); появятся — звать движковый
//           KeyframePropertyHelper::getPropertyValues(seg, kfs, prop, t) @0x2fe1e4c.
static int cp_eval_corners(void *seg, long long t, double *out) {
    if (!(p_seg_get_ckfs && p_kfs_get_ptype && p_kfs_get_list && p_kf_get_toff && p_kf_get_values)) return 0;
    if (!has_vtable(seg)) return 0;
    VecD *ckfs = (VecD *)((void *(*)(void *))p_seg_get_ckfs)(seg);
    if (!ckfs || !sane_ptr(ckfs->b)) return 0;
    // out ОБЯЗАН быть инициализирован: заполняем только найденные углы, а в незаполненных иначе
    // остаётся мусор со стека (ловил Y=61.368 при допустимом NDC [-1,1]).
    { double id4[8] = {-1,-1, 1,-1, -1,1, 1,1}; memcpy(out, id4, sizeof id4); }
    int got = 0;
    for (void **sp = (void **)ckfs->b; sp < (void **)ckfs->e; sp += 2) {
        if (!has_vtable(*sp)) continue;
        char pt[64] = {0}; readAltString(((void *(*)(void *))p_kfs_get_ptype)(*sp), pt, sizeof pt);
        int idx = -1; for (int i = 0; i < 4; i++) if (!strcmp(pt, kCP[i])) { idx = i; break; }
        if (idx < 0) continue;
        VecD *l = (VecD *)((void *(*)(void *))p_kfs_get_list)(*sp);
        if (!l || !sane_ptr(l->b)) continue;
        // ключи отсортированы по времени: берём соседей вокруг t
        double pv[2] = {0,0}, nv[2] = {0,0}; long long pt_t = -1, nt_t = -1;
        for (void **kp = (void **)l->b; kp < (void **)l->e; kp += 2) {
            if (!has_vtable(*kp)) continue;
            long long *kt = (long long *)((void *(*)(void *))p_kf_get_toff)(*kp);
            VecD *v = (VecD *)((void *(*)(void *))p_kf_get_values)(*kp);
            if (!kt || !v || !sane_ptr(v->b)) continue;
            long n = ((double *)v->e - (double *)v->b); if (n < 2) continue;
            double x = ((double *)v->b)[0], y = ((double *)v->b)[1];
            if (*kt <= t && (pt_t < 0 || *kt > pt_t)) { pt_t = *kt; pv[0] = x; pv[1] = y; }
            if (*kt >= t && (nt_t < 0 || *kt < nt_t)) { nt_t = *kt; nv[0] = x; nv[1] = y; }
        }
        if (pt_t < 0 && nt_t < 0) continue;
        double f = (pt_t >= 0 && nt_t >= 0 && nt_t != pt_t) ? (double)(t - pt_t) / (double)(nt_t - pt_t) : 0;
        if (pt_t < 0) { out[idx*2] = nv[0]; out[idx*2+1] = nv[1]; }        // левее первого ключа
        else if (nt_t < 0) { out[idx*2] = pv[0]; out[idx*2+1] = pv[1]; }  // правее последнего
        else { out[idx*2] = pv[0] + (nv[0]-pv[0])*f; out[idx*2+1] = pv[1] + (nv[1]-pv[1])*f; }
        got++;
    }
    return got == 4;
}

// ═══ СИНХРОННАЯ ЗАПИСЬ ВСЕХ 4 КРИВЫХ ═══
// Просить onClickSetKeyFrame создать ключ — ненадёжно: групповая механика CapCut НЕ ЗНАЕТ корнер-пина
// (его типов нет в UI-таблицах), поэтому ставит ключи углам вразнобой — ловили [DownRight]=0,1800000
// против [UpLeft]=1800000. Пишем все 4 кривые сами, за один проход: рассинхрон становится невозможен.
#define CP_MAXK 64
struct CpKey { long long t; double v[8]; };
static int cp_eval_corners(void *seg, long long t, double *out);   // forward

// Собирает объединённый список времён всех 4 кривых; значения на каждом времени берёт интерполяцией
// (на самих ключах она возвращает ровно их значение, так что чужие ключи не портятся).
static int cp_collect(void *seg, CpKey *out) {
    if (!(p_seg_get_ckfs && p_kfs_get_ptype && p_kfs_get_list && p_kf_get_toff)) return 0;
    if (!has_vtable(seg)) return 0;
    VecD *ckfs = (VecD *)((void *(*)(void *))p_seg_get_ckfs)(seg);
    if (!ckfs || !sane_ptr(ckfs->b)) return 0;
    long long ts[CP_MAXK]; int n = 0;
    for (void **sp = (void **)ckfs->b; sp < (void **)ckfs->e && n < CP_MAXK; sp += 2) {
        if (!has_vtable(*sp)) continue;
        char pt[64] = {0}; readAltString(((void *(*)(void *))p_kfs_get_ptype)(*sp), pt, sizeof pt);
        int isCp = 0; for (int i = 0; i < 4; i++) if (!strcmp(pt, kCP[i])) isCp = 1;
        if (!isCp) continue;
        VecD *l = (VecD *)((void *(*)(void *))p_kfs_get_list)(*sp);
        if (!l || !sane_ptr(l->b)) continue;
        for (void **kp = (void **)l->b; kp < (void **)l->e && n < CP_MAXK; kp += 2) {
            if (!has_vtable(*kp)) continue;
            long long *kt = (long long *)((void *(*)(void *))p_kf_get_toff)(*kp);
            if (!kt) continue;
            int dup = 0; for (int i = 0; i < n; i++) if (ts[i] == *kt) dup = 1;
            if (!dup) ts[n++] = *kt;
        }
    }
    for (int i = 1; i < n; i++) { long long k = ts[i]; int j = i - 1;      // сортировка вставками
        while (j >= 0 && ts[j] > k) { ts[j + 1] = ts[j]; j--; } ts[j + 1] = k; }
    for (int i = 0; i < n; i++) { out[i].t = ts[i]; cp_eval_corners(seg, ts[i], out[i].v); }
    return n;
}

// Вставляет/заменяет точку на времени t и переписывает все 4 кривые.
static int cp_sync_curves(void *seg, const double *c, long long t) {
    CpKey k[CP_MAXK + 1]; int n = cp_collect(seg, k);
    if (n <= 0) return 0;                       // ключей нет -> это статичный драг, не наше дело
    int at = -1; for (int i = 0; i < n; i++) if (k[i].t == t) at = i;
    if (at < 0) {                               // АВТО-КЕЙФРЕЙМ: точки на плейхеде нет -> вставляем
        if (n >= CP_MAXK) return 0;
        at = n; while (at > 0 && k[at - 1].t > t) { k[at] = k[at - 1]; at--; }
        k[at].t = t; n++;
    }
    memcpy(k[at].v, c, 8 * sizeof(double));     // наше значение на плейхеде
    double times[CP_MAXK], vals[CP_MAXK * 2];
    for (int idx = 0; idx < 4; idx++) {
        for (int i = 0; i < n; i++) { times[i] = (double)k[i].t;
            vals[i * 2] = k[i].v[idx * 2]; vals[i * 2 + 1] = k[i].v[idx * 2 + 1]; }
        doc_write_curves(seg, kCP[idx], times, vals, 0, n, 2);   // nv=2 -> пара [x,y]
    }
    return n;
}

// Читает 8 углов сегмента в out[8]. 0 = corner_pin нет (у свежего клипа он null).
static int cp_read_corners(void *seg, double *out) {
    if (!p_seg_get_cornerpin || !p_cp_get_info || !has_vtable(seg)) return 0;
    void *sp = ((void *(*)(void *))p_seg_get_cornerpin)(seg);
    void *cp = (sp && sane_ptr(sp)) ? *(void **)sp : 0;
    if (!has_vtable(cp)) return 0;
    void *ip = ((void *(*)(void *))p_cp_get_info)(cp);
    void *info = (ip && sane_ptr(ip)) ? *(void **)ip : 0;
    if (!has_vtable(info)) return 0;
    memcpy(out, (char *)info + 0x30, 8 * sizeof(double));
    return 1;
}

static void cp_readback(void *seg, const char *tag) {
    if (!p_seg_get_cornerpin || !p_cp_get_info || !has_vtable(seg)) return;
    void *sp = ((void *(*)(void *))p_seg_get_cornerpin)(seg);
    void *cp = (sp && sane_ptr(sp)) ? *(void **)sp : 0;
    if (!has_vtable(cp)) { Lf("READBACK[%s] CornerPin=NULL\n", tag); return; }
    void *ip = ((void *(*)(void *))p_cp_get_info)(cp);
    void *info = (ip && sane_ptr(ip)) ? *(void **)ip : 0;
    if (!has_vtable(info)) { Lf("READBACK[%s] Info=NULL\n", tag); return; }
    double *d = (double *)((char *)info + 0x30);
    Lf("READBACK[%s] %.4f %.4f  %.4f %.4f  %.4f %.4f  %.4f %.4f\n", tag,
       d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
}
void *p_seg_get_clip = 0; static void *p_seg_set_clip = 0;
// У КАЖДОГО типа сегмента СВОЙ get_clip. Для follower-стикера вызов видео-версии не годится —
// именно поэтому updateClipInfo не отправлялся. Держим оба и пробуем по очереди.
void *p_seg_get_clip_st = 0;   // get_clip=&shptr@seg+0x190; set_clip(get_clip()) метит клип грязным -> commit пересобирает
static void *p_cpi_set[8] = {0};   // порядок: UL_x,UL_y, UR_x,UR_y, LL_x,LL_y, LR_x,LR_y
static void *g_cp_fakevt[8];       // фейковый vtable для leak-safe control-block shared_ptr
static void cc_sp_noop(void *c) { (void)c; }   // никогда не зовётся (счётчик держим высоким), только страховка
// собрать libc++-совместимый shared_ptr {obj, ctrl}: ctrl = [vtable][strong-1][weak-1][obj]; счётчики высокие -> объект не освобождается
static void make_leaky_sp(void *obj, void **sp2) {
    if (!g_cp_fakevt[0]) for (int i = 0; i < 8; i++) g_cp_fakevt[i] = (void *)cc_sp_noop;
    long *c = (long *)malloc(4 * sizeof(long));
    c[0] = (long)g_cp_fakevt; c[1] = 1000; c[2] = 1000; c[3] = (long)obj;
    sp2[0] = obj; sp2[1] = c;
}

// ==== СВОЯ КОМАНДА НА ШИНЕ: ScaleSegmentReqStruct С НУЛЯ =========================
// Зачем: рефреш углов ЖДАЛ, пока пользователь сам подвинет трансформацию — только
// тогда хук ловил чужую команду и её можно было реплеить. До этого в логе
// «replay_cmd cmd=0x0», и перспектива после КАЖДОГО перезапуска выглядела сломанной.
// Теперь строим команду сами: lyra::ServiceApiMapping::makeReq(service, api, json).
// Всё сверено по дизассемблеру libvideoeditor (arm64), не угадано:
//   makeReq: x0=this, x1=service, x2=api, x3=&json, x8=sret shared_ptr<ReqStruct>.
//     Неизвестные service/api -> в sret пишутся нули, БЕЗ исключения (ветка 0x31cd60).
//   service="CommonService", api="scaleSegment" — из таблицы регистрации 0x35f428.
//   Схема json снята с from_json самой структуры (0x36bdf0 -> params 0x36c1f4):
//     {"commit_immediately":bool,"params":{"segment_id":str,"x":double,"y":double,
//      "is_keyframe":bool,"keyframe_id":str,"is_auto_fill_keyframe":bool,
//      "sync_to_all":bool,"sync_to_action_time":bool,"segment_ids":[],"scale_type":int}}
//     Каждый ключ ищется через find() ⇒ ОТСУТСТВУЮЩИЙ КЛЮЧ МОЛЧА ПРОПУСКАЕТСЯ,
//     недобор полей парс не роняет. Поэтому шлём минимум: id + x + y.
//   x/y — АБСОЛЮТНЫЙ масштаб сегмента, поэтому подставляем ТОТ, ЧТО У НЕГО УЖЕ СТОИТ:
//     по данным команда no-op, а её хендлер всё равно пере-конвертит клип и обновит
//     превью — ровно то, ради чего раньше ждали ручного движения.
static void *p_sam_instance = 0, *p_makereq = 0, *p_jsonparse = 0;
static void *p_clip_get_scale = 0, *p_scale_get_x = 0, *p_scale_get_y = 0;
// Текущий масштаб сегмента. Смещения сверены по дизассемблеру геттеров (по 2 инструкции):
// Clip::get_scale = clip+0x30 -> &shptr<Scale>; Scale::get_x = +0x30, get_y = +0x38 (double const&).
static int seg_scale(void *seg, double *sx, double *sy) {
    if (!(p_seg_get_clip && p_clip_get_scale && p_scale_get_x && p_scale_get_y)) return 0;
    if (!has_vtable(seg)) return 0;
    void *cr = ((void *(*)(void *))p_seg_get_clip)(seg);
    void *clip = (cr && sane_ptr(cr)) ? *(void **)cr : 0;
    if (!has_vtable(clip)) return 0;
    void *sr = ((void *(*)(void *))p_clip_get_scale)(clip);
    void *sc = (sr && sane_ptr(sr)) ? *(void **)sr : 0;
    if (!sane_ptr(sc)) return 0;
    const double *px = ((const double *(*)(void *))p_scale_get_x)(sc);
    const double *py = ((const double *(*)(void *))p_scale_get_y)(sc);
    if (!sane_ptr((void *)px) || !sane_ptr((void *)py)) return 0;
    *sx = *px; *sy = *py; return 1;
}
static const char *seg_docid(void *seg) {   // обратный поиск по getseg-карте: живой Segment -> документный id
    for (int i = g_nid - 1; i >= 0; i--) if (g_id_seg[i] == seg) return g_id_str[i];
    return 0;
}
// Выстрел ЛЮБОЙ командой CommonService: json -> makeReq -> Server::invoke. 1 = отправлена.
// Вынесено из cmd_scale_send без изменения логики: у ключей всё то же самое, меняются
// только api и тело json.
static void dump_req_fields(void *req);
static int cmd_send_svc(const char *svc, const char *api, const char *js, int send, unsigned char *respout) {
    if (!(p_sam_instance && p_makereq && p_jsonparse)) return 0;
    unsigned char sobj[24]; char sheap[900]; makeAltStr(js, sobj, sheap, sizeof sheap);
    // nlohmann::basic_json = {value_t type; json_value value} = 16 байт; нули = null, это ВАЛИДНЫЙ json.
    unsigned char jbuf[16] = {0};
    int parsed = ((int (*)(void *, void *))p_jsonparse)(sobj, jbuf);
    void *sam = ((void *(*)(void))p_sam_instance)();
    void *sp[2] = {0, 0};
    if (parsed && sam) call_sret4(sp, sam, (void *)svc, (void *)api, jbuf, p_makereq);
    Lf("%ld MKREQ %s/%s parse=%d req=%p js=%s\n", (long)time(NULL), svc, api, parsed, sp[0], js);
    // Дамп СВОЕГО запроса теми же глазами, что и пойманного чужого: только так видно, каких
    // полей не хватает. Гейт ve_reqdump.txt, чтобы не сорить в обычной работе.
    if (sp[0] && access("/Users/naza.prod/Movies/CapCut/User Data/ve_reqdump.txt", F_OK) == 0)
        dump_req_fields(sp[0]);
    // Вернуть json в null и тем самым освободить разобранное: jsonParse СВОПАЕТ содержимое
    // с временным и убивает временный (сверено по 0x272ba10..0x272ba34) — свой дтор не экспортирован.
    { unsigned char n0[24]; char nh[8]; makeAltStr("null", n0, nh, sizeof nh);
      ((int (*)(void *, void *))p_jsonparse)(n0, jbuf); }
    if (!sp[0] || !send) return 0;
    void *srv = g_srv ? g_srv : (p_server_instance ? ((void *(*)(void))p_server_instance)() : 0);
    if (!srv || !g_tramp_srv) { Lf("%ld MKREQ: нет сервера/трамплина\n", (long)time(NULL)); return 0; }
    if (sp[1]) __atomic_fetch_add((long *)((char *)sp[1] + 8), 1, __ATOMIC_RELAXED);   // retain — как в replay_cmd
    unsigned char resp[80] = {0};   // sret RespStruct
    call_sret3(resp, srv, sp, (void *)g_sid, g_tramp_srv);
    if (respout) memcpy(respout, resp, 32);   // отдать ответ наружу: у get-команд он и есть результат
    Lf("%ld MKREQ отправлена api=%s sid=%ld\n", (long)time(NULL), api, (long)g_sid);
    return 1;
}
static int cmd_send(const char *api, const char *js, int send) {
    return cmd_send_svc("CommonService", api, js, send, 0);
}
// Собрать команду с данным масштабом и (если send) выстрелить в шину. 1 = отправлена.
static int cmd_scale_send(const char *id, double x, double y, int send) {
    char js[512];
    snprintf(js, sizeof js,
             "{\"commit_immediately\":true,\"params\":{\"segment_id\":\"%s\",\"x\":%.17g,\"y\":%.17g}}",
             id, x, y);
    return cmd_send("scaleSegment", js, send);
}
// ve_mkreq.txt: 0 = выключено (старое поведение), 1 = только собрать (без выстрела),
// 3 = САМОПРОВЕРКА (см. ниже), нет файла или 2 = собрать и отправить (рабочий режим).
static int cmd_selfmade(void *seg) {
    static int gate = -1;
    if (gate < 0) { gate = 2;
        FILE *gf = fopen(ud("ve_mkreq.txt"), "r");
        if (gf) { if (fscanf(gf, "%d", &gate) != 1) gate = 2; fclose(gf); } }
    if (!gate) return 0;
    if (!(p_sam_instance && p_makereq && p_jsonparse)) {
        Lf("%ld MKREQ: нет символов inst=%p make=%p parse=%p\n", (long)time(NULL),
           p_sam_instance, p_makereq, p_jsonparse); return 0; }
    const char *id = seg_docid(seg);
    double sx = 0, sy = 0;
    if (!id || !seg_scale(seg, &sx, &sy)) {
        Lf("%ld MKREQ: нет id/масштаба (id=%s)\n", (long)time(NULL), id ? id : "-"); return 0; }
    if (gate == 3) {   // САМОПРОВЕРКА: «команда построилась» ничего не доказывает, пока она не
        double a = 0, b = 0;   // МЕНЯЕТ модель. Шлём заведомо другой масштаб, читаем обратно, возвращаем.
        cmd_scale_send(id, sx * 1.5, sy * 1.5, 1);
        seg_scale(seg, &a, &b);
        Lf("%ld MKREQ тест: было %.4f/%.4f -> послали x1.5 -> стало %.4f/%.4f\n", (long)time(NULL), sx, sy, a, b);
        cmd_scale_send(id, sx, sy, 1);
        seg_scale(seg, &a, &b);
        Lf("%ld MKREQ возврат: стало %.4f/%.4f (эталон %.4f/%.4f)\n", (long)time(NULL), a, b, sx, sy);
        return 1;
    }
    return cmd_scale_send(id, sx, sy, gate >= 2);
}

// Реплей PlayerClient::refreshCurrentFrameї пойманного ReqStruct — ТОТ ЖЕ сигнал рефреша превью, что шлёт хендлер команды (смена масштаба).
// ABI: (a=&sp{obj,ctrl}, b=&std::function, c=bool, d=sid). Пустой (нулевой) function — callee проверяет operator bool -> не зовёт.
static void replay_refresh(void) {
    if (!g_req_obj || !g_req_ctrl || !g_req_tramp) return;
    __atomic_fetch_add((long *)((char *)g_req_ctrl + 8), 1, __ATOMIC_RELAXED);   // retain: by-value shared_ptr в callee заберёт одну ссылку
    void *sp[2] = {g_req_obj, g_req_ctrl};
    unsigned char fn[64] = {0};   // пустой std::function
    ((void (*)(void *, void *, void *, void *))g_req_tramp)(sp, fn, (void *)g_req_bool, (void *)g_req_sid);
}
// Реплей пойманной transform-команды (scale) — её хендлер заново гоняет convert_clip_to_ve на сегменте (несёт corner-pin) + refresh.
// Зовём ОРИГИНАЛ Server::invoke через трамплин (g_tramp_srv), sret RespStruct в локальный буфер.
static void replay_cmd(void) {
    if (!g_cmd_obj || !g_cmd_ctrl || !g_tramp_srv || !p_server_instance) return;
    void *srv = ((void *(*)(void))p_server_instance)();
    if (!srv) return;
    __atomic_fetch_add((long *)((char *)g_cmd_ctrl + 8), 1, __ATOMIC_RELAXED);   // retain: invoke заберёт одну ссылку (shared_ptr by value)
    void *sp[2] = {g_cmd_obj, g_cmd_ctrl};
    unsigned char resp[80] = {0};   // sret RespStruct
    call_sret3(resp, srv, sp, (void *)g_cmd_sid, g_tramp_srv);   // x8=resp, x0=srv, x1=&sp, x2=sid -> Server::invoke
}
// ЧЕСТНЫЙ РЕФРЕШ БЕЗ ЛОВЛИ ЧУЖОЙ КОМАНДЫ: взводим грязные флаги сами и зовём commit.
// СВЕРЕНО ПО ДИЗАССЕМБЛЕРУ (не по заметке) — vesdk::VESequenceImpl::commit @0x1f85cd0:
//   1f85cfc  ldrb w8,[x20,#0x48] ; 1f85d00 ldrb w9,[x20,#0x49]   — читает оба
//   1f85d3c  ldrb w8,[x20,#0x48] ; cbnz -> 1f85d4c (работать)
//   1f85d44  ldrb w8,[x20,#0x49] ; cbz  -> 1f85eb4 (РАННИЙ ВЫХОД)
// Т.е. тело выполняется, только если хоть один флаг взведён. Имена подтверждает ИХ ЖЕ лог
// в этой функции: "[%p]timelineChange=%d, filterChange=%d" (this, byte@0x48, byte@0x49).
// ⇒ «commit сам по себе мёртв» из старых заметок — НЕВЕРНО: он не мёртв, он молча выходил,
// потому что записи в документ этих флагов не трогают.
static void seq_refresh(void) {
    if (!has_vtable(g_seq) || !g[3].tramp) return;
    volatile unsigned char *f = (volatile unsigned char *)g_seq;
    unsigned char b48 = f[0x48], b49 = f[0x49];   // ДО
    f[0x48] = 1;   // timelineChange
    f[0x49] = 1;   // filterChange
    unsigned char s48 = f[0x48], s49 = f[0x49];   // взвелись ли (проверка записи)
    ((fwd_t)g[3].tramp)(g_seq, 0, 0);   // оригинал commit() через трамплин (мы его хукаем)
    unsigned char a48 = f[0x48], a49 = f[0x49];   // ПОСЛЕ
    // По флагам после вызова судить НЕЛЬЗЯ: в commit НЕТ НИ ОДНОГО strb (проверено дизассемблером
    // 0x1f85cd0..0x1f85ecc) — он их не сбрасывает. Логируем как сырьё, без выводов.
    static int logged = 0;
    if (logged < 8) { logged++;
        Lf("%ld SEQREFRESH seq=%p флаги 0x48/0x49: до=%d/%d взвели=%d/%d после=%d/%d\n",
           (long)time(NULL), g_seq, b48, b49, s48, s49, a48, a49); }
}

// Ключи ставим САМИ (cp_sync_curves), а не ромбом: onClickSetKeyFrame — ТОГГЛ, и его групповая механика
// НЕ ЗНАЕТ корнер-пина (его типов нет ни в одной UI-таблице), поэтому ставила ключи углам вразнобой.
static void qml_autokey(int phase);   // forward: авто-кейфрейм через нашу QML-строку (родной путь CapCut)
// Включить corner-pin на сегменте и выставить 8 углов (см. форвард выше).
static void cp_apply(void *seg, const double *c) {
    if (!(p_cpinfo_ctor && p_cp_ctor && p_seg_set_cornerpin && p_cp_set_info && p_cp_set_enable && p_cpi_set[0])) return;
    if (!has_vtable(seg)) return;
    void *info = malloc(0x200); memset(info, 0, 0x200);   // CornerPinInfo ~0x70; с запасом, обнуляем
    ((void (*)(void *, bool))p_cpinfo_ctor)(info, true);
    for (int i = 0; i < 8; i++) ((void (*)(void *, const double *))p_cpi_set[i])(info, &c[i]);
    void *spi[2]; make_leaky_sp(info, spi);
    void *cp = malloc(0x200); memset(cp, 0, 0x200);
    ((void (*)(void *, bool))p_cp_ctor)(cp, true);
    ((void (*)(void *, void *))p_cp_set_info)(cp, spi);            // set_corner_pin_info(shared_ptr const&)
    bool en = true; ((void (*)(void *, const bool *))p_cp_set_enable)(cp, &en);
    void *spc[2]; make_leaky_sp(cp, spc);
    ((void (*)(void *, void *))p_seg_set_cornerpin)(seg, spc);     // SegmentVideo::set_corner_pin(shared_ptr const&)
    if (p_seg_get_clip && p_seg_set_clip) {   // метим КЛИП грязным (re-store того же clip) -> commit пересоберёт (convert_clip_to_ve прочитает corner-pin)
        void *clipref = ((void *(*)(void *))p_seg_get_clip)(seg);   // &shared_ptr<Clip> @seg+0x190
        if (clipref && *(void **)clipref) ((void (*)(void *, void *))p_seg_set_clip)(seg, clipref);
    }
}

// Выключить corner-pin у сегмента: enable=false + пометить клип грязным, чтобы commit пересобрал.
static void cp_disable(void *seg) {
    if (!(p_cp_ctor && p_seg_set_cornerpin && p_cp_set_enable) || !has_vtable(seg)) return;
    void *cp = malloc(0x200); memset(cp, 0, 0x200);
    ((void (*)(void *, bool))p_cp_ctor)(cp, true);
    bool en = false; ((void (*)(void *, const bool *))p_cp_set_enable)(cp, &en);
    void *spc[2]; make_leaky_sp(cp, spc);
    ((void (*)(void *, void *))p_seg_set_cornerpin)(seg, spc);
    if (p_seg_get_clip && p_seg_set_clip) {
        void *clipref = ((void *(*)(void *))p_seg_get_clip)(seg);
        if (clipref && *(void **)clipref) ((void (*)(void *, void *))p_seg_set_clip)(seg, clipref);
    }
}

static void drive_cornerpin(void) {   // ТОЛЬКО main-поток (зовёт API CapCut). Гейт: ve_cornerpin.txt
    if (!(p_cpinfo_ctor && p_cp_ctor && p_seg_set_cornerpin && p_cp_set_info && p_cp_set_enable && p_cpi_set[0])) return;
    if (!has_vtable(g_seq)) return;
    FILE *f = fopen(ud("ve_cornerpin.txt"), "r");
    if (!f) return;
    char line[512]; int applied = 0;
    while (fgets(line, sizeof line, f)) {
        char path[320]; double c[8];
        if (sscanf(line, "%319s %lf %lf %lf %lf %lf %lf %lf %lf",
                   path, &c[0], &c[1], &c[2], &c[3], &c[4], &c[5], &c[6], &c[7]) != 9) continue;
        void *seg = strchr(path, '/') ? seg_by_path(path) : seg_by_id(path);   // '/' -> путь; иначе segment-id (надёжнее: get_material тут пуст)
        if (!seg) continue;
        cp_apply(seg, c);
        // Пишем все 4 кривые сами, за один проход (точка на плейхеде вставляется, если её нет).
        // Рассинхрон углов тут невозможен по конструкции.
        long long ph = cp_playhead();
        // Засев шаблонов — ТОЛЬКО под гейтом снимка: без них doc_write_curves молча выходит, и это
        // было прежнее поведение (ключи углов в документ не писались вовсе). Включать глобально
        // нельзя: это меняет работу corner-pin у соседей, а ветка ещё непроверенная.
        if (g_kfsnap) seed_templates();
        int kh = cp_sync_curves(seg, c, ph);
        // СНИМОК КЛЮЧА РОДНОЙ КОМАНДОЙ (гейт ve_kfsnap.txt) — ПОСЛЕ записи в документ, и это принципиально.
        // Замерено: addCommonKeyframe/updateCommonKeyframe значение НЕ несут (datas и string_value
        // движок игнорирует) и не смотрят на поле corner_pin — ключ создаётся со значением,
        // взятым из ТРЕКА КЛЮЧЕЙ на этом времени. Значит сперва наше значение должно лечь в трек
        // (cp_sync_curves), и только потом команда — тогда движок «снимет» уже наше и синкнет рендер.
        // ⚠️ play_head — время ТАЙМЛАЙНА (замер: 100000 -> ключ на 78657 = ×source/target).
        // Здесь шлём cp_playhead() как есть: на стенде сегмент начинается с нуля, шкалы совпадают.
        if (g_kfsnap) {
            const char *did = seg_docid(seg);
            if (did) for (int i = 0; i < 4; i++) {
                char js[512];
                snprintf(js, sizeof js,
                         "{\"commit_immediately\":true,\"params\":{\"seg_id\":\"%s\",\"keyframeType\":\"%s\","
                         "\"dataParam\":{\"play_head\":%lld,\"curveType\":\"Line\"}}}", did, kCP[i], ph);
                cmd_send("addCommonKeyframe", js, 1);
            }
            double got[8] = {0}; cp_eval_corners(seg, ph, got);
            Lf("%ld KFSNAP ph=%lld tmpl=%p -> документ: UL %.4f,%.4f UR %.4f,%.4f LL %.4f,%.4f LR %.4f,%.4f\n",
               (long)time(NULL), ph, g_tmpl_kf, got[0], got[1], got[2], got[3], got[4], got[5], got[6], got[7]);
        }
        { static int n = 0; if (n < 8) { n++; Lf("CPKEYS ph=%lld raw=%lld точек=%d\n", ph, g_playhead, kh); } }
        // ДИАГ: сравниваем ЖЕЛАЕМОЕ (файл) с тем, что реально осело в сегменте сразу после записи.
        // Расхождение здесь = пишем не в тот объект; совпадение = клобер приходит позже (restore).
        { static int n = 0; if (n < 6) { n++;
            Lf("WANT      %.4f %.4f  %.4f %.4f  %.4f %.4f  %.4f %.4f  | playhead=%.6f\n",
               c[0],c[1],c[2],c[3],c[4],c[5],c[6],c[7], (double)g_playhead);
            cp_readback(seg, "after-set"); } }
        g_cp_seg = seg;   // запоминаем для read-back прямо перед захватом ключа
        applied++;
    }
    fclose(f);
    if (!applied) {   // ДИАГНОСТИКА (разово): что реально в getseg-карте — id, SegmentVideo?, живой путь
        static int dumped = 0;
        if (!dumped && g_nid > 0) { dumped = 1;
            for (int i = g_nid - 1; i >= 0; i--) {
                void *seg = g_id_seg[i];
                int live = (seg && has_vtable(seg) && *(void **)seg == g_id_vt[i]);
                int isv = (live && *(void **)seg == g_segvideo_vt);
                Lf("%ld CPDUMP id=%s live=%d segVideo=%d path=[%s]\n", (long)time(NULL), g_id_str[i], live, isv, g_id_path[i]);
            }
        }
    }
    if (applied) {
        // ПОРЯДОК ВАЖЕН: пока модель ещё держит наше значение (до commit/replay), даём CapCut
        // записать его в ключ РОДНЫМ путём. Иначе replay -> convert -> restorePropertyValuesFromKeyframe
        // затрёт наш set_corner_pin значением из ключа (это и есть «превью замерло после ключа»).
        // Если ключей нет, pluginApply() сам ничего не делает — keyless-драг работает как раньше.
        { static int n = 0; if (n < 6) { n++; cp_readback(g_cp_seg, "pre-key"); } }   // что увидит захват?
        { static int n = 0; if (n < 6) { n++; cp_readback(g_cp_seg, "post-key"); } }
        seq_refresh();      // взвести timelineChange/filterChange + commit -> рендер перечитывает модель
        int made = cmd_selfmade(g_cp_seg);   // СВОЯ команда: не ждём, пока пользователь подвигает трансформацию
        replay_cmd();       // СТАРЫЙ КОСТЫЛЬ: реплей чужой команды. Отключается пустым ve_cpcmd.txt
        replay_refresh();   // (g_cmd_obj остаётся 0 -> обе no-op) — так гоняем A/B с seq_refresh
        g_cp_pending = 0; g_cp_tries = 0;
        Lf("%ld CORNERPIN applied=%d selfmade=%d +cmdReplay(cmd=%p type=%s)\n",
           (long)time(NULL), applied, made, g_cmd_obj, g_cmd_type);
    }
    else if (++g_cp_tries < 40) g_cp_pending = 1;   // сегмент ещё не в getseg-карте (проект грузится) -> ретрай через тик
    else Lf("%ld CORNERPIN give-up (сегмент не найден по пути)\n", (long)time(NULL));
}
static void drive_cornerpin_main(void *_) { drive_cornerpin(); }

// ==== КЛЮЧ СВОЕЙ КОМАНДОЙ (ветка непроверенная, гейт ve_kfreq.txt) ==============
// Зачем: ре-конверт по scaleSegment восстанавливает свойство из ключей РЕНДЕРА, а его копия
// ключей синкается только на загрузке проекта ⇒ живая правка ключа до картинки не доходит.
// Пробуем санкционированный путь: та же фабрика makeReq, но api для ключей.
// Схема снята с from_json структуры (arm64-срез libvideoeditor, xref на строку api @0x3604f0):
//   params:    seg_id, keyframeType, addPlaceholder, material_id, dataParam
//   dataParam: play_head, curveType, datas, string_value, left_control, right_control, graphData
// Имена — ровно те литералы, с которыми сравнивает from_json (лежат сплошным блоком @0x43efb70).
// ⚠️ Грабля: 'voice' по соседству — это ХВОСТ строки 'is_clear_all_changevoice', не поле.
// Отсутствующий ключ движок молча пропускает, поэтому шлём минимум: id + тип + время + значения.
// ve_kfreq.txt: строка 1 = «<api> <segid> <t_мкс|-1>», дальше СЫРОЙ json команды.
// Сырой json намеренно: схему подбираем замерами, и новая форма не должна стоить пересборки.
// Файла нет = ветка выключена. Время -1 = текущий плейхед (туда, куда смотрит пользователь).
// Логируем ЧИСЛО ключей и углы на t до/после: «команда собралась» не значит ничего, а по одним
// углам не видно созданного ключа — движок ставит его с ТЕКУЩИМ значением, и углы не меняются.
static void drive_kfreq(void) {
    FILE *f = fopen(ud("ve_kfreq.txt"), "r");
    if (!f) return;
    char head[256], api[64], segid[64], js[768]; long long t = -1; int ok = 0;
    if (fgets(head, sizeof head, f) && sscanf(head, "%63s %63s %lld", api, segid, &t) == 3) {
        size_t n = fread(js, 1, sizeof js - 1, f); js[n] = 0; ok = (n > 2);
    }
    fclose(f);
    if (!ok) { Lf("%ld KFREQ: не разобрал (нужны строка-заголовок и json)\n", (long)time(NULL)); return; }
    if (t < 0) t = cp_playhead();
    void *seg = seg_by_id(segid);
    CpKey k[CP_MAXK + 1];
    double b[8] = {0}, a[8] = {0}; int nb = 0, na = 0;
    if (seg) { nb = cp_collect(seg, k); cp_eval_corners(seg, t, b); }
    int sent = cmd_send(api, js, 1);
    if (seg) { na = cp_collect(seg, k); cp_eval_corners(seg, t, a); }
    Lf("%ld KFREQ api=%s t=%lld sent=%d seg=%p | ключей %d -> %d | углы на t: "
       "UL %.4f,%.4f UR %.4f,%.4f LL %.4f,%.4f LR %.4f,%.4f -> UL %.4f,%.4f UR %.4f,%.4f LL %.4f,%.4f LR %.4f,%.4f\n",
       (long)time(NULL), api, t, sent, seg, nb, na,
       b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
       a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]);
}
static void drive_kfreq_main(void *_) { drive_kfreq(); }

// ve_kfmass.txt: «<segid> <KFType…> <x> <y> [N]» — переписать ВЕСЬ трек этого угла постоянным
// значением и выстрелить N команд (по умолчанию одну) на плейхеде.
// Отвечает на главный вопрос живого режима: команда синкает весь трек или только ключ на
// плейхеде. Ответ даёт СКРАБ по клипу — проверка на одном кадре отвечает на другой вопрос.
// N>1 — заодно цена выстрела: сколько стоит команда, если их придётся слать по одной на ключ.
static void drive_kfmass(void) {
    FILE *f = fopen(ud("ve_kfmass.txt"), "r");
    if (!f) return;
    char line[256], segid[64], kft[64]; double x = 0, y = 0, x1 = 0, y1 = 0; int reps = 1, ramp = 0;
    int nf = 0;
    if (fgets(line, sizeof line, f))
        nf = sscanf(line, "%63s %63s %lf %lf %d %lf %lf", segid, kft, &x, &y, &reps, &x1, &y1);
    int ok = (nf >= 4);
    ramp = (nf >= 7);   // 6-е и 7-е числа заданы -> значение едет по треку от (x,y) к (x1,y1)
    fclose(f);
    if (!ok) { Lf("%ld KFMASS: строку не разобрал\n", (long)time(NULL)); return; }
    if (reps < 1) reps = 1;
    void *seg = seg_by_id(segid);
    int idx = -1; for (int i = 0; i < 4; i++) if (!strcmp(kft, kCP[i])) idx = i;
    if (!seg || idx < 0) { Lf("%ld KFMASS: seg=%p idx=%d — нечего писать\n", (long)time(NULL), seg, idx); return; }
    seed_templates();
    CpKey k[CP_MAXK + 1]; int n = cp_collect(seg, k);
    if (n <= 0) { Lf("%ld KFMASS: ключей на сегменте нет\n", (long)time(NULL)); return; }
    double times[CP_MAXK], vals[CP_MAXK * 2];
    for (int i = 0; i < n; i++) { times[i] = (double)k[i].t;
        double f2 = (ramp && n > 1) ? (double)i / (double)(n - 1) : 0.0;
        vals[i * 2]     = ramp ? x + (x1 - x) * f2 : x;
        vals[i * 2 + 1] = ramp ? y + (y1 - y) * f2 : y; }
    struct timespec w0, w1; clock_gettime(CLOCK_MONOTONIC, &w0);
    doc_write_curves(seg, kCP[idx], times, vals, 0, n, 2);   // ВЕСЬ трек, а не точка на плейхеде
    clock_gettime(CLOCK_MONOTONIC, &w1);
    double wms = (w1.tv_sec - w0.tv_sec) * 1000.0 + (w1.tv_nsec - w0.tv_nsec) / 1e6;
    long long ph = cp_playhead();
    char js[512];
    snprintf(js, sizeof js,
             "{\"commit_immediately\":true,\"params\":{\"seg_id\":\"%s\",\"keyframeType\":\"%s\","
             "\"dataParam\":{\"play_head\":%lld,\"curveType\":\"Line\"}}}", segid, kCP[idx], ph);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    int sent = 0; for (int i = 0; i < reps; i++) sent += cmd_send("addCommonKeyframe", js, 1);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double ms = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_nsec - t0.tv_nsec) / 1e6;
    double a[8], b[8], c2[8];
    cp_eval_corners(seg, k[0].t, a); cp_eval_corners(seg, k[n / 2].t, b); cp_eval_corners(seg, k[n - 1].t, c2);
    Lf("%ld KFMASS %s ключей=%d ph=%lld запись=%.1f мс выстрелов=%d/%d за %.1f мс (%.2f мс/шт) | документ: "
       "t=%lld %.3f,%.3f | t=%lld %.3f,%.3f | t=%lld %.3f,%.3f\n",
       (long)time(NULL), kCP[idx], n, ph, wms, sent, reps, ms, ms / reps,
       k[0].t, a[idx * 2], a[idx * 2 + 1], k[n / 2].t, b[idx * 2], b[idx * 2 + 1],
       k[n - 1].t, c2[idx * 2], c2[idx * 2 + 1]);
}
static void drive_kfmass_main(void *_) { drive_kfmass(); }

// ⭐ ЖИВОЙ ЗАВОРОТ В СБОРНЫЙ КЛИП (гейт ve_wrap.txt). Строка: «<segid> <start_мкс> <dur_мкс> [имя]».
// Заворот происходит в ОТКРЫТОМ проекте, без цикла «закрыть → записать → открыть».
// ⚠️ КЛЮЧ — "type":1. С type=0 та же команда молча УДАЛЯЕТ сегмент вместе с материалом и
// отвечает без ошибки (замерено). Разница найдена сравнением своего запроса с пойманным родным:
// у родного на +148 лежит 1, у нас там был 0. Остальное (in_track_types, placeholder_video)
// оказалось необязательным.
static void drive_wrap(void) {
    FILE *f = fopen("/Users/naza.prod/Movies/CapCut/User Data/ve_wrap.txt", "r");
    if (!f) return;
    char line[256], segid[64], name[96] = {0}; long long st = 0, dur = 0;
    int nf = 0;
    if (fgets(line, sizeof line, f))
        nf = sscanf(line, "%63s %lld %lld %95[^\n]", segid, &st, &dur, name);
    fclose(f);
    if (nf < 3 || dur <= 0) { Lf("%ld WRAP: строку не разобрал (нужны id, start, duration)\n",
                                (long)time(NULL)); return; }
    if (!name[0]) snprintf(name, sizeof name, "NZLD компаунд");
    char js[512];
    snprintf(js, sizeof js,
             "{\"commit_immediately\":true,\"params\":{\"name\":\"%s\",\"seg_ids\":[\"%s\"],"
             "\"start_time\":%lld,\"duration\":%lld,\"type\":1,"
             "\"need_insert_target_track\":false,\"track_index\":0}}", name, segid, st, dur);
    int nid_before = g_nid;
    unsigned char resp[32] = {0};
    int sent = cmd_send_svc("CommonService", "combineSegment", js, 1, resp);
    // id созданного прокси: движок сразу запрашивает новый сегмент через DraftQuery::get_segment,
    // и он попадает в getseg-карту. Берём то, чего в карте не было ДО выстрела — это надёжнее,
    // чем гадать по ответу: RespStruct у combineSegment своей раскладки наружу не даёт.
    char proxy[48] = {0};
    for (int i = nid_before; i < g_nid; i++) {
        if (!strcmp(g_id_str[i], segid)) continue;          // сам завёрнутый не считается
        strncpy(proxy, g_id_str[i], sizeof proxy - 1);      // последний новый = прокси
    }
    Lf("%ld WRAP %s (%lld..%lld) sent=%d proxy=%s новых_в_карте=%d\n",
       (long)time(NULL), segid, st, st + dur, sent, proxy[0] ? proxy : "?", g_nid - nid_before);
    if (proxy[0]) {   // отдаём наружу файлом: конвейеру нужен именно id, а не строка лога
        FILE *o = fopen("/Users/naza.prod/Movies/CapCut/User Data/ve_wrap_out.txt", "w");
        if (o) { fprintf(o, "%s %s\n", segid, proxy); fclose(o); }
    }
    if (*(void **)resp) dump_req_fields(*(void **)resp);    // разово: что вообще лежит в ответе
}
static void drive_wrap_main(void *_) { drive_wrap(); }


// ⭐ РЕПЛЕЙ ПАРЫ ЗАВОРОТА (гейт ve_replay2.txt). Живой заворот = CombineSegment + AddVideo,
// и вопрос замера — достаточна ли ПАРА. Синтезировать AddVideo дорого (payload — массив
// богатых элементов), поэтому проверяем механику реплеем НАСТОЯЩИХ пойманных запросов.
// ⚠️ sid берём ТЕКУЩИЙ: захваченный протухает при переоткрытии проекта (ловили sid=-200).
static void replay_one(void *obj, void *ctrl, const char *tag) {
    if (!obj || !ctrl || !g_tramp_srv) { Lf("%ld REPLAY2 %s: нечего слать (obj=%p ctrl=%p)\n",
                                            (long)time(NULL), tag, obj, ctrl); return; }
    void *srv = g_srv ? g_srv : (p_server_instance ? ((void *(*)(void))p_server_instance)() : 0);
    if (!srv) { Lf("%ld REPLAY2 %s: сервера нет\n", (long)time(NULL), tag); return; }
    __atomic_fetch_add((long *)((char *)ctrl + 8), 1, __ATOMIC_RELAXED);   // invoke заберёт ссылку
    void *sp[2] = {obj, ctrl};
    unsigned char resp[80] = {0};
    call_sret3(resp, srv, sp, (void *)g_sid, g_tramp_srv);
    Lf("%ld REPLAY2 %s отправлена sid=%ld (обj=%p)\n", (long)time(NULL), tag, (long)g_sid, obj);
}
static void replay_pair_main(void *_) {
    char which[8] = {0};
    { FILE *f = fopen("/Users/naza.prod/Movies/CapCut/User Data/ve_replay2.txt", "r");
      if (f) { if (fscanf(f, "%7s", which) != 1) which[0] = 0; fclose(f); } }
    int do1 = (!which[0] || strchr(which, '1') != 0), do2 = (!which[0] || strchr(which, '2') != 0);
    Lf("%ld REPLAY2 старт: слот1=%p слот2=%p | шлём %s%s заморозка=%d\n", (long)time(NULL),
       g_cmd_obj, g_cmd2_obj, do1 ? "Combine " : "", do2 ? "AddVideo" : "", g_cp_freeze);
    if (do1) replay_one(g_cmd_obj, g_cmd_ctrl, "CombineSegment");
    if (do2) replay_one(g_cmd2_obj, g_cmd2_ctrl, "AddVideo");
}


// ⭐ ПОДЪЁМ СЕКВЕНЦИИ ПОСЛЕ ОТКРЫТИЯ ПРОЕКТА (гейт-выключатель ve_no_kick.txt).
// Зачем: g_seq берётся только из commit-хука, а движок сам коммитит не всегда — после
// авто-заворота (он закрывает и открывает проект) первый живой трек уходил в тишину, и это
// выглядело как «плагин сломан». Пинаем сами тем же безобидным no-op'ом, которым оживляли
// команду: шлём сегменту ТОТ ЖЕ масштаб, что у него стоит. По данным ничего не меняется,
// а хендлер зовёт Seq.commit — и g_seq оживает.
static void kick_seq_main(void *_) {
    if (has_vtable(g_seq)) { g_kick_tries = 0; return; }        // уже подняли — счётчик попыток обнуляем
    if (g_no_kick) return;
    // Пинаем ПО ОДНОМУ сегменту за тик и проверяем результат на следующем: не всякий подходит.
    // Замерено: сегмент с alpha=0 команду принимает (sent=1), а commit движок не зовёт — нечего
    // пересобирать. Поэтому кандидатов перебираем, а не выбираем «самый свежий».
    int &tried = *(int *)&g_kick_tries;
    if (tried >= 6) {
        Lf("%ld SEQKICK: 6 попыток впустую, сдаюсь до следующей загрузки\n", (long)time(NULL)); return; }
    int seen = 0;
    for (int i = g_nid - 1; i >= 0; i--) {
        void *seg = g_id_seg[i];
        if (!has_vtable(seg) || *(void **)seg != g_id_vt[i]) continue;
        if (g_segvideo_vt && *(void **)seg != g_segvideo_vt) continue;   // только видео: у них есть масштаб
        double sx = 0, sy = 0;
        if (!seg_scale(seg, &sx, &sy)) continue;
        if (seen++ < tried) continue;                                     // этого уже пробовали
        tried++;
        // Пометить клип грязным ТЕМ ЖЕ приёмом, что и cp_apply: re-store того же clip.
        // Без этого no-op команда уходит в никуда — замерено: sent=1, а commit движок не зовёт,
        // потому что пересобирать нечего. Данные не меняются: кладём ровно то, что лежало.
        if (p_seg_get_clip && p_seg_set_clip) {
            void *clipref = ((void *(*)(void *))p_seg_get_clip)(seg);
            if (clipref && *(void **)clipref) ((void (*)(void *, void *))p_seg_set_clip)(seg, clipref);
        }
        int sent = cmd_scale_send(g_id_str[i], sx, sy, 1);
        Lf("%ld SEQKICK попытка %d: %s (масштаб %.3f/%.3f) sent=%d g_seq=%p\n",
           (long)time(NULL), tried, g_id_str[i], sx, sy, sent, g_seq);
        return;                                                           // результат проверим на следующем тике
    }
    { static int q = 0; if (q++ < 3) Lf("%ld SEQKICK: подходящих сегментов в карте нет\n", (long)time(NULL)); }
}


// ==== ГАБАРИТ НАДПИСИ ИЗ ДВИЖКА (гейт ve_textbox.txt) ==========================
// Зачем: перспектива на тексте идёт через заворот в компаунд, а компаунд рождается размером
// с канвас ⇒ углы действуют по всему кадру. Лечится коэффициентом k = ширина канваса /
// ширина ЧЕРНИЛ надписи, и ошибка k переходит в углы линейно. Габарита нет нигде, кроме
// движка: в драфте fixed_width/height = −1, а офлайн-расчёт по шрифту опровергнут.
// Схему ответа наружу не вывести (json→строка не экспортирован), поэтому дампим сам
// RespStruct: имя класса из RTTI + первые 96 байт как hex/double/int64. Числа опознаём
// по тому, КАК они меняются на надписях разной длины и кегля.
static void dump_resp(const unsigned char *resp) {
    void *obj = *(void **)resp;
    Lf("%ld RESP sret: %p %p %p %p\n", (long)time(NULL), ((void **)resp)[0], ((void **)resp)[1],
       ((void **)resp)[2], ((void **)resp)[3]);
    if (!has_vtable(obj)) { Lf("  RESP: объекта нет (пустой ответ)\n"); return; }
    const char *tn = 0;
    void *vt = *(void **)obj, *tip = vt ? ((void **)vt)[-1] : 0;
    if (tip && sane_ptr(tip)) tn = ((const char **)tip)[1];
    Lf("  RESP тип=%s obj=%p\n", (tn && is_cstr((void *)tn)) ? tn : "?", obj);
    const unsigned char *p = (const unsigned char *)obj;
    for (int row = 0; row < 6; row++) {                    // 96 байт: с запасом, но в пределах объекта
        char h[64]; int n = 0;
        for (int k = 0; k < 16; k++) n += snprintf(h + n, sizeof h - n, "%02x ", p[row * 16 + k]);
        double d0, d1; long long i0, i1;
        memcpy(&d0, p + row * 16, 8); memcpy(&d1, p + row * 16 + 8, 8);
        memcpy(&i0, p + row * 16, 8); memcpy(&i1, p + row * 16 + 8, 8);
        Lf("   +%02x %s| double %.4f %.4f | int %lld %lld\n", row * 16, h, d0, d1, i0, i1);
    }
}
// Читает габарит надписи ПРЯМО ИЗ МОДЕЛИ: SegmentText::get_material (field-getter @+0x158,
// сверено дизассемблером) -> MaterialText, у него fixed_width @+0x5e8, fixed_height @+0x5f0,
// font_size @+0x2e0, text_size @+0x3c8, original_size рядом @+0x5d8 (вектор).
// Дампим и сырое окно вокруг: если чернила лежат в соседнем поле, это видно по числам.
static void dump_textmat(const char *segid) {
    void *seg = seg_by_id(segid);
    if (!has_vtable(seg)) { Lf("%ld TEXTMAT: сегмент %s не найден\n", (long)time(NULL), segid); return; }
    void *mr = (char *)seg + 0x158;                  // &shared_ptr<MaterialText>
    void *mat = *(void **)mr;
    if (!has_vtable(mat)) { Lf("%ld TEXTMAT: материала нет (seg=%p)\n", (long)time(NULL), seg); return; }
    const unsigned char *p = (const unsigned char *)mat;
    double fw, fh, fs; int ts;
    memcpy(&fw, p + 0x5e8, 8); memcpy(&fh, p + 0x5f0, 8);
    memcpy(&fs, p + 0x2e0, 8); memcpy(&ts, p + 0x3c8, 4);
    Lf("%ld TEXTMAT %s mat=%p | fixed %.3f x %.3f | font_size %.3f | text_size %d\n",
       (long)time(NULL), segid, mat, fw, fh, fs, ts);
    void **vb = (void **)(p + 0x5d8);                // original_size: вектор double
    if (sane_ptr(vb[0]) && sane_ptr(vb[1]) && (char *)vb[1] > (char *)vb[0]) {
        long n = ((double *)vb[1] - (double *)vb[0]);
        char s[128]; int k = 0;
        for (long i = 0; i < n && i < 6; i++) k += snprintf(s + k, sizeof s - k, "%.3f ", ((double *)vb[0])[i]);
        Lf("   original_size: %ld чисел: %s\n", n, s);
    } else Lf("   original_size: пусто\n");
    for (int row = 0; row < 4; row++) {              // окно вокруг габаритных полей
        int off = 0x5c0 + row * 16;
        double d0, d1; memcpy(&d0, p + off, 8); memcpy(&d1, p + off + 8, 8);
        Lf("   +%03x: double %.4f %.4f\n", off, d0, d1);
    }
}
// ⭐ ГАБАРИТ ЧЕРНИЛ: lvve::TextUtils::get_bounding_rect(shared_ptr<SegmentText> const&,
// LVVERectF&, long, long) — ПРЯМАЯ функция, без шины и без ReqStruct.
// ABI сверен по дизассемблеру 0x237761c: x0=&sp{ptr,ctrl}, x1=&out, x2/x3 = long.
// shared_ptr собираем своим leak-safe (счётчики высокие) — функция берёт сильную ссылку.
static void *p_text_bbox = 0, *p_text_bbox_dd = 0;   // вариант с двумя double — проверяем, не канвас ли это
static int text_bbox(const char *segid, double *out4) {
    if (!p_text_bbox) return 0;
    void *seg = seg_by_id(segid);
    if (!has_vtable(seg)) { Lf("%ld TEXTBOX: сегмент %s не найден\n", (long)time(NULL), segid); return 0; }
    void *sp[2]; make_leaky_sp(seg, sp);
    double r[8]; memset(r, 0, sizeof r);
    ((void (*)(void *, void *, long, long))p_text_bbox)(sp, r, 0, 0);
    float *f = (float *)r;
    Lf("%ld TEXTBOX %s: double %.3f %.3f %.3f %.3f | float %.3f %.3f %.3f %.3f\n",
       (long)time(NULL), segid, r[0], r[1], r[2], r[3], f[0], f[1], f[2], f[3]);
    memcpy(out4, r, 4 * sizeof(double));
    return 1;
}
// ve_textbox.txt: строка 1 = «<service> <api> <метка>», дальше СЫРОЙ json.
// Служебный режим: service=MODEL -> вместо команды читаем материал сегмента (api = id сегмента).
// Файла нет = выключено. Ответ пишем в ve_textbox_out.txt, чтобы питон мог забрать.
static void drive_textbox(void) {
    FILE *f = fopen("/Users/naza.prod/Movies/CapCut/User Data/ve_textbox.txt", "r");
    if (!f) return;
    char head[256], svc[64], api[96], tag[64], js[768]; int ok = 0;
    if (fgets(head, sizeof head, f) && sscanf(head, "%63s %95s %63s", svc, api, tag) >= 2) {
        size_t n = fread(js, 1, sizeof js - 1, f); js[n] = 0; ok = (n > 2);
    }
    fclose(f);
    if (!strcmp(svc, "MODEL")) { dump_textmat(api); return; }   // api тут = id сегмента
    if (!strcmp(svc, "BBOX2")) {   // тот же габарит, но через перегрузку с двумя double (тег = "w,h")
        void *seg = seg_by_id(api);
        double dw = 0, dh = 0; sscanf(tag, "%lf,%lf", &dw, &dh);
        if (has_vtable(seg) && p_text_bbox_dd) {
            void *sp2[2]; make_leaky_sp(seg, sp2);
            double r[8]; memset(r, 0, sizeof r);
            ((void (*)(void *, void *, double, double, long, long))p_text_bbox_dd)(sp2, r, dw, dh, 0, 0);
            float *f2 = (float *)r;
            Lf("%ld TEXTBOX2 %s канвас=(%.0f,%.0f): чернила %.1f x %.1f px (rect %.3f %.3f %.3f %.3f)\n",
               (long)time(NULL), api, dw, dh, f2[3], f2[1], f2[0], f2[1], f2[2], f2[3]);
            FILE *o = fopen("/Users/naza.prod/Movies/CapCut/User Data/ve_textbox_out.txt", "a");
            if (o) { fprintf(o, "%s %.1f %.1f\n", api, f2[3], f2[1]); fclose(o); }   // id ширина высота, px канваса
        } else Lf("%ld TEXTBOX2: сегмент/символ нет\n", (long)time(NULL));
        return;
    }
    if (!strcmp(svc, "BBOX")) {   // api = id сегмента; результат наружу файлом для питона
        double r[4] = {0, 0, 0, 0};
        if (text_bbox(api, r)) {
            FILE *o = fopen("/Users/naza.prod/Movies/CapCut/User Data/ve_textbox_out.txt", "a");
            if (o) { fprintf(o, "%s %.3f %.3f %.3f %.3f\n", api, r[0], r[1], r[2], r[3]); fclose(o); }
        }
        return;
    }
    if (!ok) { Lf("%ld TEXTBOX: не разобрал (нужны заголовок и json)\n", (long)time(NULL)); return; }
    unsigned char resp[32] = {0};
    // api с '?' в начале = ТОЛЬКО собрать, не стрелять: так безопасно проверяется, знает ли
    // фабрика пару (service, api) — неизвестную она отдаёт нулевым shared_ptr.
    int build_only = (api[0] == '?');
    int sent = cmd_send_svc(svc, build_only ? api + 1 : api, js, build_only ? 0 : 1, resp);
    Lf("%ld TEXTBOX %s/%s метка=%s sent=%d\n", (long)time(NULL), svc, api, tag, sent);
    if (sent) dump_resp(resp);
}
static void drive_textbox_main(void *_) { drive_textbox(); }

static void auto_apply(void *seg, void *kf, void *kfs) {
    if (!seg || !kf || !kfs) return;
    if (!(p_deep_copy && p_deepcopy_kfs && p_set_property_type && p_set_keyframe_list && p_insertOrUpdate)) return;
    void *coll = (char *)seg + 0xa8;
    char props[8][40]; int np = 0; readProps(props, &np);
    int made = 0;
    for (int i = 0; i < np; i++) {
        void *clone_sp[2] = {0, 0};
        call_sret2(clone_sp, kfs, 1, p_deepcopy_kfs);        // CommonKeyframes::deep_copy -> клон-трек
        if (!clone_sp[0]) continue;
        char ph[64]; unsigned char pobj[24];
        makeAltStr(props[i], pobj, ph, sizeof(ph));
        ((iou_t)p_set_property_type)(clone_sp[0], pobj, 0);
        void *empty[3] = {0, 0, 0};
        ((iou_t)p_set_keyframe_list)(clone_sp[0], empty, 0);
        writeCurveForProp(clone_sp, kf, props[i]);           // залить точки кривой (клоны kf)
        ((iou_t)p_insertOrUpdate)(coll, pobj, clone_sp);     // прикрепить трек (insertOrUpdate — идемпотентно)
        made++;
    }
    Lf("%ld AUTO created %d tracks on seg=%p\n", (long)time(NULL), made, seg);
    // Ре-рендер превью: пушим кривые в ЖИВУЮ рендер-секвенцию экранного NewVEWrapper (setKeyFrame -> dirty).
    // Прежние попытки (prerender-cache / vesdk refreshCF / PlayerClient replay) — подтверждённые тупики
    // (repaint из устаревшей модели); дизассемблер показал: только запись в рендер-секвенцию форсит ре-рендер.
    render_sync(props, np);
    seq_refresh();   // + взвод timelineChange/filterChange и commit: без флагов commit молча выходил,
                     // поэтому «превью после трекинга не обновляется без дёрганья масштаба»
}
// авто-триггер (главный поток): применяем на выделенный follower синтетическими шаблонами
static void auto_apply_main(void *_) {
    seed_templates();   // главный поток, движок прогрет -> UUID::generate/malloc безопасны
    void *seg = g_follower_seg ? g_follower_seg : g_last_seg;   // предпочитаем найденный по id
    if (seg && g_tmpl_kf && g_tmpl_kfs) auto_apply(seg, g_tmpl_kf, g_tmpl_kfs);
    else Lf("%ld AUTO skip: seg=%p tmpl=%p/%p\n", (long)time(NULL), seg, g_tmpl_kf, g_tmpl_kfs);
}
static void hook6(void *a, void *b, void *c) {
    void *kf = b ? *(void **)b : 0;    // CommonKeyframe*
    if (g_ready && (long)time(NULL) - g_ready >= 5 && g_ins_cnt < 2) {
        g_ins_cnt++;
        Lf("%ld INSERT kfs=%p kf=%p\n", (long)time(NULL), a ? *(void **)a : 0, kf);
    }
    ((fwd_t)g[6].tramp)(a, b, c);   // просто форвардим вставку.
    // ВАЖНО: НЕ применяем кривую здесь. Раньше был «ручной fallback», но он стрелял по g_last_seg
    // (последнему выделенному клипу) на ЛЮБУЮ первую вставку ключа → чужие keyframe + краш при выборе
    // другого клипа. Всё авто-применение теперь только в ovl_poll -> auto_apply_main (один раз на трек).
}
// VEEditorImpl::getPreviewControl(this) -> ловим ВОЗВРАТ = живой VEPreviewControlImpl* (для рефреша превью)
static void *hook8(void *a, void *b, void *c) {
    void *pc = ((void *(*)(void *))g[8].tramp)(a);
    if (pc) g_pc = pc;
    return pc;
}
// PlayerClient::refreshCurrentFrame[NoFlag](&sp{obj@0,ctrl@8}, &function, bool, long sid) — ABI из дизасма (без this).
// Ловим ReqStruct (retain -> переживёт UI) + трамплин той же функции, чтобы реплеить и форснуть рефреш превью.
static void capture_req(void *a, void *c, void *d, void *tramp) {
    if (g_req_obj || !a) return;
    void *obj = ((void **)a)[0], *ctrl = ((void **)a)[1];
    if (obj && ctrl) {
        __atomic_fetch_add((long *)((char *)ctrl + 8), 1, __ATOMIC_RELAXED);   // retain
        g_req_obj = obj; g_req_ctrl = ctrl; g_req_bool = (long)c; g_req_sid = (long)d; g_req_tramp = tramp;
        Lf("%ld REQ captured obj=%p ctrl=%p bool=%ld sid=%ld tramp=%p\n", (long)time(NULL), obj, ctrl, (long)c, (long)d, tramp);
    }
}
static void hook9(void *a, void *b, void *c, void *d) {   // refreshCurrentFrameNoFlag
    capture_req(a, c, d, g[9].tramp);
    ((void(*)(void *, void *, void *, void *))g[9].tramp)(a, b, c, d);
}
static void hook10(void *a, void *b, void *c, void *d) {  // refreshCurrentFrame (флаговый)
    if (g_trace) Lf("%ld TRACE refreshCurrentFrame this=%p x1=%p x2=%p x3=%p\n", (long)time(NULL), a, b, c, d);
    capture_req(a, c, d, g[10].tramp);
    ((void(*)(void *, void *, void *, void *))g[10].tramp)(a, b, c, d);
}
// NewVEWrapper методы — ловим this=x0 = экранный wrapper (первый непустой). render_sync валидирует +0x408.
static void hook11(void *a, void *b, void *c) {
    g_hk[11]++;
    if (!g_wrap_cap) { g_wrap_cap = a; Lf("%ld WRAP cap setCurEditSeq %p seq=%p\n", (long)time(NULL), a, *(void **)((char *)a + 0x408)); }
    // ⭐ СЕКВЕНЦИЯ ЗАГРУЖЕНА В РЕДАКТОР. g_seq обновляется ТОЛЬКО из commit-хука, а его до первого
    // действия пользователя может не быть — и всё это время g_seq показывает на ПРОШЛУЮ секвенцию.
    // has_vtable такой указатель пропускает (освобождённая память ещё похожа на объект), поэтому
    // тут его гасим сами: лучше «гейты молчат секунду», чем запись в мёртвую секвенцию.
    // Поднимет обратно kick_seq() из ovl_poll — безобидной no-op командой.
    g_seq = 0; g_teclip = 0; g_layerid[0] = 0; g_seq_kick = 1; g_seq_kick_at = (long)time(NULL); g_kick_tries = 0;
    ((fwd_t)g[11].tramp)(a, b, c);
}
static void hook12(void *a, void *b, void *c) {
    g_hk[12]++;
    if (!g_wrap_cap) { g_wrap_cap = a; Lf("%ld WRAP cap prepare %p seq=%p\n", (long)time(NULL), a, *(void **)((char *)a + 0x408)); }
    ((fwd_t)g[12].tramp)(a, b, c);
}
static void hook13(void *a, void *b, void *c) {
    g_hk[13]++;
    if (g_trace) Lf("%ld TRACE updateAvClip this=%p clip=%p bool=%p\n", (long)time(NULL), a, b, c);
    if (!g_wrap_cap) { g_wrap_cap = a; Lf("%ld WRAP cap updateAvClip %p seq=%p\n", (long)time(NULL), a, *(void **)((char *)a + 0x408)); }
    ((fwd_t)g[13].tramp)(a, b, c);
}
// PlayerWin::onFrameRendered(this=экранный wrapper, ll, int) — зовётся рендер-движком на КАЖДЫЙ кадр.
// Ловим НАСТОЯЩИЙ экранный wrapper (валидируем: [wrap+0x408] = живой heap-ptr рендер-seq). Кешируем один раз.
static void hook14(void *a, void *b, void *c) {
    g_hk[14]++;
    if (a && !g_screen_wrapper) {
        void *seq = *(void **)((char *)a + 0x408);
        if ((uintptr_t)seq > 0x100000000ULL) {   // валидная рендер-seq -> это тот самый экранный wrapper
            g_screen_wrapper = a;
            Lf("%ld SCREEN-WRAP %p seq@408=%p g_seq=%p (%s)\n", (long)time(NULL), a, seq, g_seq,
               seq == g_seq ? "==g_seq" : "!=g_seq — g_seq был не тот!");
        }
    }
    ((fwd_t)g[14].tramp)(a, b, c);
}

static const char* dumped_class = nullptr;
static void dump_meta(void *self) {
    void *vt = *(void**)self;
    void *(*metaObject_fn)(void*) = (void *(*)(void*))((void**)vt)[1];
    void *meta = metaObject_fn(self);
    if (!meta) return;

    typedef const char* (*ClassNameFn)(void*);
    ClassNameFn className = (ClassNameFn)dlsym(RTLD_DEFAULT, "_ZNK11QMetaObject9classNameEv");
    
    typedef int (*PropCountFn)(void*);
    PropCountFn propCount = (PropCountFn)dlsym(RTLD_DEFAULT, "_ZNK11QMetaObject13propertyCountEv");
    
    struct QMetaProp { void* a; void* b; void* c; void* d; }; // Ensure it's large enough (up to 32 bytes)
    typedef QMetaProp (*PropFn)(void*, int);
    PropFn prop = (PropFn)dlsym(RTLD_DEFAULT, "_ZNK11QMetaObject8propertyEi");
    
    typedef const char* (*PropNameFn)(QMetaProp*);
    PropNameFn propName = (PropNameFn)dlsym(RTLD_DEFAULT, "_ZNK13QMetaProperty4nameEv");
    
    if (!className || !propCount || !prop || !propName) return;
    
    const char* cname = className(meta);
    if (!cname || !strstr(cname, "ViewModel")) return;
    if (dumped_class == cname) return;
    dumped_class = cname;
    
    int count = propCount(meta);
    Lf("META class=%s count=%d\n", cname, count);
    for (int i=0; i<count; i++) {
        QMetaProp p = prop(meta, i);
        const char *name = propName(&p);
        Lf("  prop %d: %s\n", i, name);
    }
}


static void hook17(void *a, void *b, void *c) {
    if (b) {
        void* arr = *(void**)b;
        if (arr) {
            long size = *(long*)((char*)arr + 8);
            long offset = *(long*)((char*)arr + 24);
            char* str = (char*)arr + offset;
            char buf[64] = {0};
            int n = size < 63 ? size : 63;
            memcpy(buf, str, n);
            for (int i=0; i<n; i++) if (buf[i] < 32 || buf[i] > 126) buf[i] = '.';
            Lf("QML LOADED: %s...\n", buf);
            
            if (strstr(str, "DraftInspectorVideoScreenViewModel")) {
                Lf("INJECTING CORNER PIN INTO QML!\n");
                const char* injection = R"QML(
    Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 100
        color: "#1e1e26"
        border.color: "#3a3a44"
        radius: 8
        z: 9999
        Column {
            anchors.fill: parent
            anchors.margins: 10
            spacing: 5
            Text { text: "Искажение (Corner Pin)"; color: "white"; font.pixelSize: 13; font.bold: true }
            Row {
                spacing: 10
                Text { text: "Интенс."; color: "#999999"; font.pixelSize: 12 }
                Slider {
                    id: cpSlider
                    width: 150
                    from: -1.0; to: 1.0; value: 0.0
                    onValueChanged: {
                        root.viewModel.objectName = "PLUGIN_CMD:cp:" + value
                    }
                }
            }
        }
    }
)QML";
                char* last_brace = strrchr(str, '}');
                if (last_brace) {
                    long prefix_len = last_brace - str;
                    long inj_len = strlen(injection);
                    long total_len = prefix_len + inj_len + 2; 
                    char* new_str = (char*)malloc(total_len);
                    memcpy(new_str, str, prefix_len);
                    memcpy(new_str + prefix_len, injection, inj_len);
                    new_str[prefix_len + inj_len] = '}';
                    new_str[prefix_len + inj_len + 1] = '\0';
                    
                    void* (*QByteArray_ctor)(void*, const char*, long) = 
                        (void* (*)(void*, const char*, long))dlsym(RTLD_DEFAULT, "_ZN10QByteArrayC1EPKcx");
                    
                    void* my_ba[4] = {0};
                    QByteArray_ctor(my_ba, new_str, total_len - 1);
                    
                    ((fwd_t)g[17].tramp)(a, my_ba, c);
                    free(new_str);
                    return;
                }
            }
        }
    }
    ((fwd_t)g[17].tramp)(a, b, c);
}
static int hook18(void *self, int c, int id, void **a) {
    if (c == 1 && id == 0 && a && a[0]) {
        void* d = *(void**)a[0];
        if (d) {
            long size = *(long*)((char*)d + 8);
            long offset = *(long*)((char*)d + 24);
            unsigned short* chars = (unsigned short*)((char*)d + offset);
            char buf[256] = {0};
            int n = size < 255 ? size : 255;
            for (int i=0; i<n; i++) buf[i] = (char)chars[i];
            
            if (strncmp(buf, "PLUGIN_CMD:", 11) == 0) {
                if (strncmp(buf + 11, "corner_pin:", 11) == 0) {
                    float val = atof(buf + 22);
                    Lf("PLUGIN_CMD: corner_pin = %f\n", val);
                    
                    if (g_selected_segid[0] && p_cpinfo_ctor && p_seg_set_cornerpin) {
                        void *seg = seg_by_id(g_selected_segid);
                        if (seg) {
                            double dx = val * 0.2;
                            double coords[8] = {
                                dx, dx,
                                1.0 - dx, dx,
                                0.0, 1.0,
                                1.0, 1.0
                            };
                            void *info = malloc(0x200); memset(info, 0, 0x200);
                            ((void (*)(void *, bool))p_cpinfo_ctor)(info, true);
                            for (int i = 0; i < 8; i++) {
                                ((void (*)(void *, const double *))p_cpi_set[i])(info, &coords[i]);
                            }
                            void *spi[2]; make_leaky_sp(info, spi);
                            void *cp = malloc(0x200); memset(cp, 0, 0x200);
                            ((void (*)(void *, bool))p_cp_ctor)(cp, true);
                            ((void (*)(void *, void *))p_cp_set_info)(cp, spi);
                            bool en = true; ((void (*)(void *, const bool *))p_cp_set_enable)(cp, &en);
                            void *spc[2]; make_leaky_sp(cp, spc);
                            ((void (*)(void *, void *))p_seg_set_cornerpin)(seg, spc);
                            if (p_seg_get_clip && p_seg_set_clip) {
                                void *clipref = ((void *(*)(void *))p_seg_get_clip)(seg);
                                if (clipref && *(void **)clipref) ((void (*)(void *, void *))p_seg_set_clip)(seg, clipref);
                            }
                            seq_commit();
                        }
                    }
                }
                return -1;
            }
        }
    }
    int ret = ((int(*)(void*,int,int,void**))g[18].tramp)(self, c, id, a);
    return ret;
}
// ловим ГОТОВЫЙ shared_ptr<IVESequence> из предрендера: x1 = &shared_ptr{ptr, ctrl}
static long long hook_dur(void *a) {
    g_hk[19]++;
    if (!g_no_kick && has_vtable(a)) seq_adopt(a);   // ТОЛЬКО живой объект: мусор в g_seq класть нельзя
    return ((long long (*)(void *))g[19].tramp)(a);
}
static void hook19(void *a, void *b, void *c) {
    if (b && sane_ptr(((void **)b)[0])) {
        static int lg = 0;
        if (g_seq_sp[0] != ((void **)b)[0]) {
            g_seq_sp[0] = ((void **)b)[0]; g_seq_sp[1] = ((void **)b)[1];
            if (lg < 3) { lg++; Lf("%ld SEQSP пойман ptr=%p ctrl=%p\n", (long)time(NULL), g_seq_sp[0], g_seq_sp[1]); }
        }
    }
    ((fwd_t)g[20].tramp)(a, b, c);
}
// ЩИТ: KeyframeUtils::applyKeyframePreset роняет CapCut на пресете кривой.
// Разобрано по минидампу 1e419aa0 (EXC_BAD_ACCESS, адрес сбоя 0x2c, x0=0):
//   0x2ffaa30  ldr x22,[x1] ; 0x2ffaa34 cbz x22,выход   -> на null проверен ТОЛЬКО 2-й ключ
//   0x2ffaac0  ldr x0,[x19] ; 0x2ffaac8 bl set_curveType -> 1-й ключ берётся ВСЛЕПУЮ
//   set_curveType+0x4: `ldr w9,[x0,#0x2c]` при x0=0 -> падение.
// Пресет на ПЕРВОМ ключе дорожки: предыдущего ключа нет, указатель пустой — гарантированный вылет.
// Это баг движка, а не наш, но чинить его больше некому. Отбиваем вызов ровно так же, как сам
// движок отбивает пустой второй аргумент: просто возвращаемся. Утечки нет — shared_ptr'ы,
// переданные по значению, уничтожает ВЫЗЫВАЮЩИЙ (Itanium ABI), и это видно в их же ранней
// ветке 0x2ffae50: там только ldp/add sp/ret, ни одного release.
// Аргументы НЕ симметричны (разобрано по всем вызовам сеттеров внутри функции):
//   arg1 = ПРЕДЫДУЩИЙ ключ -> set_right_control (исходящая ручка)
//   arg2 = ТЕКУЩИЙ ключ    -> set_left_control  (входящая ручка)
// То есть пресет задаёт плавность ОТРЕЗКА между двумя ключами, а не «ключа». У первого ключа
// отрезка слева нет — потому предыдущий и пуст.
// ПОДСТАНОВКА ПРОБОВАЛАСЬ И НЕ РАБОТАЕТ — не повторять. Передача валидного ключа обоими
// аргументами (tramp(b,b,c)) не задаёт ручку, а ОБНУЛЯЕТ её: движок считает величину ручки от
// интервала между двумя ключами, а у одинаковых ключей интервал нулевой. Замер на живом драфте:
// right_control ключа 0 был (210000, 0.0), после подстановки стал (0.0, 0.0) — существующая
// плавность затёрлась. Поэтому просто выходим, как делает сам движок для пустого 2-го аргумента.
// Чтобы пресет на первом ключе реально заработал, нужен СЛЕДУЮЩИЙ ключ вторым аргументом
// (отрезок 1->2), а его в этом вызове нет — он есть только у вызывающего
// CommonClient::applyCommonKeyframesPreset. Это отдельная работа, не щит.
static void hook20(void *a, void *b, void *c) {
    void *ka = a ? *(void **)a : 0, *kb = b ? *(void **)b : 0;
    if (!ka || !kb) {
        static int lg = 0;
        if (lg < 20) { lg++;
            Lf("%ld ПРЕСЕТ отбит: пустой ключ (1-й=%p 2-й=%p) — движок бы упал здесь\n",
               (long)time(NULL), ka, kb); }
        return;
    }
    ((fwd_t)g[21].tramp)(a, b, c);   // idx сдвинулся: перед ним встал источник секвенции
}
static void *hookfn[22] = {(void *)hook0, (void *)hook1, (void *)hook2, (void *)hook3,
                           (void *)asm_thunk4, (void *)asm_thunk5, (void *)hook6, (void *)asm_thunk7,
                           (void *)hook8, (void *)hook9, (void *)hook10, (void *)hook11,
                           (void *)hook12, (void *)hook13, (void *)hook14, (void *)asm_thunk_skf,
                           (void *)asm_thunk_srv, (void *)asm_thunk_cv, (void *)asm_thunk_mkr,
                           (void *)hook_dur, (void *)hook19, (void *)hook20};

// абсолютный переход arm64 (16 байт): LDR X16,#8 ; BR X16 ; .quad dest
static void make_jump(uint32_t out[4], uint64_t dest) {
    out[0] = 0x58000050u; out[1] = 0xD61F0200u;
    out[2] = (uint32_t)(dest & 0xFFFFFFFFu); out[3] = (uint32_t)(dest >> 32);
}
// украденную инструкцию нельзя переносить, если она PC-относительная

static int is_pcrel(uint32_t I) {
    if ((I & 0x9F000000) == 0x10000000) return 1; // adr, adrp
    if ((I & 0x7C000000) == 0x14000000) return 1; // b, bl
    if ((I & 0x7E000000) == 0x34000000) return 1; // cbz, cbnz
    if ((I & 0x7E000000) == 0x36000000) return 1; // tbz, tbnz
    if ((I & 0xFF000000) == 0x54000000) return 1; // b.cond
    if ((I & 0x3B000000) == 0x18000000) return 1; // ldr (literal)
    return 0;
}
static int patch_code(void *addr, const void *buf, size_t n) {
    long ps = sysconf(_SC_PAGESIZE);
    void *page = (void *)((long)addr & ~(ps - 1));
    kern_return_t kr = vm_protect(mach_task_self(), (vm_address_t)page, ps * 2, 0, VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
    if (kr != KERN_SUCCESS) return -1;
    memcpy(addr, buf, n);
    sys_icache_invalidate(addr, n);
    vm_protect(mach_task_self(), (vm_address_t)page, ps * 2, 0, VM_PROT_READ | VM_PROT_EXECUTE);
    return 0;
}
static void *make_tramp(void *orig, const uint32_t stolen[4]) {
    for (int i=0; i<4; i++) if (is_pcrel(stolen[i])) return NULL; // can't relocate PC-rel
    uint32_t *m = (uint32_t *)mmap(NULL, sysconf(_SC_PAGESIZE), PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
    if (m == MAP_FAILED) return NULL;
    uint32_t buf[8];
    memcpy(buf, stolen, 16);
    make_jump(&buf[4], (uint64_t)orig + 16);
    pthread_jit_write_protect_np(0);
    memcpy(m, buf, 32);
    pthread_jit_write_protect_np(1);
    sys_icache_invalidate(m, 32);
    return m;
}

// ===================== НАБЛЮДАТЕЛЬ ПЛАГИН-ПЛАТФОРМЫ (lvopenplugin) =====================
// Отвечает на вопрос: почему инжектнутый PluginApplicationContainer НЕ грузит плагин?
// Гипотезы: (H1) гейт usepluginplatform/external_config рубит загрузку; (H2) контейнеру нужен
// правильный driver и загрузка вообще не триггерится. Различаем НАБЛЮДЕНИЕМ, а не подбором QML.
// Все цели — экспортированные символы libvideoeditor (lyra::wrapper::VePluginManager / PlayerClient),
// проверены nm: preloadOpenPlugins@0x3697784, loadPlugin@0x369797c, loadPlugins@0x36978cc,
// getOpenPluginMgr@0x195d1c, isExternalPluginEnabled@0x36976f0, instance@0x3697450, pluginCount@0x3697e0c.
// Хуки — log-and-forward через save-all трамплины (как asm_thunk4): сохраняем x0-x9+x30, логируем,
// восстанавливаем, прыгаем в оригинал. sret-безопасно (x8 цел). Наблюдение только, движок не трогаем.
extern "C" void *g_optramp[8] = {0};
// idx 4-7 = ТОЧКИ ВХОДА LYNX (libLynx.dylib, все экспортированы -> символьные, не адреса).
// Вопрос, который они закрывают: дёргает ли PluginApplicationContainer вообще Lynx-рантайм?
// Если при инжекте контейнера НИ ОДИН не сработал -> контейнер даже не доходит до создания view.
static const char *op_names[8] = {"preloadOpenPlugins", "loadPlugin", "loadPlugins", "getOpenPluginMgr",
                                  "LYNX.InitLynxViewBase", "LYNX.LoadTemplateFromURL", "LYNX.LoadTemplate", "LYNX.LoadTemplateBundle"};
extern "C" void op_log(long idx, void *x0, void *x1, void *x2) {
    Lf("%ld OP[%ld] %s this=%p x1=%p x2=%p\n", (long)time(NULL), idx,
       (idx >= 0 && idx < 8 && op_names[idx]) ? op_names[idx] : "?", x0, x1, x2);
    if (idx == 1 || idx == 2) { logString("plugin_id", (uint64_t)x1); logHex24((uint64_t)x1); }   // x1 = const std::string& (id/путь)
    if (idx == 5) { logString("template_url", (uint64_t)x1); }   // LoadTemplateFromURL: x1 = const string& (URL шаблона)
    if (idx == 6) { logString("tmpl_name", (uint64_t)x2); }      // LoadTemplate(vector<uint8>&, string&): x2 = имя
}
extern "C" void op_thunk0(void), op_thunk1(void), op_thunk2(void), op_thunk3(void);
extern "C" void op_thunk4(void), op_thunk5(void), op_thunk6(void), op_thunk7(void);
__asm__(
"   .text\n   .p2align 2\n"
"   .globl _op_thunk0\n_op_thunk0:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #0\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #0]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk1\n_op_thunk1:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #1\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #8]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk2\n_op_thunk2:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #2\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #16]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk3\n_op_thunk3:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #3\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #24]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk4\n_op_thunk4:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #4\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #32]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk5\n_op_thunk5:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #5\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #40]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk6\n_op_thunk6:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #6\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #48]\n   br   x16\n"
"   .p2align 2\n   .globl _op_thunk7\n_op_thunk7:\n"
"   sub  sp, sp, #96\n"
"   stp  x0, x1, [sp, #0]\n   stp  x2, x3, [sp, #16]\n   stp  x4, x5, [sp, #32]\n"
"   stp  x6, x7, [sp, #48]\n   stp  x8, x9, [sp, #64]\n   str  x30, [sp, #80]\n"
"   mov  x0, #7\n   ldr  x1, [sp, #0]\n   ldr  x2, [sp, #8]\n   ldr  x3, [sp, #16]\n"
"   bl   _op_log\n"
"   ldp  x0, x1, [sp, #0]\n   ldp  x2, x3, [sp, #16]\n   ldp  x4, x5, [sp, #32]\n"
"   ldp  x6, x7, [sp, #48]\n   ldp  x8, x9, [sp, #64]\n   ldr  x30, [sp, #80]\n"
"   add  sp, sp, #96\n"
"   adrp x16, _g_optramp@PAGE\n   add  x16, x16, _g_optramp@PAGEOFF\n   ldr  x16, [x16, #56]\n   br   x16\n"
);
// ФЛИП ГЕЙТА: isExternalPluginEnabled -> всегда 1 (будим спящую подсистему OpenPlugin).
// Простой C-хук (this-only, возврат bool в w0, без sret) — зовём оригинал через трамплин ради лога,
// но возвращаем 1. Если движок сверяется с этим флагом перед preload/load — флип его открывает.
static void *g_enb_tramp = 0;
typedef unsigned char (*enb_fn_t)(void *);
extern "C" unsigned char op_isenabled_hook(void *self) {
    unsigned char orig = g_enb_tramp ? ((enb_fn_t)g_enb_tramp)(self) : 0;
    static int lg = 0; if (lg < 3) { lg++; Lf("%ld  OP FLIP isExternalPluginEnabled orig=%d -> 1\n", (long)time(NULL), orig); }
    return 1;
}
// ФОРС КОНСТРУИРОВАНИЯ МЕНЕДЖЕРА (durable, символьный, не адрес).
// Дизасм pluginManager() (@0x369754c): он зовёт getLvOpenpluginExternalConfig(), читает enable-байт
// cfg->[+0x18]; если 0 — пропускает конструирование и возвращает NULL. Если !=0 — ЛЕНИВО строит менеджер
// (bl 0x35e842c + vtable[0x420]) и кеширует в this+0x40. => Достаточно flip'нуть enable-байт в ВОЗВРАТЕ
// конфига, и pluginManager сам построит менеджер штатным вендорным путём.
// getLvOpenpluginExternalConfig — sret (x8=буфер {cfg.ptr,ctrl}, x0=this). Пост-хук: зовём оригинал через
// трамплин (x0/x8 целы), затем cfg.ptr=[x8]; strb 1,[cfg.ptr+0x18].
extern "C" void *g_cfg_tramp = 0;
extern "C" void op_cfg_thunk(void);
__asm__(
"   .text\n   .p2align 2\n   .globl _op_cfg_thunk\n_op_cfg_thunk:\n"
"   sub  sp, sp, #0x20\n"
"   stp  x0, x8, [sp, #0]\n"          // сохранить this, sret-буфер
"   str  x30, [sp, #0x10]\n"
"   adrp x16, _g_cfg_tramp@PAGE\n   ldr  x16, [x16, _g_cfg_tramp@PAGEOFF]\n"
"   blr  x16\n"                       // оригинал: заполняет [x8]={cfg.ptr,ctrl}, x0/x8 целы на входе
"   ldr  x8, [sp, #0x8]\n"            // saved sret-буфер
"   ldr  x9, [x8]\n"                  // cfg.ptr
"   cbz  x9, Lcfg_done\n"
"   mov  w10, #1\n   strb w10, [x9, #0x18]\n"   // cfg->enable = 1
"Lcfg_done:\n"
"   ldr  x30, [sp, #0x10]\n"
"   add  sp, sp, #0x20\n"
"   ret\n"
);
static void install_op_observer() {
    static const char *syms[8] = {
        "_ZN4lyra7wrapper15VePluginManager18preloadOpenPluginsEv",
        "_ZN4lyra7wrapper15VePluginManager10loadPluginERKNSt3__112basic_stringIcNS2_11char_traitsIcEENS2_9allocatorIcEEEE",
        "_ZN4lyra7wrapper15VePluginManager11loadPluginsERKNSt3__112basic_stringIcNS2_11char_traitsIcEENS2_9allocatorIcEEEE",
        "_ZN12PlayerClient16getOpenPluginMgrENSt3__110shared_ptrIN4lyra25GetOpenPluginMgrReqStructEEEl",
        // --- LYNX (libLynx.dylib): дёргает ли контейнер рантайм вообще ---
        "_ZN4lynx12LynxViewBase16InitLynxViewBaseERNS_19LynxViewBaseBuilderE",
        "_ZN4lynx12LynxViewBase19LoadTemplateFromURLERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE",
        "_ZN4lynx12LynxViewBase12LoadTemplateERKNSt3__16vectorIhNS1_9allocatorIhEEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS3_IcEEEE",
        "_ZN4lynx12LynxViewBase18LoadTemplateBundleEPNS_18LynxTemplateBundleERKNSt3__112basic_stringIcNS3_11char_traitsIcEENS3_9allocatorIcEEEEPNS_16LynxTemplateDataE",
    };
    void *thunks[8] = {(void *)op_thunk0, (void *)op_thunk1, (void *)op_thunk2, (void *)op_thunk3,
                       (void *)op_thunk4, (void *)op_thunk5, (void *)op_thunk6, (void *)op_thunk7};
    for (int i = 0; i < 8; i++) {
        void *a = dlsym(RTLD_DEFAULT, syms[i]);
        if (!a) { Lf("%ld  OP no-sym %s\n", (long)time(NULL), op_names[i]); continue; }
        uint32_t stolen[4]; memcpy(stolen, a, 16);
        int bad = 0; for (int k = 0; k < 4; k++) if (is_pcrel(stolen[k])) bad = 1;
        if (bad) { Lf("%ld  OP pcrel-skip %s\n", (long)time(NULL), op_names[i]); continue; }
        void *tr = make_tramp(a, stolen);
        if (!tr) { Lf("%ld  OP tramp FAIL %s\n", (long)time(NULL), op_names[i]); continue; }
        g_optramp[i] = tr;
        uint32_t jb[4]; make_jump(jb, (uint64_t)thunks[i]);
        if (patch_code(a, jb, 16) != 0) { Lf("%ld  OP patch FAIL %s\n", (long)time(NULL), op_names[i]); continue; }
        Lf("%ld  OP HOOKED %s @ %p tramp=%p\n", (long)time(NULL), op_names[i], a, tr);
    }
    // ФОРС (опционально, по ve_op_flip.txt): пост-хук на getLvOpenpluginExternalConfig -> enable=1.
    // Это будит подсистему: pluginManager() увидит enabled и лениво сконструирует настоящий lvop-менеджер.
    // Символьный хук (durable), не адрес. По умолчанию ВЫКЛ, чтобы не менять поведение движка.
    if (access(ud("ve_op_flip.txt"), F_OK) == 0)
    {
        void *a = dlsym(RTLD_DEFAULT, "_ZN4lyra12AbtestConfig29getLvOpenpluginExternalConfigEv");
        if (!a) { Lf("%ld  OP FORCE no-sym getLvOpenpluginExternalConfig\n", (long)time(NULL)); }
        else {
            uint32_t stolen[4]; memcpy(stolen, a, 16);
            int bad = 0; for (int k = 0; k < 4; k++) if (is_pcrel(stolen[k])) bad = 1;
            if (bad) { Lf("%ld  OP FORCE pcrel-skip\n", (long)time(NULL)); }
            else {
                void *tr = make_tramp(a, stolen);
                if (!tr) { Lf("%ld  OP FORCE tramp FAIL\n", (long)time(NULL)); }
                else {
                    g_cfg_tramp = tr;
                    uint32_t jb[4]; make_jump(jb, (uint64_t)op_cfg_thunk);
                    if (patch_code(a, jb, 16) == 0) Lf("%ld  OP FORCE HOOKED getLvOpenpluginExternalConfig @ %p tramp=%p (enable->1)\n", (long)time(NULL), a, tr);
                    else Lf("%ld  OP FORCE patch FAIL\n", (long)time(NULL));
                }
            }
        }
    }
    (void)op_isenabled_hook; (void)g_enb_tramp;   // старый флип оставлен в коде, но не ставится
}
// Прямой опрос вердикта гейта — по файлу ve_op_probe.txt (rm+touch). Зовём геттеры singleton'а
// VePluginManager. isExternalPluginEnabled даёт H1/H2 напрямую; pluginCount — сколько уже загружено.
// Тело пробы гейта — исполняется на ГЛАВНОМ потоке (движковые вызовы небезопасны с фонового).
// Флип isExternalPluginEnabled уже стоит глобально. Форсим ТОЛЬКО preload (он безопасен — проверено:
// краш был в ручном loadPlugin из-за самодельной строки, preload не падал). Реальные строки id
// придут из самого preload -> их поймает наш loadPlugin-хук.
static void op_gate_main(void *_) {
    typedef void *(*inst_t)();
    typedef unsigned char (*enb_t)(void *);
    typedef unsigned long (*cnt_t)(void *);
    typedef void (*preload_t)(void *);
    inst_t inst = (inst_t)dlsym(RTLD_DEFAULT, "_ZN4lyra7wrapper15VePluginManager8instanceEv");
    enb_t enb  = (enb_t)dlsym(RTLD_DEFAULT, "_ZNK4lyra7wrapper15VePluginManager23isExternalPluginEnabledEv");
    cnt_t cnt  = (cnt_t)dlsym(RTLD_DEFAULT, "_ZNK4lyra7wrapper15VePluginManager11pluginCountEv");
    preload_t preload = (preload_t)dlsym(RTLD_DEFAULT, "_ZN4lyra7wrapper15VePluginManager18preloadOpenPluginsEv");
    if (!inst) { Lf("%ld  OPGATE no instance sym\n", (long)time(NULL)); return; }
    void *mgr = inst();
    Lf("%ld  OPGATE(before) mgr=%p enabled=%d count=%ld\n", (long)time(NULL),
       mgr, enb ? (int)enb(mgr) : -1, cnt ? (long)cnt(mgr) : -1);   // enabled должен быть 1 (флип активен)
    // РЕШАЮЩЕЕ: pluginManager() возвращает НАСТОЯЩИЙ lvop-менеджер (shared_ptr, sret). preloadOpenPlugins
    // просто делегирует в него (дизасм @0x36977a8). Если он NULL — подсистема не сконструирована на старте
    // (гейт usepluginplatform:0 при инициализации), и никакой флип downstream её не разбудит.
    // ВАЖНО: shared_ptr возвращается через sret (x8) т.к. нетривиально копируем. Чтобы компилятор
    // тоже использовал sret (а не x0/x1), делаем структуру нетривиальной (деструктор). Иначе ABI-промах -> краш.
    struct sp2 { void *p; void *c; ~sp2() {} };
    typedef struct sp2 (*pm_t)(void *);
    pm_t pm = (pm_t)dlsym(RTLD_DEFAULT, "_ZNK4lyra7wrapper15VePluginManager13pluginManagerEv");
    void *realmgr = 0;
    if (pm) { struct sp2 r = pm(mgr); realmgr = r.p;
              Lf("%ld  OPGATE pluginManager REAL=%p ctrl=%p (NULL=подсистема не поднята)\n", (long)time(NULL), r.p, r.c); }
    if (preload) { Lf("%ld  OPGATE -> preloadOpenPlugins()\n", (long)time(NULL)); preload(mgr); }
    // loadPlugin ТОЖЕ возвращает через sret (x8) — дизасм @0x36979c4: stp xzr,xzr,[x19=x8].
    // Звать как void -> x8 мусор -> запись в мусор -> SIGBUS (это и был "краш loadPlugin", не null).
    // Возврат нетривиальной структурой -> компилятор даёт валидный sret-буфер.
    // Строка id в АЛЬТ-раскладке libc++ (short SSO): данные с 0, размер в байте 23.
    typedef struct sp2 (*load_t)(void *, void *);
    load_t load = (load_t)dlsym(RTLD_DEFAULT, "_ZN4lyra7wrapper15VePluginManager10loadPluginERKNSt3__112basic_stringIcNS2_11char_traitsIcEENS2_9allocatorIcEEEE");
    if (load && realmgr) {
        // id читаем из ve_op_pid.txt (без пересборки под каждый). Fallback — bundled id.
        char pidbuf[64] = {0}; const char *pid = "com.lv.intlcrop";
        FILE *pf = fopen(ud("ve_op_pid.txt"), "r");
        if (pf) { if (fgets(pidbuf, sizeof(pidbuf), pf)) { char *nl = strchr(pidbuf, '\n'); if (nl) *nl = 0;
                     if (pidbuf[0]) pid = pidbuf; } fclose(pf); }
        unsigned long n = strlen(pid);
        if (n < 23) {   // SSO (короткая) в АЛЬТ-раскладке: данные с 0, размер в байте 23
            unsigned char sbuf[24]; memset(sbuf, 0, sizeof(sbuf));
            memcpy(sbuf, pid, n); sbuf[23] = (unsigned char)n;
            Lf("%ld  OPGATE -> loadPlugin(\"%s\") sso\n", (long)time(NULL), pid);
            struct sp2 lr = load(mgr, sbuf);
            Lf("%ld  OPGATE loadPlugin вернул=%p ctrl=%p\n", (long)time(NULL), lr.p, lr.c);
        } else {        // LONG: {char* data; size_t size; size_t cap|MSB} — АЛЬТ: флаг в MSB байта 23
            char *heap = (char *)malloc(n + 1); memcpy(heap, pid, n + 1);
            unsigned long lo[3]; lo[0] = (unsigned long)heap; lo[1] = n;
            lo[2] = (n + 16) & ~15UL; ((unsigned char *)lo)[23] |= 0x80;   // cap + long-флаг
            Lf("%ld  OPGATE -> loadPlugin(\"%s\") long\n", (long)time(NULL), pid);
            struct sp2 lr = load(mgr, lo);
            Lf("%ld  OPGATE loadPlugin вернул=%p ctrl=%p\n", (long)time(NULL), lr.p, lr.c);
        }
        usleep(500000);
    }
    Lf("%ld  OPGATE(after) count=%ld\n", (long)time(NULL), cnt ? (long)cnt(mgr) : -1);
}
static void op_probe_gate() {
    // движковые вызовы — только с главного потока (ovl_poll фоновый). g_ready-гвард в вызывающем.
    dispatch_async_f(dispatch_get_main_queue(), 0, op_gate_main);
}

#include <mach-o/getsect.h>

#include <mach-o/dyld.h>
#include <sys/mman.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

#include <mach-o/dyld.h>
#include <sys/mman.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

#include <mach-o/dyld.h>
#include <sys/mman.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

void patch_qml_in_memory() {
    uint32_t count = _dyld_image_count();
    const struct mach_header_64* target_mh = NULL;
    
    for (uint32_t i = 0; i < count; i++) {
        const char* name = _dyld_get_image_name(i);
        if (strstr(name, "libVECreator.dylib")) {
            target_mh = (const struct mach_header_64*)_dyld_get_image_header(i);
            break;
        }
    }
    
    if (!target_mh) return;
    char* base = (char*)target_mh;
    char* found = base + 0xE1EABE0; 
    
    const char* search_str = 
"    embeddedControlBarView: {\n"
"        if (viewModel.isTemplateCombinated && !viewModel.isCommerceTemplate && viewModel.isTemplateNeedPurchase) {\n"
"            return videoSettingView.controlBarContainer\n"
"        } else if (videoSettingView.beautyControlContainerVisible) {\n"
"            return videoSettingView.beautyControleContainer\n"
"        }\n"
"        return null\n"
"    }";

    size_t search_len = strlen(search_str); // 365
    
    if (memcmp(found, search_str, search_len) == 0) {
        const char* replace_str = 
"    embeddedControlBarView:null\n"
"    Rectangle{\n"
"        y:220;width:300;height:35;color:\"#2C2C2C\";z:99\n"
"        Row{\n"
"            spacing:10\n"
"            Text{text:\"Corner Pin\";color:\"white\"}\n"
"            Slider{width:150;from:-1;to:1;value:0;onValueChanged:root.viewModel.objectName=\"PLUGIN_CMD:cp:\"+value}\n"
"        }\n"
"    }";
        
        char replace_buf[400];
        memset(replace_buf, ' ', sizeof(replace_buf));
        size_t rlen = strlen(replace_str);
        if (rlen > search_len) {
            Lf("PATCH_QML: replace_str too long! %zu > %zu\n", rlen, search_len);
            return;
        }
        memcpy(replace_buf, replace_str, rlen);
        
        long ps = sysconf(_SC_PAGESIZE);
        void *page = (void *)((long)found & ~(ps - 1));
        
        vm_protect(mach_task_self(), (vm_address_t)page, ps * 2, 0, VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
        memcpy(found, replace_buf, search_len);
        vm_protect(mach_task_self(), (vm_address_t)page, ps * 2, 0, VM_PROT_READ | VM_PROT_EXECUTE);
        Lf("PATCH_QML: Successfully patched QML with minified Rectangle! Len: %zu\n", rlen);
    }
}
static void *worker(void *_) {
    for (int t = 0; t < 30; t++) { if (dlsym(RTLD_DEFAULT, g[0].sym)) break; sleep(1); }
    p_set_time_offset = (setto_t)dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframe15set_time_offsetERKx");
    p_set_values = (setv_t)dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframe10set_valuesERKNSt3__16vectorIdNS1_9allocatorIdEEEE");
    p_deep_copy = dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframe9deep_copyEb");
    p_get_segment = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery11get_segmentENSt3__110shared_ptrINS_5DraftEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEEb");
    p_get_segment_recursive = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery21get_segment_recursiveENSt3__110shared_ptrINS_5DraftEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    p_deepcopy_kfs = dlsym(RTLD_DEFAULT, "_ZN4lvve15CommonKeyframes9deep_copyEb");
    p_set_property_type = dlsym(RTLD_DEFAULT, "_ZN4lvve15CommonKeyframes17set_property_typeERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    p_set_keyframe_list = dlsym(RTLD_DEFAULT, "_ZN4lvve15CommonKeyframes17set_keyframe_listERKNSt3__16vectorINS1_10shared_ptrINS_14CommonKeyframeEEENS1_9allocatorIS5_EEEE");
    p_insertOrUpdate = dlsym(RTLD_DEFAULT, "_ZN4lvve13KeyframeUtils23insertOrUpdateKeyframesERKNSt3__110shared_ptrINS_9NodeArrayINS_15CommonKeyframesEEEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEENS2_IS4_EE");
    p_kf_ctor = (ctorb_t)dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframeC1Eb");
    p_kfs_ctor = (ctorb_t)dlsym(RTLD_DEFAULT, "_ZN4lvve15CommonKeyframesC1Eb");
    p_createCommonPoint = dlsym(RTLD_DEFAULT, "_ZN4lvve13KeyframeUtils17createCommonPointEdd");   // (x,y)->owned shared_ptr<CommonPoint>
    // Уборщик пустых треков ключей — ИХ ЖЕ. Нужен при отвязке дорожки от камеры (drive_clears):
    // сносит треки без ключей, чтобы не оставлять структуру, которой движок сам не делает.
    p_clear_empty_kf = dlsym(RTLD_DEFAULT, "_ZN4lvve13KeyframeUtils19clearEmptyKeyframesENSt3__110shared_ptrINS_7SegmentEEE");
    // Снос ОДНОГО трека ключей по имени свойства. Берёт ту же коллекцию (seg+0xa8), что и
    // insertOrUpdateKeyframes, поэтому вызывается тождественно, только без третьего аргумента.
    p_remove_kfs = dlsym(RTLD_DEFAULT, "_ZN4lvve13KeyframeUtils15removeKeyframesERKNSt3__110shared_ptrINS_9NodeArrayINS_15CommonKeyframesEEEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    if (!p_remove_kfs) Lf("dlsym removeKeyframes = NULL (пустые треки останутся в редакторе кривых)\n");
    if (!p_clear_empty_kf) Lf("dlsym clearEmptyKeyframes = NULL (отвязка оставит пустые треки)\n");
    p_set_left_control = dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframe16set_left_controlERKNSt3__110shared_ptrINS_11CommonPointEEE");
    p_set_right_control = dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframe17set_right_controlERKNSt3__110shared_ptrINS_11CommonPointEEE");
    // easing документных ключей (этап 2)
    p_set_curveType = dlsym(RTLD_DEFAULT, "_ZN4lvve14CommonKeyframe13set_curveTypeERK21LVVEKeyframeCurveType");
    p_get_left_control = dlsym(RTLD_DEFAULT, "_ZNK4lvve14CommonKeyframe16get_left_controlEv");
    p_get_right_control = dlsym(RTLD_DEFAULT, "_ZNK4lvve14CommonKeyframe17get_right_controlEv");
    p_cp_set_x = dlsym(RTLD_DEFAULT, "_ZN4lvve11CommonPoint5set_xERKd");
    p_cp_set_y = dlsym(RTLD_DEFAULT, "_ZN4lvve11CommonPoint5set_yERKd");
    p_seg_get_material = dlsym(RTLD_DEFAULT, "_ZNK4lvve12SegmentVideo12get_materialEv");   // персист по пути (компаунды)
    p_mat_get_path = dlsym(RTLD_DEFAULT, "_ZNK4lvve13MaterialVideo8get_pathEv");
    p_matpath_by_draft = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery32get_material_video_path_by_draftENSt3__110shared_ptrINS_5DraftEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    p_get_segs_recursive = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery22get_segments_recursiveENSt3__110shared_ptrINS_5DraftEEE13LVVETrackTypeb");   // фолбэк свежего заворачивания
    p_get_all_segments = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery16get_all_segmentsENSt3__110shared_ptrINS_5DraftEEE");   // сегменты драфта (для DoF: спарить видео+эффект в компаунде)
    p_get_all_subdraft = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery14GetAllSubDraftENSt3__110shared_ptrINS_5DraftEEEb");   // ВСЕ сабдрафты от root (обход всех компаундов независимо от захвата)
    p_getall_comb = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery24getAllCombinationSegmentENSt3__110shared_ptrINS_5DraftEEEb");   // внешние компаунд-сегменты
    p_getsubdraft = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery11getSubDraftENSt3__110shared_ptrINS_7SegmentEEE");   // компаунд-сегмент -> сабдрафт
    p_getseg_and_draft = dlsym(RTLD_DEFAULT, "_ZN4lvve10DraftQuery25get_segment_and_cur_draftENSt3__110shared_ptrINS_5DraftEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEERNS2_INS_7SegmentEEERS4_13LVVETrackType12LVVEMetaType");   // id -> сегмент + его субдрафт
    { void *vt = dlsym(RTLD_DEFAULT, "_ZTVN4lvve12SegmentVideoE"); if (vt) g_segvideo_vt = (char *)vt + 16; }   // vptr SegmentVideo напрямую (символ+16, подтверждено конструктором) — робастно, не зависит от rig-id
    { void *vt = dlsym(RTLD_DEFAULT, "_ZTVN4lvve18SegmentVideoEffectE"); if (vt) g_segeffect_vt = (char *)vt + 16; }   // vptr SegmentVideoEffect (символ+16)
    // CORNER-PIN (перспектива): конструкторы + сеттеры + attach на SegmentVideo. Всё в libvideoeditor, dlsym по одноподчёркнутым именам.
    p_cpinfo_ctor = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfoC1Eb");
    p_seg_get_cornerpin = dlsym(RTLD_DEFAULT, "_ZNK4lvve12SegmentVideo14get_corner_pinEv");
    p_seg_get_ckfs  = dlsym(RTLD_DEFAULT, "_ZNK4lvve7Segment20get_common_keyframesEv");
    p_seg_get_material = dlsym(RTLD_DEFAULT, "_ZNK4lvve12SegmentVideo12get_materialEv");   // 20, НЕ 19 — длину брать из nm, не считать руками
    p_kfs_get_ptype = dlsym(RTLD_DEFAULT, "_ZNK4lvve15CommonKeyframes17get_property_typeEv");
    p_kfs_get_list  = dlsym(RTLD_DEFAULT, "_ZNK4lvve15CommonKeyframes17get_keyframe_listEv");
    p_kf_get_toff   = dlsym(RTLD_DEFAULT, "_ZNK4lvve14CommonKeyframe15get_time_offsetEv");
    p_kf_get_values = dlsym(RTLD_DEFAULT, "_ZNK4lvve14CommonKeyframe10get_valuesEv");
    p_cp_get_info = dlsym(RTLD_DEFAULT, "_ZNK4lvve9CornerPin19get_corner_pin_infoEv");
    p_cp_ctor     = dlsym(RTLD_DEFAULT, "_ZN4lvve9CornerPinC1Eb");
    p_text_bbox_dd = dlsym(RTLD_DEFAULT, "_ZN4lvve9TextUtils17get_bounding_rectERKNSt3__110shared_ptrINS_11SegmentTextEEER9LVVERectFddll");
    p_text_bbox   = dlsym(RTLD_DEFAULT, "_ZN4lvve9TextUtils17get_bounding_rectERKNSt3__110shared_ptrINS_11SegmentTextEEER9LVVERectFll");
    p_cpi_set[0]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo16set_upper_left_xERKd");
    p_cpi_set[1]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo16set_upper_left_yERKd");
    p_cpi_set[2]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo17set_upper_right_xERKd");
    p_cpi_set[3]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo17set_upper_right_yERKd");
    p_cpi_set[4]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo16set_lower_left_xERKd");
    p_cpi_set[5]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo16set_lower_left_yERKd");
    p_cpi_set[6]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo17set_lower_right_xERKd");
    p_cpi_set[7]  = dlsym(RTLD_DEFAULT, "_ZN4lvve13CornerPinInfo17set_lower_right_yERKd");
    p_cp_set_info   = dlsym(RTLD_DEFAULT, "_ZN4lvve9CornerPin19set_corner_pin_infoERKNSt3__110shared_ptrINS_13CornerPinInfoEEE");
    p_cp_set_enable = dlsym(RTLD_DEFAULT, "_ZN4lvve9CornerPin21set_enable_corner_pinERKb");
    p_seg_set_cornerpin = dlsym(RTLD_DEFAULT, "_ZN4lvve12SegmentVideo14set_corner_pinERKNSt3__110shared_ptrINS_9CornerPinEEE");
    p_seg_get_clip = dlsym(RTLD_DEFAULT, "_ZNK4lvve12SegmentVideo8get_clipEv");
    // манглы сверены по nm libvideoeditor
    p_seg_get_clip_st = dlsym(RTLD_DEFAULT, "_ZNK4lvve14SegmentSticker8get_clipEv");
    if (!p_seg_get_clip_st) p_seg_get_clip_st = dlsym(RTLD_DEFAULT, "_ZNK4lvve19SegmentImageSticker8get_clipEv");
    p_seg_set_clip = dlsym(RTLD_DEFAULT, "_ZN4lvve12SegmentVideo8set_clipERKNSt3__110shared_ptrINS_4ClipEEE");
    p_str2curve = dlsym(RTLD_DEFAULT, "_ZN4lvve17DraftEnumTransfer37TransferStringToLVVEKeyframeCurveTypeERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    if (p_str2curve && g_ct_freecurve < 0) {   // enum FreeCurveInOut через строковый транслятор (не угадываем число)
        unsigned char sobj[24]; char sheap[32]; makeAltStr("FreeCurveInOut", sobj, sheap, sizeof sheap);
        g_ct_freecurve = ((int (*)(const void *))p_str2curve)(sobj);
        Lf("%ld CURVE enum FreeCurveInOut=%d\n", (long)time(NULL), g_ct_freecurve);
    }
    p_prcache_get = dlsym(RTLD_DEFAULT, "_ZN4lvve28SegmentPreRenderCacheManager11getInstanceEv");
    p_prcache_clear = dlsym(RTLD_DEFAULT, "_ZN4lvve28SegmentPreRenderCacheManager13clearAllCacheEv");
    p_refresh = dlsym(RTLD_DEFAULT, "_ZN4lvve9PlayerWin19RefreshCurrentFrameE10VESeekFlagi");
    p_getEditor = dlsym(RTLD_DEFAULT, "_ZN5vesdk14VESequenceImpl9getEditorEv");
    p_getPreviewControl = dlsym(RTLD_DEFAULT, "_ZN5vesdk12VEEditorImpl17getPreviewControlEv");
    p_refreshCF = dlsym(RTLD_DEFAULT, "_ZN5vesdk20VEPreviewControlImpl19refreshCurrentFrameEPFiPvixfNSt3__112basic_stringIcNS2_11char_traitsIcEENS2_9allocatorIcEEEEES1_10VESeekFlag");
    p_hotUpdate = dlsym(RTLD_DEFAULT, "_ZN5vesdk12VEEditorImpl9hotUpdateEv");
    p_server_instance = dlsym(RTLD_DEFAULT, "_ZN4lyra6Server8instanceEv");
    // ФАБРИКА КОМАНД (см. cmd_selfmade). jsonParse — единственный экспортированный вход в
    // nlohmann: своих ctor/parse/dump у basic_json в экспортах нет, а этот берёт строку и json&.
    p_sam_instance  = dlsym(RTLD_DEFAULT, "_ZN4lyra17ServiceApiMapping8instanceEv");
    p_makereq       = dlsym(RTLD_DEFAULT, g[18].sym);   // тот же мангл, что у хука idx18
    p_jsonparse     = dlsym(RTLD_DEFAULT, "_ZN4lyra16CaptionAnimUtils9jsonParseERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEERN8nlohmann10basic_jsonINS1_3mapENS1_6vectorES7_bxydS5_NSA_14adl_serializerENSD_IhNS5_IhEEEEEE");
    p_clip_get_scale = dlsym(RTLD_DEFAULT, "_ZNK4lvve4Clip9get_scaleEv");
    p_scale_get_x    = dlsym(RTLD_DEFAULT, "_ZNK4lvve5Scale5get_xEv");
    p_scale_get_y    = dlsym(RTLD_DEFAULT, "_ZNK4lvve5Scale5get_yEv");
    // БЫЛО "_ZN4lyra6Server10getSessionEv" (без аргументов) — ТАКОГО СИМВОЛА НЕТ. dlsym молча
    // отдавал NULL, get_wrapper() вылетал на guard'е → wrapper=0 → render_sync no-op → превью
    // не обновлялось. Реальный (сверено по nm libvideoeditor.dylib): getSession(long sid).
    p_get_session     = dlsym(RTLD_DEFAULT, "_ZN4lyra6Server10getSessionEl");
    p_get_vewrapper   = dlsym(RTLD_DEFAULT, "_ZN4lyra7Session12getVeWrapperEv");
    p_wrap_setkf   = dlsym(RTLD_DEFAULT, "_ZN4lvve12NewVEWrapper11setKeyFrameENSt3__110shared_ptrIN5vesdk3pub8KeyframeEEERKNS1_12basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    p_vkf_ctor     = dlsym(RTLD_DEFAULT, "_ZN5vesdk3pub8KeyframeC1Ev");
    p_vkf_set_json = dlsym(RTLD_DEFAULT, "_ZN5vesdk3pub8Keyframe8set_jsonERKNSt3__112basic_stringIcNS2_11char_traitsIcEENS2_9allocatorIcEEEE");
    p_vkf_set_id   = dlsym(RTLD_DEFAULT, "_ZN5vesdk3pub8Keyframe6set_idERKNSt3__112basic_stringIcNS2_11char_traitsIcEENS2_9allocatorIcEEEE");
    p_wrap_refresh = dlsym(RTLD_DEFAULT, "_ZN4lvve9PlayerWin19RefreshCurrentFrameE10VESeekFlagi");
    p_wrap_prepare = dlsym(RTLD_DEFAULT, "_ZN4lvve12NewVEWrapper7prepareEv");   // ре-рендер из рендер-seq по dirty
    p_wrap_commit  = dlsym(RTLD_DEFAULT, "_ZN4lvve12NewVEWrapper6commitEi");
    p_getNativeSequence = dlsym(RTLD_DEFAULT, "_ZN5vesdk14VESequenceImpl17getNativeSequenceEv");
    p_getClipWithClipID = dlsym(RTLD_DEFAULT, "_ZN10TESequence17getClipWithClipIDERKNSt3__112basic_stringIcNS0_11char_traitsIcEENS0_9allocatorIcEEEEPiS9_");
    // Оба имени сверены по nm libcccreator (getClipId — КОНСТАНТНЫЙ метод ⇒ _ZNK, а не _ZN).
    p_getAllClips = dlsym(RTLD_DEFAULT, "_ZN10TESequence11getAllClipsE12ETETrackType");
    // мангл сверен по nm libvideoeditor (НЕ придуман)
    p_rl_updateClipInfo = dlsym(RTLD_DEFAULT,
        "_ZN5vesdk25VESequenceRenderLayerImpl14updateClipInfoENSt3__110shared_ptrINS_3pub4ClipEEE");
    p_setCurEditSeq = dlsym(RTLD_DEFAULT,
        "_ZN4lvve12NewVEWrapper13setCurEditSeqENSt3__110shared_ptrIN5vesdk11IVESequenceEEEb");
    p_getClipId   = dlsym(RTLD_DEFAULT, "_ZNK10TEClipBase9getClipIdEv");
    p_getClipById       = dlsym(RTLD_DEFAULT, "_ZN10TESequence11getClipByIdERKNSt3__112basic_stringIcNS0_11char_traitsIcEENS0_9allocatorIcEEEE");
    p_getClipAndSeqById = dlsym(RTLD_DEFAULT, "_ZN10TESequence17getClipAndSeqByIdERKNSt3__112basic_stringIcNS0_11char_traitsIcEENS0_9allocatorIcEEEE");
    p_vseqGetClipById   = dlsym(RTLD_DEFAULT, "_ZN5vesdk14VESequenceImpl11getClipByIdERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE");
    p_getClipId_TE      = dlsym(RTLD_DEFAULT, "_ZNK10TEClipBase9getClipIdEv");
    p_originID          = dlsym(RTLD_DEFAULT, "_ZNK10TEClipBase8originIDEv");
    p_getFilePath       = dlsym(RTLD_DEFAULT, "_ZN10TEClipBase11getFilePathEv");
    // seed_templates() НЕ здесь: конструктор зовёт UUID::generate()->CFUUIDCreate->malloc, а на
    // фоновом потоке в раннем старте malloc уходит в бесконечную рекурсию (SIGBUS). Строим лениво
    // на главном потоке (auto_apply_main), когда движок прогрет — как это делал ручной путь.
    int armed = 0;
    
    // Предварительно вычисляем g_ve_base и g_creator_base
    void *srv_invoke = dlsym(RTLD_DEFAULT, g[16].sym);
    if (srv_invoke) g_ve_base = (long)srv_invoke - 0x4c8fb8;
    
    for (int t = 0; t < 50; t++) {
        for (uint32_t i = 0; i < _dyld_image_count(); i++) {
            const char *name = _dyld_get_image_name(i);
            if (name && strstr(name, "libVECreator.dylib")) {
                g_creator_base = _dyld_get_image_vmaddr_slide(i);
                break;
            }
        }
        if (g_creator_base != 0) break;
        usleep(100000); // 100ms
    }
    
    if (g_creator_base == 0) {
        Lf("%ld  FATAL: libVECreator.dylib not found!\n", (long)time(NULL));
    }
    
    for (int i = 0; i < NT; i++) {
        void *a = 0;
        if (g[i].sym) {
            a = dlsym(RTLD_DEFAULT, g[i].sym);
        } else if (g[i].addr) {
            if (i == 18) {
                a = (void *)(g_creator_base + (long)g[i].addr);
                Lf("%ld  qt_metacall base=0x%lx addr=0x%lx a=%p\n", (long)time(NULL), g_creator_base, (long)g[i].addr, a);
            }
            else a = (void *)((long)g_ve_base + (long)g[i].addr);
        }
        if (!a) { Lf("%ld  no-sym %s\n", (long)time(NULL), g[i].name); continue; }
        
        // Check if pointer is readable before memcpy
        int fd = open("/dev/random", O_WRONLY);
        if (fd >= 0) {
            if (write(fd, a, 16) < 0) {
                Lf("%ld  unreadable %s at %p\n", (long)time(NULL), g[i].name, a);
                close(fd);
                continue;
            }
            close(fd);
        }
        
        uint32_t stolen[4]; memcpy(stolen, a, 16);
        int bad = 0;
        for (int k = 0; k < 4; k++) if (is_pcrel(stolen[k])) bad = 1;
        if (bad) { Lf("%ld  pcrel-skip %s\n", (long)time(NULL), g[i].name); continue; }
        void *tr = make_tramp(a, stolen);
        if (!tr) { Lf("%ld  tramp FAIL %s\n", (long)time(NULL), g[i].name); continue; }
        g[i].addr = a; g[i].tramp = tr;
        if (i == 17) g_tramp_cv = tr;   // плейхед: asm_thunk_cv прыгает сюда
        if (i == 18) g_tramp_mkr = tr;  // makeReq
        if (i == 4) g_tramp4 = tr;   // asm-трамплины берут оригинал отсюда
        if (i == 5) g_tramp5 = tr;
        if (i == 7) g_tramp7 = tr;
        if (i == 15) g_tramp_skf = tr;
        if (i == 16) g_tramp_srv = tr;
        uint32_t jb[4]; make_jump(jb, (uint64_t)hookfn[i]);
        int kr = patch_code(a, jb, 16);
        if (kr != 0) { Lf("%ld  patch FAIL %s kr=%d\n", (long)time(NULL), g[i].name, kr); continue; }
        armed++;
        Lf("%ld  HOOKED %s @ %p tramp=%p\n", (long)time(NULL), g[i].name, a, tr);
    }
    if (g[16].addr) g_ve_base = (long)g[16].addr - 0x4c8fb8;   // база libvideoeditor (Server::invoke nm=0x4c8fb8) -> vtable-офсеты стабильны
    g_ready = (long)time(NULL);
    Lf("%ld READY hooked=%d ve_base=0x%lx (жду ~5с, потом ловлю ручные правки)\n", (long)time(NULL), armed, g_ve_base);
    // Наблюдатель плагин-платформы (lvopenplugin) — путь ЗАКРЫТ (платформа выключена вендором,
    // см. память capcut-openplugin-platform). Код оставлен для истории, но в проде НЕ ставим:
    // это 8 лишних патчей движка без пользы. Включается файлом ve_op_observe.txt.
    if (access(ud("ve_op_observe.txt"), F_OK) == 0)
        install_op_observer();
    // ponytail: patch_qml_in_memory() ОТКЛЮЧЁН. Он патчит ИСХОДНЫЙ ТЕКСТ .qml в ресурсах, но CapCut
    // везёт скомпилированные QV4-юниты (2007 магиков qv4cdata в arm64-срезе = ровно число .qml),
    // поэтому исходник никто не парсит — патч ложится в мёртвые байты. Плюс бил по
    // embeddedControlBarView, которого в панели «Трансформация» нет вообще. Замена: qml_probe_main.
    // patch_qml_in_memory();
    return NULL;
}

// ===== Cocoa-оверлей прогресс-бара (создаётся из инъекции через objc_msgSend) =====
typedef struct { double x, y, w, h; } Rect4;   // NSRect = 4 double (homogeneous -> d0..d3)
typedef struct { double x, y; } Point2;        // NSPoint = 2 double (d0,d1) — для [NSEvent mouseLocation]
static id ocls(const char *n) { return (id)objc_getClass(n); }
static SEL osel(const char *n) { return sel_registerName(n); }
static id oNew(const char *c) { return ((id(*)(id, SEL))objc_msgSend)(ocls(c), osel("alloc")); }
static id oStr(const char *s) { return ((id(*)(id, SEL, const char *))objc_msgSend)(ocls("NSString"), osel("stringWithUTF8String:"), s); }
// сглаженные углы для view/окна: слой + cornerRadius (+опц. цвет фона слоя, если a>=0)
static void set_round(id view, double rad, double r, double g, double b, double a) {
    ((void(*)(id, SEL, signed char))objc_msgSend)(view, osel("setWantsLayer:"), 1);
    id layer = ((id(*)(id, SEL))objc_msgSend)(view, osel("layer"));
    if (!layer) return;
    ((void(*)(id, SEL, double))objc_msgSend)(layer, osel("setCornerRadius:"), rad);
    ((void(*)(id, SEL, signed char))objc_msgSend)(layer, osel("setMasksToBounds:"), 1);
    if (a >= 0) {
        id col = ((id(*)(id, SEL, double, double, double, double))objc_msgSend)(
            ocls("NSColor"), osel("colorWithCalibratedRed:green:blue:alpha:"), r, g, b, a);
        id cg = ((id(*)(id, SEL))objc_msgSend)(col, osel("CGColor"));   // layer.backgroundColor = CGColorRef
        ((void(*)(id, SEL, id))objc_msgSend)(layer, osel("setBackgroundColor:"), cg);
    }
}
// плавное появление окна: alpha 0 -> 1 за 0.22с (NSAnimationContext + animator-прокси). ГЛАВНЫЙ ПОТОК.
static void fade_in(id win) {
    ((void(*)(id, SEL, double))objc_msgSend)(win, osel("setAlphaValue:"), 0.0);
    id ctx = (id)ocls("NSAnimationContext");
    ((void(*)(id, SEL))objc_msgSend)(ctx, osel("beginGrouping"));
    id cur = ((id(*)(id, SEL))objc_msgSend)(ctx, osel("currentContext"));
    ((void(*)(id, SEL, double))objc_msgSend)(cur, osel("setDuration:"), 0.22);
    id anim = ((id(*)(id, SEL))objc_msgSend)(win, osel("animator"));
    ((void(*)(id, SEL, double))objc_msgSend)(anim, osel("setAlphaValue:"), 1.0);
    ((void(*)(id, SEL))objc_msgSend)(ctx, osel("endGrouping"));
}
// пост клавиши в очередь событий CapCut (в процессе, без Accessibility): CGEvent -> NSEvent -> [NSApp postEvent]
extern "C" void *CGEventCreateKeyboardEvent(void *src, unsigned short key, bool down);
extern "C" void CFRelease(const void *);
static void post_one_key(unsigned short code) {   // ГЛАВНЫЙ ПОТОК
    id app = ((id(*)(id, SEL))objc_msgSend)(ocls("NSApplication"), osel("sharedApplication"));
    for (int down = 1; down >= 0; down--) {
        void *cg = CGEventCreateKeyboardEvent(0, code, down != 0);
        if (!cg) continue;
        id e = ((id(*)(id, SEL, void *))objc_msgSend)(ocls("NSEvent"), osel("eventWithCGEvent:"), cg);
        if (e) ((void(*)(id, SEL, id, signed char))objc_msgSend)(app, osel("postEvent:atStart:"), e, 0);
        CFRelease(cg);
    }
}
static void post_refresh_keys(void) {   // шаг кадра вперёд(→)+назад(←): seek -> ре-рендер, playhead net-zero
    post_one_key(0x7C);   // Right Arrow = next frame
    post_one_key(0x7B);   // Left Arrow  = prev frame
    Lf("%ld REFRESH-KEYS posted (right+left)\n", (long)time(NULL));
}
static id g_ovl_win = 0, g_ovl_bar = 0, g_ovl_lab = 0, g_ovl_x = 0;
static volatile double g_ovl_pct = 0; static char g_ovl_msg[128] = {0}; static volatile int g_ovl_visible = 0;
static volatile int g_ovl_hover = 0;       // курсор над плашкой -> показываем крестик
// mtime ve_progress.txt, на котором пользователь закрыл плашку руками. Закрытие ЧИСТО визуальное:
// операция идёт дальше, а плашка вернётся на следующую запись прогресса (mtime сменится) — значит
// зависшая плашка убирается насовсем, а новая операция снова видна, даже с тем же текстом.
static volatile long g_ovl_dis_mt = 0;

// Python на время reopen (клик по плитке проекта на «Главной») ставит этот флаг: наши плавающие окна
// (док/оверлей level 5, кнопка level 6) висят поверх и перехватили бы Quartz-клик → проект не открылся бы.
static int panels_hidden(void) {
    return access(ud("ve_hide_panels.txt"), F_OK) == 0;
}

static void ovl_create(void *_) {   // ГЛАВНЫЙ ПОТОК (AppKit)
    id win = oNew("NSWindow");
    Rect4 wf = {220, 240, 380, 96};
    win = ((id(*)(id, SEL, Rect4, unsigned long, unsigned long, signed char))objc_msgSend)(
        win, osel("initWithContentRect:styleMask:backing:defer:"), wf, 0UL, 2UL, 0);
    ((void(*)(id, SEL, long))objc_msgSend)(win, osel("setLevel:"), 5L);                 // поверх окон
    ((void(*)(id, SEL, signed char))objc_msgSend)(win, osel("setOpaque:"), 0);
    id color = ((id(*)(id, SEL, double, double, double, double))objc_msgSend)(
        ocls("NSColor"), osel("colorWithCalibratedRed:green:blue:alpha:"), 0.09, 0.09, 0.11, 0.97);
    ((void(*)(id, SEL, id))objc_msgSend)(win, osel("setBackgroundColor:"), color);
    id content = ((id(*)(id, SEL))objc_msgSend)(win, osel("contentView"));

    id bar = oNew("NSProgressIndicator");
    Rect4 bf = {20, 26, 340, 18};
    bar = ((id(*)(id, SEL, Rect4))objc_msgSend)(bar, osel("initWithFrame:"), bf);
    ((void(*)(id, SEL, signed char))objc_msgSend)(bar, osel("setIndeterminate:"), 0);
    ((void(*)(id, SEL, double))objc_msgSend)(bar, osel("setMinValue:"), 0.0);
    ((void(*)(id, SEL, double))objc_msgSend)(bar, osel("setMaxValue:"), 100.0);
    ((void(*)(id, SEL, double))objc_msgSend)(bar, osel("setDoubleValue:"), 0.0);
    ((void(*)(id, SEL, id))objc_msgSend)(content, osel("addSubview:"), bar);

    id lab = oNew("NSTextField");
    Rect4 lf = {20, 54, 340, 22};
    lab = ((id(*)(id, SEL, Rect4))objc_msgSend)(lab, osel("initWithFrame:"), lf);
    ((void(*)(id, SEL, signed char))objc_msgSend)(lab, osel("setBezeled:"), 0);
    ((void(*)(id, SEL, signed char))objc_msgSend)(lab, osel("setEditable:"), 0);
    ((void(*)(id, SEL, signed char))objc_msgSend)(lab, osel("setDrawsBackground:"), 0);
    ((void(*)(id, SEL, id))objc_msgSend)(lab, osel("setTextColor:"),
        ((id(*)(id, SEL))objc_msgSend)(ocls("NSColor"), osel("whiteColor")));
    ((void(*)(id, SEL, id))objc_msgSend)(lab, osel("setStringValue:"), oStr(""));
    ((void(*)(id, SEL, id))objc_msgSend)(content, osel("addSubview:"), lab);

    // Крестик «закрыть» в правом верхнем углу: показывается на наведении, гасит плашку визуально.
    // Своего ObjC-класса ради одной кнопки не заводим — наведение и клик ловит ovl_mouse_tick
    // опросом, окно и так опрашивается. NSTextField, а не NSButton, ровно поэтому: нужен только вид.
    id xb = oNew("NSTextField");
    Rect4 xf = {wf.w - 30, wf.h - 28, 22, 22};
    xb = ((id(*)(id, SEL, Rect4))objc_msgSend)(xb, osel("initWithFrame:"), xf);
    ((void(*)(id, SEL, signed char))objc_msgSend)(xb, osel("setBezeled:"), 0);
    ((void(*)(id, SEL, signed char))objc_msgSend)(xb, osel("setEditable:"), 0);
    ((void(*)(id, SEL, signed char))objc_msgSend)(xb, osel("setSelectable:"), 0);
    ((void(*)(id, SEL, signed char))objc_msgSend)(xb, osel("setDrawsBackground:"), 0);
    ((void(*)(id, SEL, id))objc_msgSend)(xb, osel("setTextColor:"),
        ((id(*)(id, SEL, double, double))objc_msgSend)(ocls("NSColor"),
            osel("colorWithCalibratedWhite:alpha:"), 0.78, 1.0));
    ((void(*)(id, SEL, id))objc_msgSend)(xb, osel("setStringValue:"), oStr("✕"));
    ((void(*)(id, SEL, signed char))objc_msgSend)(xb, osel("setHidden:"), 1);
    ((void(*)(id, SEL, id))objc_msgSend)(content, osel("addSubview:"), xb);

    // окно создано скрытым — покажет ovl_update, когда пойдёт прогресс
    g_ovl_win = win; g_ovl_bar = bar; g_ovl_lab = lab; g_ovl_x = xb;
    Lf("%ld OVERLAY created win=%p\n", (long)time(NULL), win);
}
static void ovl_update(void *_) {   // ГЛАВНЫЙ ПОТОК
    if (!g_ovl_win) return;
    if (panels_hidden()) { ((void(*)(id, SEL, id))objc_msgSend)(g_ovl_win, osel("orderOut:"), 0); return; }
    ((void(*)(id, SEL, double))objc_msgSend)(g_ovl_bar, osel("setDoubleValue:"), g_ovl_pct);
    ((void(*)(id, SEL, id))objc_msgSend)(g_ovl_lab, osel("setStringValue:"), oStr(g_ovl_msg));
    if (g_ovl_x)
        ((void(*)(id, SEL, signed char))objc_msgSend)(g_ovl_x, osel("setHidden:"), g_ovl_hover ? 0 : 1);
    ((void(*)(id, SEL, id))objc_msgSend)(g_ovl_win, osel(g_ovl_visible ? "orderFront:" : "orderOut:"), (id)0);
}
// Наведение и клик по крестику — ОПРОСОМ (NSEvent.mouseLocation + pressedMouseButtons), а не
// событиями: борелесс-окно уровня 5 своих обработчиков не имеет, а заводить ObjC-класс с
// target/action ради одной кнопки дороже, чем два вызова раз в 50 мс. Зовётся из ovl_poll.
static void ovl_mouse_tick(void) {
    if (!g_ovl_win || !g_ovl_visible) {
        if (g_ovl_hover) { g_ovl_hover = 0; dispatch_async_f(dispatch_get_main_queue(), 0, ovl_update); }
        return;
    }
    Point2 m = ((Point2(*)(id, SEL))objc_msgSend)(ocls("NSEvent"), osel("mouseLocation"));
    Rect4 f = ((Rect4(*)(id, SEL))objc_msgSend)(g_ovl_win, osel("frame"));
    int in = m.x >= f.x && m.x <= f.x + f.w && m.y >= f.y && m.y <= f.y + f.h;
    // зона крестика шире самого значка: попасть в 22 px мышью по плавающему окну неудобно
    int onx = in && m.x >= f.x + f.w - 40 && m.y >= f.y + f.h - 40;
    long btn = ((long(*)(id, SEL))objc_msgSend)(ocls("NSEvent"), osel("pressedMouseButtons"));
    if (onx && (btn & 1)) {
        struct stat st;
        g_ovl_dis_mt = (stat(ud("ve_progress.txt"), &st) == 0)
            ? (long)st.st_mtimespec.tv_sec * 1000000000L + st.st_mtimespec.tv_nsec : -1;
        g_ovl_visible = 0; g_ovl_hover = 0;
        Lf("%ld OVERLAY закрыт вручную (только вид, операция идёт дальше)\n", (long)time(NULL));
        dispatch_async_f(dispatch_get_main_queue(), 0, ovl_update);
        return;
    }
    if (in != g_ovl_hover) { g_ovl_hover = in; dispatch_async_f(dispatch_get_main_queue(), 0, ovl_update); }
}
// Cocoa-морда плагина (кнопка «Трекер» + док + WKWebView с ui.html) УДАЛЕНА: морда переехала в
// НАТИВНУЮ вкладку QML внутри левой верхней панели CapCut (см. qml_tabprobe_main / ve_tab.qml).
// main_window() оставлен — его использует corner-pin оверлей ниже.
// главное окно CapCut = самое большое видимое NSWindow (Qt оборачивает NSWindow)
static id main_window(void) {
    id app = ((id(*)(id, SEL))objc_msgSend)(ocls("NSApplication"), osel("sharedApplication"));
    id wins = ((id(*)(id, SEL))objc_msgSend)(app, osel("windows"));
    long n = ((long(*)(id, SEL))objc_msgSend)(wins, osel("count"));
    id best = 0; double bestArea = 0;
    for (long i = 0; i < n; i++) {
        id w = ((id(*)(id, SEL, long))objc_msgSend)(wins, osel("objectAtIndex:"), i);
        if (!((signed char(*)(id, SEL))objc_msgSend)(w, osel("isVisible"))) continue;
        Rect4 f = ((Rect4(*)(id, SEL))objc_msgSend)(w, osel("frame"));   // NSRect в d0-d3 (HFA)
        double area = f.w * f.h;
        if (area > bestArea) { bestArea = area; best = w; }
    }
    return best;
}
// ===== CORNER-PIN OVERLAY: 4 ручки поверх VEPreviewMetalView, Ctrl+драг -> NDC -> ve_cornerpin.txt =====
typedef struct { double x, y; } Pt2;
static id g_co_win = 0, g_co_view = 0, g_co_dot[4] = {0, 0, 0, 0};
static double g_co_ndc[4][2] = {{-1,-1},{1,-1},{-1,1},{1,1}};   // ИСТОЧНИК ИСТИНЫ: углы в NDC (UL,UR,LL,LR)
static double g_co_ph[4][2];              // они же в локальных координатах вью (=превью, y-up) — производные
static int g_co_inited = 0, g_co_drag = -1;
static char g_co_segid[48] = {0};
static double g_co_sx = 1.0, g_co_sy = 1.0, g_co_tx = 0.0, g_co_ty = 0.0;   // clip слоя: масштаб x/y + сдвиг
static double g_co_rot = 0.0, g_co_srcw = 0.0, g_co_srch = 0.0;             // поворот (градусы) + размеры исходника (aspect-fit бокса)
static Rect4 g_co_scr;                     // прямоугольник превью на экране
static id g_co_found; static double g_co_area;
// ВЫБОР ПОВЕРХНОСТИ. Тот же оверлей, но ручки живут в ДОЛЯХ КАНВАСА (0..1, y вниз), а не в коробке
// слоя: поверхность — это область кадра (экран телефона, вывеска), она ни к какому слою не привязана.
// Гейт — наличие ve_surfpick.txt; в этом режиме Ctrl не нужен и corner-pin никуда не пишется.
static id g_co_edge[4] = {0, 0, 0, 0};
static double g_surf[4][2] = {{0.35,0.30},{0.65,0.30},{0.35,0.70},{0.65,0.70}};   // UL,UR,DL,DR
static int g_surf_on = 0; static long g_surf_mtime = 0;
static void co_walk(id v) {
    if (!v) return;
    const char *cn = object_getClassName(v);
    if (cn && !strcmp(cn, "VEPreviewMetalView")) {
        Rect4 b = ((Rect4(*)(id, SEL))objc_msgSend)(v, osel("bounds"));
        if (b.w * b.h > g_co_area) { g_co_area = b.w * b.h; g_co_found = v; }
    }
    id subs = ((id(*)(id, SEL))objc_msgSend)(v, osel("subviews"));
    long n = subs ? ((long(*)(id, SEL))objc_msgSend)(subs, osel("count")) : 0;
    for (long i = 0; i < n; i++) co_walk(((id(*)(id, SEL, long))objc_msgSend)(subs, osel("objectAtIndex:"), i));
}
static int co_preview_frame(Rect4 *out) {
    id mw = main_window(); if (!mw) return 0;
    id cv = ((id(*)(id, SEL))objc_msgSend)(mw, osel("contentView"));
    g_co_found = 0; g_co_area = 0; co_walk(cv);
    if (!g_co_found) return 0;
    Rect4 b = ((Rect4(*)(id, SEL))objc_msgSend)(g_co_found, osel("bounds"));
    Rect4 wr = ((Rect4(*)(id, SEL, Rect4, id))objc_msgSend)(g_co_found, osel("convertRect:toView:"), b, (id)0);
    Rect4 sr = ((Rect4(*)(id, SEL, Rect4))objc_msgSend)(mw, osel("convertRectToScreen:"), wr);
    *out = sr; return 1;
}
#define CO_PI 3.14159265358979323846
// бокс слоя в локальных координатах превью: исходник вписан в канвас (aspect-fit) при scale=1, потом × scale; центр = сдвиг
static void co_box(double *lw, double *lh, double *cx, double *cy) {
    double pw = g_co_scr.w, ph = g_co_scr.h, bw = pw, bh = ph;
    if (g_co_srcw > 0 && g_co_srch > 0 && ph > 0) {
        double sa = g_co_srcw / g_co_srch, ca = pw / ph;
        if (sa > ca) { bw = pw; bh = pw / sa; } else { bh = ph; bw = ph * sa; }   // contain
    }
    *lw = bw * g_co_sx; *lh = bh * g_co_sy;
    *cx = pw / 2.0 + g_co_tx * pw / 2.0; *cy = ph / 2.0 + g_co_ty * ph / 2.0;     // transform y вверх = Cocoa вверх
}
static void co_ndc_local(double nx, double ny, double *lx, double *ly) {   // NDC(y-down) -> локальные (y-up), с поворотом
    double lw, lh, cx, cy; co_box(&lw, &lh, &cx, &cy);
    double ox = nx * lw / 2.0, oy = -ny * lh / 2.0;                        // смещение от центра
    double r = -g_co_rot * CO_PI / 180.0;                                  // поворот CapCut по часовой -> матем. отрицательный
    double cs = cos(r), sn = sin(r);
    *lx = cx + ox * cs - oy * sn; *ly = cy + ox * sn + oy * cs;
}
static void co_local_ndc(double lx, double ly, double *nx, double *ny) {   // обратно: снимаем поворот, потом в NDC
    double lw, lh, cx, cy; co_box(&lw, &lh, &cx, &cy);
    double dx = lx - cx, dy = ly - cy;
    double r = -g_co_rot * CO_PI / 180.0;
    double cs = cos(r), sn = sin(r);
    double ox = dx * cs + dy * sn, oy = -dx * sn + dy * cs;                // инверсия поворота
    *nx = (lw > 0.001) ? ox / (lw / 2.0) : 0.0; *ny = (lh > 0.001) ? -oy / (lh / 2.0) : 0.0;
}
// живой clip слоя -> g_co_*: Clip*@seg+0x190; Scale*@clip+0x30, Transform*@clip+0x48 (x@+0x30,y@+0x38); rotation@clip+0x40;
// исходник: MaterialVideo*@seg+0x1b8, width@+0x118, height@+0x11c (int32)
static int co_read_clip(const char *segid) {
    void *seg = seg_by_id(segid);
    if (!seg || !has_vtable(seg)) return 0;
    void *clip = *(void **)((char *)seg + 0x190);
    if (!clip || !has_vtable(clip)) return 0;
    void *scale = *(void **)((char *)clip + 0x30), *tr = *(void **)((char *)clip + 0x48);
    if (!scale || !tr || (unsigned long)scale < 0x100000000UL || (unsigned long)tr < 0x100000000UL) return 0;
    double a = *(double *)((char *)scale + 0x30), b = *(double *)((char *)scale + 0x38);
    if (!(a > 0.0001 && a < 100.0 && b > 0.0001 && b < 100.0)) return 0;   // sanity -> иначе fallback
    g_co_sx = a; g_co_sy = b;
    g_co_tx = *(double *)((char *)tr + 0x30); g_co_ty = *(double *)((char *)tr + 0x38);
    g_co_rot = *(double *)((char *)clip + 0x40);
    g_co_srcw = 0; g_co_srch = 0;
    // Материал берём ЧЕРЕЗ ЭКСПОРТНЫЙ ГЕТТЕР, а не по числу: офсет 0x1b8 из старой заметки — НЕВЕРЕН,
    // там лежит shared_ptr<NodeWrapperSegmentVideoRenderIndex> (RTTI поймал), отсюда были w=h=0,
    // вырожденная коробка и дикие NDC. Реальный офсет 0x1d0, но геттер надёжнее любого числа.
    void *matref = p_seg_get_material ? ((void *(*)(void *))p_seg_get_material)(seg)
                                      : (void *)((char *)seg + 0x1d0);      // &shared_ptr<MaterialVideo>
    void *mat = (matref && sane_ptr(matref)) ? *(void **)matref : 0;
    if (mat && has_vtable(mat)) { int w = *(int *)((char *)mat + 0x118), h = *(int *)((char *)mat + 0x11c);
        if (w > 0 && h > 0 && w < 100000 && h < 100000) { g_co_srcw = w; g_co_srch = h; }
        else { static int n = 0; if (n < 3) { n++;
            // Класс материала из RTTI: typeinfo=*(vt-8), name=*(typeinfo+8). У фото это может быть
            // НЕ MaterialVideo -> тогда +0x118 это чужое поле, отсюда и нули.
            const char *cn = "?"; void *vt = *(void **)mat;
            if (sane_ptr(vt)) { void *ti = ((void **)vt)[-1]; if (sane_ptr(ti)) cn = ((const char **)ti)[1]; }
            Lf("MATDIM w=%d h=%d mat=%p class=%s\n", w, h, mat, cn ? cn : "?"); } } }
    else { static int n = 0; if (n < 3) { n++; Lf("MATDIM материал невалиден mat=%p\n", mat); } }
    return 1;
}
// доли канваса (y вниз) <-> локальные координаты превью (y вверх). Превью показывает канвас целиком
// и в его же соотношении сторон, поэтому пересчёт — чистое умножение, без вписывания.
static void co_surf_local(double fx, double fy, double *lx, double *ly) {
    *lx = fx * g_co_scr.w; *ly = (1.0 - fy) * g_co_scr.h;
}
static void co_local_surf(double lx, double ly, double *fx, double *fy) {
    *fx = (g_co_scr.w > 1.0) ? lx / g_co_scr.w : 0.0;
    *fy = (g_co_scr.h > 1.0) ? 1.0 - ly / g_co_scr.h : 0.0;
    if (*fx < 0) *fx = 0; if (*fx > 1) *fx = 1;      // за кадр поверхность уводить незачем
    if (*fy < 0) *fy = 0; if (*fy > 1) *fy = 1;
}
// Рёбра квада — тонкие повёрнутые вью. Без них четыре точки не читаются как четырёхугольник и
// попасть ими по углам экрана невозможно.
static void co_place_edges(void) {
    const int e[4][2] = {{0, 1}, {1, 3}, {3, 2}, {2, 0}};   // UL-UR-DR-DL-UL
    for (int i = 0; i < 4; i++) {
        if (!g_co_edge[i]) continue;
        double x0 = g_co_ph[e[i][0]][0], y0 = g_co_ph[e[i][0]][1];
        double x1 = g_co_ph[e[i][1]][0], y1 = g_co_ph[e[i][1]][1];
        double dx = x1 - x0, dy = y1 - y0, len = sqrt(dx * dx + dy * dy);
        if (len < 1.0) len = 1.0;
        ((void (*)(id, SEL, double))objc_msgSend)(g_co_edge[i], osel("setFrameRotation:"), 0.0);
        Rect4 fr = { (x0 + x1) / 2.0 - len / 2.0, (y0 + y1) / 2.0 - 1.0, len, 2.0 };
        ((void (*)(id, SEL, Rect4))objc_msgSend)(g_co_edge[i], osel("setFrame:"), fr);
        ((void (*)(id, SEL, double))objc_msgSend)(g_co_edge[i], osel("setFrameCenterRotation:"),
                                                  atan2(dy, dx) * 180.0 / CO_PI);
    }
}
static void co_place_dots(void) {
    for (int i = 0; i < 4; i++) {
        if (g_surf_on) co_surf_local(g_surf[i][0], g_surf[i][1], &g_co_ph[i][0], &g_co_ph[i][1]);
        else           co_ndc_local(g_co_ndc[i][0], g_co_ndc[i][1], &g_co_ph[i][0], &g_co_ph[i][1]);
        if (g_co_dot[i]) { Rect4 df = { g_co_ph[i][0] - 9, g_co_ph[i][1] - 9, 18, 18 };
            ((void(*)(id, SEL, Rect4))objc_msgSend)(g_co_dot[i], osel("setFrame:"), df); }
    }
    for (int i = 0; i < 4; i++)
        if (g_co_edge[i]) ((void (*)(id, SEL, signed char))objc_msgSend)(
            g_co_edge[i], osel("setHidden:"), (signed char)(g_surf_on ? 0 : 1));
    if (g_surf_on) co_place_edges();
}
static void surf_write_file(void) {
    FILE *f = fopen(ud("ve_surfquad.txt"), "w");
    if (!f) return;
    fprintf(f, "%.5f %.5f %.5f %.5f %.5f %.5f %.5f %.5f\n",
            g_surf[0][0], g_surf[0][1], g_surf[1][0], g_surf[1][1],
            g_surf[2][0], g_surf[2][1], g_surf[3][0], g_surf[3][1]);
    fclose(f);
}
// Гейт режима. Перечитываем по mtime: панель может включить выбор заново с другим стартовым квадом.
static int surf_gate(void) {
    struct stat st;
    if (stat(ud("ve_surfpick.txt"), &st) != 0) {
        g_surf_on = 0; g_surf_mtime = 0; return 0;
    }
    if ((long)st.st_mtime != g_surf_mtime) {
        g_surf_mtime = (long)st.st_mtime;
        FILE *f = fopen(ud("ve_surfpick.txt"), "r");
        if (f) { double v[8];
            if (fscanf(f, "%lf %lf %lf %lf %lf %lf %lf %lf",
                       &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]) == 8)
                for (int i = 0; i < 4; i++) { g_surf[i][0] = v[i * 2]; g_surf[i][1] = v[i * 2 + 1]; }
            fclose(f); }
    }
    g_surf_on = 1;
    return 1;
}
static void co_write_file(void) {
    FILE *f = fopen(ud("ve_cornerpin.txt"), "w");
    if (!f) return;
    fprintf(f, "%s %.4f %.4f %.4f %.4f %.4f %.4f %.4f %.4f\n", g_co_segid,
        g_co_ndc[0][0], g_co_ndc[0][1], g_co_ndc[1][0], g_co_ndc[1][1],
        g_co_ndc[2][0], g_co_ndc[2][1], g_co_ndc[3][0], g_co_ndc[3][1]);
    fclose(f);
}
static id co_hitTest_imp(id self, SEL _cmd, Pt2 p) {   // p в координатах окна (=локальные вью); self только на ручках, иначе pass-through
    (void)self; (void)_cmd;
    for (int i = 0; i < 4; i++) { double dx = p.x - g_co_ph[i][0], dy = p.y - g_co_ph[i][1]; if (dx*dx + dy*dy < 484) return g_co_view; }
    return (id)0;
}
static void co_mouseDown_imp(id self, SEL _cmd, id ev) {
    (void)self; (void)_cmd;
    unsigned long mods = ((unsigned long(*)(id, SEL))objc_msgSend)(ev, osel("modifierFlags"));
    Pt2 wp = ((Pt2(*)(id, SEL))objc_msgSend)(ev, osel("locationInWindow"));
    int best = -1; double bd = 1e18;
    for (int i = 0; i < 4; i++) { double dx = wp.x - g_co_ph[i][0], dy = wp.y - g_co_ph[i][1], d = dx*dx + dy*dy; if (d < bd) { bd = d; best = i; } }
    // В режиме выбора поверхности Ctrl не нужен: это самостоятельный инструмент, а не наложение
    // поверх обычной работы с превью, и ручки видны только пока панель его включила.
    g_co_drag = (best >= 0 && bd < 484 && (g_surf_on || (mods & 0x40000))) ? best : -1;   // 0x40000 = Control
}
static void co_mouseDragged_imp(id self, SEL _cmd, id ev) {
    (void)self; (void)_cmd;
    if (g_co_drag < 0) return;
    Pt2 wp = ((Pt2(*)(id, SEL))objc_msgSend)(ev, osel("locationInWindow"));
    if (g_surf_on) {   // выбор поверхности: пишем ТОЛЬКО квад, corner-pin не трогаем вовсе
        co_local_surf(wp.x, wp.y, &g_surf[g_co_drag][0], &g_surf[g_co_drag][1]);
        co_place_dots();
        surf_write_file();
        return;
    }
    co_local_ndc(wp.x, wp.y, &g_co_ndc[g_co_drag][0], &g_co_ndc[g_co_drag][1]);
    // КЛАПАН: если коробка слоя вырождается (напр. размеры исходника не прочитались), co_local_ndc
    // выдаёт дикие NDC (ловил 61 при норме [-1,1]), они уходят в set_corner_pin, а ромб честно
    // запекает их в ключи — проект насыпается мусором. ±8 хватает на любой осмысленный перекос.
    for (int k = 0; k < 2; k++) {
        double *v = &g_co_ndc[g_co_drag][k];
        if (!(*v > -8.0 && *v < 8.0)) *v = (*v > 0 || !(*v == *v)) ? 8.0 : -8.0;   // + NaN-guard
    }
    co_place_dots();
    co_write_file();
}
static void co_mouseUp_imp(id self, SEL _cmd, id ev) {
    (void)self; (void)_cmd; (void)ev;
    if (g_co_drag >= 0) { if (g_surf_on) surf_write_file(); else co_write_file(); g_co_drag = -1; }
}
static void co_create_or_update(void) {
    if (!co_preview_frame(&g_co_scr)) { if (g_co_win) ((void(*)(id, SEL, id))objc_msgSend)(g_co_win, osel("orderOut:"), 0); return; }
    if (!g_co_win) {
        Class vc = objc_allocateClassPair((Class)ocls("NSView"), "VECornerView", 0);
        if (vc) {
            class_addMethod(vc, osel("hitTest:"), (IMP)co_hitTest_imp, "@@:{CGPoint=dd}");
            class_addMethod(vc, osel("mouseDown:"), (IMP)co_mouseDown_imp, "v@:@");
            class_addMethod(vc, osel("mouseDragged:"), (IMP)co_mouseDragged_imp, "v@:@");
            class_addMethod(vc, osel("mouseUp:"), (IMP)co_mouseUp_imp, "v@:@");
            objc_registerClassPair(vc);
        } else vc = (Class)ocls("VECornerView");
        id win = oNew("NSWindow");
        win = ((id(*)(id, SEL, Rect4, unsigned long, unsigned long, signed char))objc_msgSend)(
            win, osel("initWithContentRect:styleMask:backing:defer:"), g_co_scr, 0UL, 2UL, 0);
        ((void(*)(id, SEL, long))objc_msgSend)(win, osel("setLevel:"), 5L);
        ((void(*)(id, SEL, signed char))objc_msgSend)(win, osel("setOpaque:"), 0);
        ((void(*)(id, SEL, id))objc_msgSend)(win, osel("setBackgroundColor:"), ((id(*)(id, SEL))objc_msgSend)(ocls("NSColor"), osel("clearColor")));
        ((void(*)(id, SEL, signed char))objc_msgSend)(win, osel("setIgnoresMouseEvents:"), 0);
        id view = ((id(*)(id, SEL))objc_msgSend)(((id(*)(id, SEL))objc_msgSend)((id)vc, osel("alloc")), osel("init"));
        Rect4 vf = {0, 0, g_co_scr.w, g_co_scr.h};
        ((void(*)(id, SEL, Rect4))objc_msgSend)(view, osel("setFrame:"), vf);
        ((void(*)(id, SEL, id))objc_msgSend)(win, osel("setContentView:"), view);
        g_co_win = win; g_co_view = view;
        // Рёбра добавляем ПЕРВЫМИ: addSubview кладёт сверху, а линии должны идти под ручками.
        for (int i = 0; i < 4; i++) {
            id e = ((id(*)(id, SEL))objc_msgSend)(((id(*)(id, SEL))objc_msgSend)(ocls("NSView"), osel("alloc")), osel("init"));
            set_round(e, 1.0, 0.13, 0.78, 0.98, 0.75);
            ((void(*)(id, SEL, signed char))objc_msgSend)(e, osel("setHidden:"), 1);
            ((void(*)(id, SEL, id))objc_msgSend)(view, osel("addSubview:"), e);
            g_co_edge[i] = e;
        }
        for (int i = 0; i < 4; i++) {
            id d = ((id(*)(id, SEL))objc_msgSend)(((id(*)(id, SEL))objc_msgSend)(ocls("NSView"), osel("alloc")), osel("init"));
            set_round(d, 9.0, 0.13, 0.78, 0.98, 0.95);   // cyan-ручка
            ((void(*)(id, SEL, id))objc_msgSend)(view, osel("addSubview:"), d);
            g_co_dot[i] = d;
        }
    } else {
        ((void(*)(id, SEL, Rect4, signed char))objc_msgSend)(g_co_win, osel("setFrame:display:"), g_co_scr, 1);
        Rect4 vf = {0, 0, g_co_scr.w, g_co_scr.h};
        ((void(*)(id, SEL, Rect4))objc_msgSend)(g_co_view, osel("setFrame:"), vf);
    }
    co_place_dots();
    ((void(*)(id, SEL, id))objc_msgSend)(g_co_win, osel("orderFront:"), 0);
}
static void co_main(void *_) {   // ГЛАВНЫЙ ПОТОК: ручки на ВЫДЕЛЕННОМ сегменте, пока зажат Ctrl
    (void)_;
    // ВЫБОР ПОВЕРХНОСТИ идёт раньше всего и полностью вытесняет режим ручного искажения: там ручки
    // привязаны к слою и к Ctrl, здесь — к кадру и к кнопке в панели. Смешивать нечего.
    if (surf_gate()) {
        if (!co_preview_frame(&g_co_scr)) {
            if (g_co_win) ((void(*)(id, SEL, id))objc_msgSend)(g_co_win, osel("orderOut:"), 0);
            return;
        }
        co_create_or_update();
        return;
    }
    unsigned long mods = (unsigned long)CGEventSourceFlagsState(1);   // 1=kCGEventSourceStateHIDSystemState (аппаратно)
    int ctrl = (mods & 0x40000) != 0;   // kCGEventFlagMaskControl
    { static long lt = 0; long nw = (long)time(NULL); if (nw != lt) { lt = nw; Lf("%ld CO mods=0x%lx ctrl=%d sel=[%s]\n", nw, mods, ctrl, g_selected_segid); } }
    if (!ctrl || !g_selected_segid[0]) { if (g_co_win) ((void(*)(id, SEL, id))objc_msgSend)(g_co_win, osel("orderOut:"), 0); return; }   // прячем: Ctrl отпущен / нет выделения
    // Сменили слой -> прошлое латченное время чужое: сбрасываем, иначе первый драг уедет в чужой ключ.
    if (strcmp(g_selected_segid, g_co_segid) != 0) { strncpy(g_co_segid, g_selected_segid, 47); g_ph_ours = -1; }
    // Ручки СЛЕДЯТ ЗА ПЛЕЙХЕДОМ: restore на каждом ре-конверте кладёт в corner_pin интерполированное
    // значение, поэтому просто перечитываем поле каждый тик — ручки едут по анимации сами.
    // Читаем ТОЛЬКО когда не тянем (иначе перебьём мышь) и только если corner_pin есть (у свежего
    // клипа он null -> оставляем identity).
    if (g_co_drag < 0) {
        double cur[8];
        void *sg = seg_by_id(g_co_segid);
        // Плейхед принимаем ТОЛЬКО если вычислитель считал НАШ сегмент: движок дёргает
        // getPropertyValues и для чужих сегментов/пре-рендера, и на разных временах. Сам фильтр — в
        // трамплине (см. asm_thunk_cv); отсюда только сообщаем ему, ЧЕЙ сегмент нам нужен.
        g_cp_seg_want = sg;
        long long ph = cp_playhead();   // -1 -> ни одного совпадения ещё не было -> сырое время
        { static long lt = 0; long nw = (long)time(NULL); if (nw != lt) { lt = nw;
            Lf("PHSEG evseg=%p sg=%p match=%d ph=%lld raw=%lld\n",
               g_ph_seg, sg, (g_ph_seg && g_ph_seg == sg), ph, g_playhead); } }
        if (cp_eval_corners(sg, ph, cur) || cp_read_corners(sg, cur))
            for (int i = 0; i < 4; i++) { g_co_ndc[i][0] = cur[i * 2]; g_co_ndc[i][1] = cur[i * 2 + 1]; }
        else { double id4[4][2] = {{-1,-1},{1,-1},{-1,1},{1,1}};
               for (int i = 0; i < 4; i++) { g_co_ndc[i][0] = id4[i][0]; g_co_ndc[i][1] = id4[i][1]; } }
    }
    // ВСЕГДА перечитываем clip: масштаб/сдвиг/поворот слоя могли измениться -> ручки едут за ним
    int rc = co_read_clip(g_co_segid);
    if (!rc) { g_co_sx = 1; g_co_sy = 1; g_co_tx = 0; g_co_ty = 0; g_co_rot = 0; g_co_srcw = 0; g_co_srch = 0; }
    { static long lt = 0; long nw = (long)time(NULL);   // ЛОГ ПОСЛЕ чтения — иначе печатал прошлый тик
      if (nw != lt) { lt = nw;
        Lf("COREAD rc=%d ph=%lld ndc[%.2f,%.2f %.2f,%.2f %.2f,%.2f %.2f,%.2f] sx=%.4f sy=%.4f rot=%.1f src=%.0fx%.0f scr=%.0fx%.0f\n",
           rc, cp_playhead(), g_co_ndc[0][0],g_co_ndc[0][1],g_co_ndc[1][0],g_co_ndc[1][1],
           g_co_ndc[2][0],g_co_ndc[2][1],g_co_ndc[3][0],g_co_ndc[3][1],
           g_co_sx, g_co_sy, g_co_rot, g_co_srcw, g_co_srch, g_co_scr.w, g_co_scr.h); } }
    co_create_or_update();
}

// ============ Route A: нативная строка в панели «Трансформация» (QML-инжект) ============
// Панель — обычный декларативный QML: qrc:/FunctionPanel/common/transform/SegmentTransformView.qml,
// PanelSettingsGroup { Column { ParamSettingSliderViewEx; PanelTableRowItem «Расположение»; ... } }.
// Подменять .qml БЕСПОЛЕЗНО (AOT: скомпилированный QV4-юнит выигрывает у исходника) и
// перерегистрировать qrc НЕЛЬЗЯ (qRegisterResourceData добавляет корень в КОНЕЦ, а поиск берёт
// ПЕРВОЕ совпадение → наш корень всегда проигрывает). Поэтому создаём НОВЫЙ компонент своим
// QQmlComponent::setData по СВОЕМУ url и репарентим в живой Column — AOT-кэшу это безразлично.
// Контекст берём у самого Column → viewModel/keyframeMgr/QmlKeyframe/i18n наследуются даром.
// Гейт: touch ve_qml.txt (rm → перевзвод, можно гонять повторно, выделив клип).
struct QListD { void *d; void **ptr; long size; };   // Qt6 QList = QArrayDataPointer{d,ptr,size}
struct QStrD  { void *d; void *ptr; long size; };    // QString — то же
struct QBAV   { long size; const char *data; };      // QByteArrayView{size,data} (16б → в регистрах)

static QListD (*q_allWindows)(void);
static void  *(*q_contentItem)(void *);
static QListD (*q_childItems)(void *);
static const char *(*q_className)(const void *);
static void  *(*q_qmlEngine)(const void *);
static void  *(*q_qmlContext)(const void *);
static void   (*q_compctor)(void *, void *, void *);
static void   (*q_bactor)(void *, const char *, long);
static QStrD  (*q_fromUtf8)(QBAV);
static void   (*q_urlctor)(void *, const void *, int);
static void   (*q_setData)(void *, const void *, const void *);
static void  *(*q_create)(void *, void *);
static QStrD  (*q_errString)(void *);
static void   (*q_setParentItem)(void *, void *);
static void   (*q_stackBefore)(void *, const void *);   // порядок в Row: наша вкладка не должна прятаться за «…»
static void   (*q_variant_int)(void *, int);
static bool   (*q_setProperty)(void *, const char *, const void *);
// ВАЖНО про ABI: property() возвращает QVariant ПО ЗНАЧЕНИЮ. Нельзя объявлять void-функцией с явным
// sret-параметром — тогда он уедет в x0 и функция примет его за this. Объявляем возврат структурой
// нужного размера (Qt6 QVariant = 32 байта), и clang сам положит &ret в x8, а this в x0.
// (setProperty таким не страдает — он возвращает bool, поэтому и работал сразу.)
struct QVar32 { char b[32]; };
static QVar32 (*q_property)(const void *, const char *);   // this=x0, name=x1, &ret=x8
static QStrD  (*q_toString)(const void *);                 // QVariant::toString -> QString (sret)
static void   (*q_variant_str)(void *, const void *);      // QVariant(const QString&) — сверено по nm
static void  *g_qml_row = 0;   // наша живая строка «Искажение» — через неё зовём родной путь ключей

static void *g_qml_stv = 0, *g_qml_col = 0;   // найденные SegmentTransformView и его Column
static int g_qml_nodes = 0, g_qml_logged = 0;   // бюджет обхода и лога — РАЗНЫЕ (общий кэп 3000 обрывал
                                                // обход на всплывашках до MainWindow_QMLTYPE_485)

static int qml_syms(void) {
    if (q_allWindows) return 1;
    // ВСЕ имена сверены по nm экспортам бандла, не по докам Qt (QQuickItem=10 символов, не 11;
    // fromUtf8 в Qt6 берёт QByteArrayView; setUrlInterceptor НЕ экспортирован, addUrlInterceptor — да).
    #define QS(v, s) v = (decltype(v))dlsym(RTLD_DEFAULT, s); if (!v) Lf("QMLSYM MISS %s\n", s);
    QS(q_allWindows,    "_ZN15QGuiApplication10allWindowsEv")
    QS(q_contentItem,   "_ZNK12QQuickWindow11contentItemEv")
    QS(q_childItems,    "_ZNK10QQuickItem10childItemsEv")
    QS(q_className,     "_ZNK11QMetaObject9classNameEv")
    QS(q_qmlEngine,     "_Z9qmlEnginePK7QObject")
    QS(q_qmlContext,    "_Z10qmlContextPK7QObject")
    QS(q_compctor,      "_ZN13QQmlComponentC1EP10QQmlEngineP7QObject")
    QS(q_bactor,        "_ZN10QByteArrayC1EPKcx")
    QS(q_fromUtf8,      "_ZN7QString8fromUtf8E14QByteArrayView")
    QS(q_urlctor,       "_ZN4QUrlC1ERK7QStringNS_11ParsingModeE")
    QS(q_setData,       "_ZN13QQmlComponent7setDataERK10QByteArrayRK4QUrl")
    QS(q_create,        "_ZN13QQmlComponent6createEP11QQmlContext")
    QS(q_errString,     "_ZNK13QQmlComponent11errorStringEv")
    QS(q_setParentItem, "_ZN10QQuickItem13setParentItemEPS_")
    q_stackBefore = (void (*)(void *, const void *))dlsym(RTLD_DEFAULT, "_ZN10QQuickItem11stackBeforeEPKS_");
    QS(q_variant_int,   "_ZN8QVariantC1Ei")
    QS(q_setProperty,   "_ZN7QObject11setPropertyEPKcRK8QVariant")
    QS(q_property,      "_ZNK7QObject8propertyEPKc")
    QS(q_toString,      "_ZNK8QVariant8toStringEv")
    QS(q_variant_str,   "_ZN8QVariantC1ERK7QString")
    #undef QS
    return q_allWindows && q_contentItem && q_childItems && q_className && q_qmlEngine &&
           q_qmlContext && q_compctor && q_bactor && q_fromUtf8 && q_urlctor && q_setData &&
           q_create && q_errString && q_setParentItem;
}

static void qstr_ascii(const QStrD *s, char *out, int max) {   // QString(UTF-16) → ASCII для лога
    int n = 0;
    if (s && sane_ptr(s->ptr) && s->size > 0)
        for (long i = 0; i < s->size && n < max - 1; i++) {
            unsigned short c = ((unsigned short *)s->ptr)[i];
            out[n++] = (c >= 32 && c < 127) ? (char)c : '?';
        }
    out[n] = 0;
}

// ВНИМАНИЕ №2: sane_ptr нельзя применять и к УКАЗАТЕЛЯМ НА ФУНКЦИИ — в arm64 они выровнены по 4,
// а sane_ptr требует 8 → проверка vt[0] падала как монетка и съедала ~90% окон в "(?)".
static int sane_code(const void *p) { return p && (uintptr_t)p > 0x100000000ULL && ((uintptr_t)p & 3) == 0; }
static const void *q_mo(void *o) {   // QObject::metaObject() — vptr[0] (единичное наследование от QObject)
    if (!has_vtable(o)) return 0;
    void **vt = *(void ***)o;
    if (!sane_code(vt[0])) return 0;
    const void *mo = ((const void *(*)(void *))vt[0])(o);
    return sane_ptr((void *)mo) ? mo : 0;
}
// ВНИМАНИЕ: sane_ptr требует выравнивания по 8 — для const char* из className() это НЕВЕРНО
// (указатель в таблицу строк почти никогда не 8-выровнен) и резало ВСЕ имена в "(?)".
static int sane_str(const void *p) { return p && (uintptr_t)p > 0x100000000ULL; }
static const char *q_cls(void *o) {
    const void *mo = q_mo(o);
    if (!mo) return "(?)";
    const char *n = q_className(mo);
    return sane_str(n) ? n : "(?)";
}
static int q_inherits(void *o, const char *base) {   // superdata = *(void**)mo (офсет 0 в QMetaObject)
    for (const void *m = q_mo(o); sane_ptr((void *)m); m = *(const void **)m) {
        const char *cn = q_className(m);
        if (sane_str(cn) && strstr(cn, base)) return 1;
    }
    return 0;
}

// ponytail: childItems() отдаёт QList по значению — копии не разрушаем (нет экспортного дтора,
// а руками трогать refcount libc++/Qt рискованнее течи). Пробник одноразовый → течь ограничена.
static void qml_walk(void *item, int depth, int inStv) {
    if (!has_vtable(item) || depth > 60 || g_qml_nodes > 300000 || g_qml_col) return;   // нашли — не копаем дальше
    const char *c = q_cls(item);
    g_qml_nodes++;
    const char *mark = "";
    if (strstr(c, "SegmentTransformView")) { g_qml_stv = item; inStv = 1; mark = "   <== ПАНЕЛЬ"; }
    else if (inStv && !g_qml_col && strstr(c, "Column")) { g_qml_col = item; mark = "   <== ЦЕЛЬ (Column)"; }
    // Логируем только то, что ведёт к цели, — полный дамп 3000+ узлов уже не нужен (схему имён знаем)
    if (*mark || inStv || (g_qml_logged < 400 && depth <= 1)) {
        if (g_qml_logged < 3000) { Lf("QMLTREE %*s%s%s\n", depth * 2, "", c, mark); g_qml_logged++; }
    }
    QListD k = q_childItems(item);
    if (!sane_ptr(k.ptr)) return;
    for (long i = 0; i < k.size && i < 512 && !g_qml_col; i++) qml_walk(k.ptr[i], depth + 1, inStv);
}

// Читаем строковое свойство нашей QML-строки: property() отдаёт QVariant по значению (sret),
// затем toString() -> QString (sret) -> ASCII. Обход того, что console.log из QML CapCut глотает.
static void qml_dump_prop(void *obj, const char *name) {
    if (!obj || !q_property || !q_toString) { Lf("QMLDUMP нет символов\n"); return; }
    QVar32 var = q_property(obj, name);
    QStrD s = q_toString(&var);
    char b[3000]; qstr_ascii(&s, b, sizeof b);
    Lf("QMLDUMP %s = %s\n", name, b);
}

// file/url/mark параметризованы: тем же кодом ставим и строку «Искажение» в панель Трансформации,
// и вкладку в ряд вкладок MaterialPanel. mark = имя нашего QML-типа для проверки идемпотентности.
static void *qml_inject(void *col, const char *file, const char *URL, const char *mark) {
    // Идемпотентность: наш item уже в этом контейнере -> ничего не делаем. Так probe можно звать
    // на каждую смену выделения без дублей. Старый item НЕ трогаем указателем: панель
    // пересоздаётся вместе с выделением, и указатель может висеть на освобождённой памяти.
    { QListD k = q_childItems(col);
      if (sane_ptr(k.ptr))
          for (long i = 0; i < k.size; i++)
              if (has_vtable(k.ptr[i]) && strstr(q_cls(k.ptr[i]), mark)) {
                  Lf("QMLINJ %s уже в панели (%p) — пропуск\n", mark, k.ptr[i]);
                  return k.ptr[i];   // уже стоит — просто перецепляемся на него
              } }
    void *eng = q_qmlEngine(col), *ctx = q_qmlContext(col);
    Lf("QMLINJ engine=%p ctx=%p\n", eng, ctx);
    if (!eng || !ctx) { Lf("QMLINJ: нет engine/context\n"); return 0; }

    // QML берём из файла → правим морду БЕЗ пересборки dylib (только re-trigger гейта).
    static const char *FALLBACK =
        "import QtQuick\n"
        "Rectangle { width: 220; height: 26; color: \"#E01B24\"; radius: 3\n"
        "  Text { anchors.centerIn: parent; text: \"PLUGIN OK\"; color: \"white\" } }\n";
    static char srcbuf[262144];   // панель растёт; 64К упирались в потолок на середине фич
    const char *SRC = FALLBACK;
    { int fd = open(file, O_RDONLY);
      if (fd >= 0) { long n = read(fd, srcbuf, sizeof srcbuf - 1); close(fd);
          if (n > 0) { srcbuf[n] = 0; SRC = srcbuf; qml_subst_ud(srcbuf, sizeof srcbuf);
                       Lf("QMLINJ QML из %s (%ld байт)\n", file, n); } }
      else Lf("QMLINJ %s не читается — fallback\n", file); }

    // sizeof(QQmlComponent)==16 (QObject: vptr+d_ptr; всё остальное в d). 512 — с запасом.
    void *comp = calloc(1, 512);
    Lf("QMLINJ [1] ctor comp=%p fn=%p\n", comp, (void *)q_compctor);
    q_compctor(comp, eng, 0);
    Lf("QMLINJ [2] ctor ok, vptr=%p\n", *(void **)comp);

    char ba[32]; memset(ba, 0, sizeof ba);
    q_bactor(ba, SRC, (long)strlen(SRC));
    Lf("QMLINJ [3] QByteArray ok\n");

    // url ДОЛЖЕН лежать в каталоге панели: QQmlDataBlob оставляет m_finalUrl базой для
    // относительных импортов, так что «import FunctionPanel» и соседи резолвятся как из qrc.
    QBAV v; v.size = (long)strlen(URL); v.data = URL;
    QStrD us = q_fromUtf8(v);
    Lf("QMLINJ [4] QString ok size=%ld ptr=%p\n", us.size, us.ptr);
    char url[16]; memset(url, 0, sizeof url);
    q_urlctor(url, &us, 0);   // ParsingMode 0 = TolerantMode
    Lf("QMLINJ [5] QUrl ok d=%p\n", *(void **)url);

    q_setData(comp, ba, url);
    Lf("QMLINJ [6] setData ok\n");
    { QStrD e = q_errString(comp); char b[600]; qstr_ascii(&e, b, sizeof b);
      Lf("QMLINJ [6b] errorString=[%s]\n", b); }
    void *obj = q_create(comp, ctx);
    Lf("QMLINJ [7] create=%p\n", obj);
    if (!obj) {
        QStrD e = q_errString(comp); char b[600]; qstr_ascii(&e, b, sizeof b);
        Lf("QMLINJ create=NULL ОШИБКА: %s\n", b);
        return 0;
    }
    Lf("QMLINJ создан %p class=%s\n", obj, q_cls(obj));
    if (!q_inherits(obj, "QQuickItem")) { Lf("QMLINJ: не QQuickItem — репарент пропущен\n"); return 0; }
    q_setParentItem(obj, col);
    Lf("QMLINJ setParentItem(%p) — ГОТОВО (%s)\n", col, mark);
    return obj;
}

// Qt ругается в stderr, а launcher делает exec → всё уходит в окно Terminal мимо лога.
// Перекидываем fd 2 в наш лог: ошибки QML-компиляции теперь видны здесь всегда.
// stderr уводим в ОТДЕЛЬНЫЙ файл. Раньше лил в plugin_probe.log — тот забивался NUL-байтами,
// становился binary, и macOS grep молча переставал показывать ВСЁ (даже "0" для -c).
static void qml_capture_stderr(void) {
    static int done = 0; if (done) return; done = 1;
    int lf = open(ud("ve_stderr.log"),
                  O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (lf >= 0) { dup2(lf, 2); close(lf); setvbuf(stderr, 0, _IONBF, 0); }
    Lf("QMLPROBE stderr -> ve_stderr.log\n");
}

// Дёргаем pluginTick у нашей QML-строки → она синхронно зовёт РОДНОЙ onClickSetKeyFrame,
// и CapCut сам пишет ключ с текущим (нашим) значением corner-pin. Звать ТОЛЬКО с main-потока
// и ТОЛЬКО до commit/replay, пока модель ещё держит наше значение.
// phase=1 — снять ключ на плейхеде (ДО нашей записи), phase=2 — поставить заново (ПОСЛЕ неё,
// чтобы CapCut захватил уже наше значение). Ставим 0 между фазами, иначе onPluginPhaseChanged
// не сработает повторно на том же числе.
static void qml_autokey(int phase) {
    static int logged = 0;
    if (!g_qml_row || !q_setProperty || !q_variant_int) {
        if (logged < 3) { Lf("AUTOKEY нет: row=%p setProp=%p var=%p\n", g_qml_row,
                             (void *)q_setProperty, (void *)q_variant_int); logged++; }
        return;
    }
    char z[64], v[64]; memset(z, 0, sizeof z); memset(v, 0, sizeof v);
    q_variant_int(z, 0);  q_setProperty(g_qml_row, "pluginPhase", z);   // сброс
    q_variant_int(v, phase);
    bool ok = q_setProperty(g_qml_row, "pluginPhase", v);
    if (logged < 8) { Lf("AUTOKEY phase=%d setProperty=%d\n", phase, (int)ok); logged++; }
}

static void qml_probe_main(void *_) {
    qml_capture_stderr();
    if (!qml_syms()) { Lf("QMLPROBE: символы Qt не резолвятся — стоп\n"); return; }
    g_qml_stv = g_qml_col = 0; g_qml_nodes = g_qml_logged = 0;
    QListD ws = q_allWindows();
    Lf("QMLPROBE окон=%ld\n", ws.size);
    if (!sane_ptr(ws.ptr)) { Lf("QMLPROBE: список окон пуст\n"); return; }
    // Два прохода: сперва MainWindow/EditPanel (панель живёт там), потом остальные — иначе
    // бюджет уходит во всплывашки (11×LVPopupWindow, 6×AIGenerateToolTip).
    for (int pass = 0; pass < 2 && !g_qml_col; pass++)
        for (long i = 0; i < ws.size && !g_qml_col; i++) {
            void *w = ws.ptr[i];
            if (!has_vtable(w)) continue;
            const char *wc = q_cls(w);
            int main_ish = strstr(wc, "MainWindow") || strstr(wc, "EditPanel");
            if (pass == 0 && !main_ish) continue;
            if (pass == 1 && main_ish) continue;
            if (pass == 0) Lf("QMLPROBE окно[%ld] %s  <== ГЛАВНОЕ, копаем\n", i, wc);
            if (!q_inherits(w, "QQuickWindow")) continue;
            void *ci = q_contentItem(w);
            if (has_vtable(ci)) qml_walk(ci, 0, 0);
        }
    Lf("QMLPROBE итог: stv=%p col=%p (узлов %d)\n", g_qml_stv, g_qml_col, g_qml_nodes);
    if (g_qml_col) {
        g_qml_row = qml_inject(g_qml_col, ud("ve_row.qml"),
                               "qrc:/FunctionPanel/common/transform/VEPluginRow.qml", "VEPluginRow");
        if (g_qml_row) qml_dump_prop(g_qml_row, "pluginMatId");   // кого РЕАЛЬНО показывает панель
    }
    else Lf("QMLPROBE: панель не найдена — выдели ВИДЕОКЛИП в CapCut, потом: rm ve_qml.txt && touch ve_qml.txt\n");
}

// ---- ВКЛАДКА В ЛЕВОЙ ВЕРХНЕЙ ПАНЕЛИ («Медиа»/«Аудио»/…) ----
// Панель = qrc:/MaterialPanel/left_tab/base/NewTabPanel.qml:
//   Rectangle root { VEFlickable tabBar { Row tabBarPrivate { Repeater{model: ObjectModel tabBarObj} } }
//                    … Item content }
// Вкладки data-driven (viewmodel.datasource.tabItem(i) → tabBarObj), НО в модель лезть не надо:
// свою кнопку кладём прямо в Row, свою панель — в content (оба id видны нашему QML через
// qmlContext(Row) — это контекст документа NewTabPanel.qml), а родной инспектор гасим их же
// ШТАТНЫМ состоянием viewmodel.index = MainTabEnum.NoFound (им пользуется их собственный reset()).
static void *g_tab_panel = 0, *g_tab_row = 0, *g_tab_item = 0;
static void *g_insp_host = 0, *g_insp_item = 0;   // панель параметров камеры в родном инспекторе
static volatile int g_insp_seltick = 0;   // сменилось выделение -> ретраю инспектора дать полный лимит заново
static int g_tab_nodes = 0, g_tab_logged = 0;

// C++ -> QML строкой: QString::fromUtf8 -> QVariant(QString) -> setProperty. Тем же способом, что
// уже проверен на int (qml_autokey): setProperty находит свойство и обработчик в QML срабатывает.
// ponytail: QString/QVariant тут текут (дторов не зовём) — ~40 байт на СМЕНУ ВЫДЕЛЕНИЯ, не на кадр.
// Если начнёт беспокоить — брать _ZN8QVariantD1Ev, но сейчас это шум на фоне всего остального.
static void qml_set_str(void *obj, const char *name, const char *val) {
    if (!obj || !q_setProperty || !q_variant_str || !q_fromUtf8) return;
    QBAV v; v.size = (long)strlen(val); v.data = val;
    QStrD s = q_fromUtf8(v);
    char var[32]; memset(var, 0, sizeof var);
    q_variant_str(var, &s);
    q_setProperty(obj, name, var);
}

// Панель показывает ЖИВОЕ выделение CapCut — свой таймлайн ей не нужен.
static void qml_push_sel(void *_) {
    if (g_tab_item) qml_set_str(g_tab_item, "selSegId", g_selected_segid);
    if (g_insp_item) qml_set_str(g_insp_item, "selSegId", g_selected_segid);
}

static void qml_tab_walk(void *item, int depth, int inTab) {
    if (!has_vtable(item) || depth > 60 || g_tab_nodes > 300000 || g_tab_row) return;
    const char *c = q_cls(item);
    g_tab_nodes++;
    const char *mark = "";
    // "MainTabPanel" ловит и MainTabPanel_QMLTYPE_n, и NewMainTabPanel_QMLTYPE_n — какой из двух
    // живой, покажет дамп (TabPanel.qml в qrc НЕТ, так что ставлю на New*, но не угадываю — смотрю).
    if (!inTab && strstr(c, "MainTabPanel")) { g_tab_panel = item; inTab = 1; mark = "   <== ПАНЕЛЬ ВКЛАДОК"; }
    else if (inTab && !g_tab_row && strstr(c, "QQuickRow")) { g_tab_row = item; mark = "   <== ЦЕЛЬ (Row вкладок)"; }
    if (*mark || inTab || (g_tab_logged < 300 && depth <= 2))
        if (g_tab_logged < 3000) { Lf("TABTREE %*s%s%s\n", depth * 2, "", c, mark); g_tab_logged++; }
    QListD k = q_childItems(item);
    if (!sane_ptr(k.ptr)) return;
    for (long i = 0; i < k.size && i < 512 && !g_tab_row; i++) qml_tab_walk(k.ptr[i], depth + 1, inTab);
}

// ---- РАМКА ROI: берём РОДНУЮ рамку CapCut, а не рисуем свою ----
// В плеере уже есть Player/view/area_select/RectangleSelectorBase.qml (пунктир + 8 ручек SizeControl
// + перетаскивание + прилипание + перекрестье). EditControl.qml:2716 вешает поверх плеера
// CustomTrackingSelector (её наследника) для СВОЕГО отслеживания.
// Берём БАЗУ, а не CustomTrackingSelector: у того вьюмодель CustomTrackingAreaSelectViewModel
// привязана к состоянию их трекера (видимость решает он). У базы вьюмодель с дефолтом
// (AreaSelectBaseViewModel{}) → визуал их, управление наше.
static void *g_roi_item = 0;
static void *g_find_hit = 0; static const char *g_find_cls = 0; static int g_find_nodes = 0;

static void qml_find_walk(void *item, int depth) {
    if (!has_vtable(item) || depth > 60 || g_find_hit || g_find_nodes > 300000) return;
    g_find_nodes++;
    if (strstr(q_cls(item), g_find_cls)) { g_find_hit = item; return; }
    QListD k = q_childItems(item);
    if (!sane_ptr(k.ptr)) return;
    for (long i = 0; i < k.size && i < 512 && !g_find_hit; i++) qml_find_walk(k.ptr[i], depth + 1);
}
// cls сверять с суффиксом "_QMLTYPE": "EditControl" как подстрока ловит ещё и
// GenericMaskEditControl/ManualBeautyBodyControl — они тоже содержат "EditControl".
static void *qml_find(const char *cls) {
    g_find_hit = 0; g_find_cls = cls; g_find_nodes = 0;
    QListD ws = q_allWindows();
    if (!sane_ptr(ws.ptr)) return 0;
    for (int pass = 0; pass < 2 && !g_find_hit; pass++)
        for (long i = 0; i < ws.size && !g_find_hit; i++) {
            void *w = ws.ptr[i];
            if (!has_vtable(w)) continue;
            const char *wc = q_cls(w);
            int main_ish = (strstr(wc, "MainWindow") || strstr(wc, "EditPanel")) ? 1 : 0;
            if ((pass == 0) != main_ish) continue;
            if (!q_inherits(w, "QQuickWindow")) continue;
            void *ci = q_contentItem(w);
            if (has_vtable(ci)) qml_find_walk(ci, 0);
        }
    Lf("QMLFIND %s -> %p (узлов %d)\n", cls, g_find_hit, g_find_nodes);
    return g_find_hit;
}

// ---- КНОПКА В ШАПКЕ ДОРОЖКИ (SWIRL): привязка дорожки к 3D-камере прямо из таймлайна ----
// Шапки (TimeLineTrackHeader) объявлены внутри дорожек, но C++ CapCut ПЕРЕПАРЕНТИВАЕТ их в
// контейнер левой колонки (MainTimeLine.qml: setHeaderContentView) — искать их под элементом
// дорожки бесполезно, они прямые дети контейнера. Их столько же, сколько дорожек, поэтому
// нужен сбор ВСЕХ, а не первого попавшегося (qml_find возвращает первый).
// Префикс сверяем с НАЧАЛА строки: подстрока "TimeLineTrackHeader" ловит ещё и
// TemplateTimeLineTrackHeader / CaptionTemplateCreatorTimeLineTrackHeader.
#define TRKHDR_MAX 64
static void *g_hdrs[TRKHDR_MAX]; static int g_hdr_n = 0;
static void qml_hdr_walk(void *item, int depth) {
    if (!has_vtable(item) || depth > 60 || g_hdr_n >= TRKHDR_MAX) return;
    if (strncmp(q_cls(item), "TimeLineTrackHeader_QMLTYPE", 27) == 0) { g_hdrs[g_hdr_n++] = item; return; }
    QListD k = q_childItems(item);
    if (!sane_ptr(k.ptr)) return;
    for (long i = 0; i < k.size && i < 512; i++) qml_hdr_walk(k.ptr[i], depth + 1);
}
static int qml_find_headers(void) {
    g_hdr_n = 0;
    QListD ws = q_allWindows();
    if (!sane_ptr(ws.ptr)) return 0;
    for (long i = 0; i < ws.size; i++) {
        void *w = ws.ptr[i];
        if (!has_vtable(w) || !q_inherits(w, "QQuickWindow")) continue;
        const char *wc = q_cls(w);
        if (!strstr(wc, "MainWindow") && !strstr(wc, "EditPanel")) continue;
        void *ci = q_contentItem(w);
        if (has_vtable(ci)) qml_hdr_walk(ci, 0);
    }
    Lf("TRKHDR найдено шапок: %d\n", g_hdr_n);
    return g_hdr_n;
}
// Внутри шапки: корневой Item -> Rectangle -> Row (containerLayout) — туда кладём кнопку.
static void *qml_hdr_row(void *hdr) {
    QListD a = q_childItems(hdr);
    if (!sane_ptr(a.ptr)) return 0;
    for (long i = 0; i < a.size; i++) {
        if (!has_vtable(a.ptr[i])) continue;
        QListD b = q_childItems(a.ptr[i]);
        if (!sane_ptr(b.ptr)) continue;
        for (long j = 0; j < b.size; j++)
            if (has_vtable(b.ptr[j]) && strstr(q_cls(b.ptr[j]), "QQuickRow")) return b.ptr[j];
    }
    return 0;
}
// Ряд иконок в шапке КЛИПАЕТСЯ: ширина колонки считается из headerItemNum (их read-only свойство
// без сигнала), поэтому лишняя кнопка просто не видна. Расширяем контейнер напрямую — биндинг
// при этом рвётся, но ширина колонки статична, пока не сворачивают шапки.
// Геометрию берём прямыми методами QQuickItem, а не через QVariant: у нас нет хелперов для
// double-варианта, а parentItem() из property("parent") пришлось бы распаковывать вручную.
static void  *(*q_parentItem)(const void *) = 0;   // QQuickItem::parentItem
static double (*q_width)(const void *) = 0;        // QQuickItem::width
static void   (*q_setWidth)(void *, double) = 0;   // QQuickItem::setWidth
static void qml_geom_syms(void) {
    if (q_parentItem) return;
    q_parentItem = (void *(*)(const void *))dlsym(RTLD_DEFAULT, "_ZNK10QQuickItem10parentItemEv");
    q_width      = (double (*)(const void *))dlsym(RTLD_DEFAULT, "_ZNK10QQuickItem5widthEv");
    q_setWidth   = (void (*)(void *, double))dlsym(RTLD_DEFAULT, "_ZN10QQuickItem8setWidthEd");
    Lf("TRKHDR гео-символы: parentItem=%p width=%p setWidth=%p\n", (void*)q_parentItem, (void*)q_width, (void*)q_setWidth);
}
// ИНСПЕКТОР КАМЕРЫ. Выделили клип камеры — справа сверху должны быть параметры КАМЕРЫ, а не
// обычные «масштаб/расположение/поворот» клипа. Находим родной SegmentTransformView и ставим
// свою панель ЕМУ ЖЕ в родители: она сама решает, показаться поверх него или уступить.
// Так родной виджет остаётся нетронутым, а мы ничем не рискуем на чужих клипах.
static void qml_insp_walk(void *item, int depth) {
    if (!has_vtable(item) || depth > 60 || g_insp_host) return;
    if (strncmp(q_cls(item), "SegmentTransformView_QMLTYPE", 28) == 0) {
        // q_parentItem резолвится ЛЕНИВО и исторически — из фичи шапки дорожки (qml_geom_syms).
        // Инспектор камеры от неё не зависит и может добежать сюда раньше: тогда указатель ещё
        // нулевой, и `blr x0` уходит по нулю. Поймано минидампом 32e70096: pc=0,
        // lr=ve_hook+0x19364 = ровно этот вызов. Проверяем, а резолв делает qml_insp_inject.
        void *par = q_parentItem ? q_parentItem(item) : 0;
        if (has_vtable(par)) g_insp_host = par;
        return;
    }
    QListD k = q_childItems(item);
    if (!sane_ptr(k.ptr)) return;
    for (long i = 0; i < k.size && i < 512; i++) qml_insp_walk(k.ptr[i], depth + 1);
}
static void qml_insp_inject(void *_) {
    qml_geom_syms();   // обход дерева зовёт q_parentItem — резолвим САМИ, не надеясь на шапку дорожки
    g_insp_host = 0;
    QListD ws = q_allWindows();
    if (!sane_ptr(ws.ptr)) return;
    for (long i = 0; i < ws.size && !g_insp_host; i++) {
        void *w = ws.ptr[i];
        if (!has_vtable(w) || !q_inherits(w, "QQuickWindow")) continue;
        const char *wc = q_cls(w);
        if (!strstr(wc, "MainWindow") && !strstr(wc, "EditPanel")) continue;
        void *ci = q_contentItem(w);
        if (has_vtable(ci)) qml_insp_walk(ci, 0);
    }
    if (!g_insp_host) { static int n = 0; if (++n <= 3) Lf("%ld CAMINSP родной блок трансформации не найден\n", (long)time(NULL)); return; }
    g_insp_item = qml_inject(g_insp_host, ud("ve_caminsp.qml"),
                             "qrc:/FunctionPanel/common/transform/VEPluginCamInsp.qml", "VEPluginCamInsp");
    if (g_insp_item) {
        // Панель встала ПОЗЖЕ смены выделения, поэтому id надо отдать ей сразу: иначе она так и
        // не узнает, что выделена именно камера, и останется невидимой.
        qml_set_str(g_insp_item, "selSegId", g_selected_segid);
        Lf("%ld CAMINSP панель камеры поставлена host=%p sel=%s\n", (long)time(NULL), g_insp_host, g_selected_segid);
    }
}

#define TRKHDR_ADD 22.0
// Базовую ширину запоминаем ОДИН раз: иначе повторный инжект (горячая перезагрузка) накручивал бы
// колонку всё шире и шире.
static double g_hdr_base = 0;
static void qml_hdr_widen(void *item, double add) {   // расширить сам элемент
    if (!q_width || !q_setWidth || !has_vtable(item)) return;
    double w = q_width(item);
    if (w <= 0) return;
    if (g_hdr_base <= 0) g_hdr_base = w;
    double want = g_hdr_base + add;
    if (w < want - 0.5) q_setWidth(item, want);
}
static void qml_hdr_widen_parent(void *hdr, double add) {   // ...и колонку, которую клипает Flickable
    if (!q_parentItem || !q_width || !q_setWidth) return;
    void *cont = q_parentItem(hdr);
    if (!has_vtable(cont)) return;
    double w = q_width(cont);
    if (w <= 0 || g_hdr_base <= 0) return;
    double want = g_hdr_base + add;
    if (w >= want - 0.5) return;
    q_setWidth(cont, want);
    Lf("TRKHDR колонка расширена %.0f -> %.0f (%s)\n", w, want, q_cls(cont));
}

static void qml_trackbtn_inject(void) {
    if (!qml_find_headers()) { Lf("TRKHDR: шапок нет — открыт ли редактор?\n"); return; }
    qml_geom_syms();
    // замер геометрии: понять, что именно клипает
    if (g_hdr_n && q_parentItem && q_width) {
        void *h = g_hdrs[0], *p = q_parentItem(h);
        void *gp = has_vtable(p) ? q_parentItem(p) : 0;
        Lf("TRKHDR гео: hdr w=%.0f | parent %s w=%.0f | grand %s w=%.0f\n",
           q_width(h),
           has_vtable(p) ? q_cls(p) : "-", has_vtable(p) ? q_width(p) : -1.0,
           has_vtable(gp) ? q_cls(gp) : "-", has_vtable(gp) ? q_width(gp) : -1.0);
    }
    int done = 0;
    for (int i = 0; i < g_hdr_n; i++) {
        void *row = qml_hdr_row(g_hdrs[i]);
        if (!row) { Lf("TRKHDR[%d]: Row не найден\n", i); continue; }
        // горячая перезагрузка: снять прошлую кнопку
        { QListD k = q_childItems(row);
          if (sane_ptr(k.ptr))
              for (long j = 0; j < k.size; j++)
                  if (has_vtable(k.ptr[j]) && strstr(q_cls(k.ptr[j]), "VEPluginTrackBtn"))
                      q_setParentItem(k.ptr[j], 0); }
        void *it = qml_inject(row, ud("ve_trackbtn.qml"),
                              "qrc:/TimeLine/track/header/VEPluginTrackBtn.qml", "VEPluginTrackBtn");
        if (it) { done++; qml_dump_prop(it, "pluginDbg"); }   // по КАЖДОЙ: видно сопоставление строка→дорожка
        qml_hdr_widen(g_hdrs[i], TRKHDR_ADD);   // сама шапка
    }
    if (g_hdr_n) qml_hdr_widen_parent(g_hdrs[0], TRKHDR_ADD);   // и колонка целиком (её клипает Flickable)
    Lf("TRKHDR кнопок поставлено: %d из %d\n", done, g_hdr_n);
}

static void qml_roi_inject(void) {
    void *ec = qml_find("EditControl_QMLTYPE");
    if (!ec) { Lf("ROI: EditControl не найден — открыт ли редактор?\n"); return; }
    { QListD k = q_childItems(ec);   // горячая перезагрузка
      if (sane_ptr(k.ptr))
          for (long i = 0; i < k.size; i++)
              if (has_vtable(k.ptr[i]) && strstr(q_cls(k.ptr[i]), "VEPluginRoi")) {
                  Lf("ROI снимаю прошлую рамку %p\n", k.ptr[i]);
                  q_setParentItem(k.ptr[i], 0);
              } }
    // URL в каталоге Player: относительные импорты и «import Player» резолвятся как из qrc.
    g_roi_item = qml_inject(ec, ud("ve_roi.qml"),
                            "qrc:/Player/view/area_select/VEPluginRoi.qml", "VEPluginRoi");
    if (g_roi_item) qml_dump_prop(g_roi_item, "pluginDbg");
}

// ПЛЕЙХЕД: в плеере его НЕТ (измерено — VEPlayerViewModel времени не хранит). Держит его курсор
// ТАЙМЛАЙНА: MainTimeLine.qml:1551 → MainTimeLineCursorViewModel (у него clickSeekByTime(t),
// значит соответствие время↔пиксель он знает). Вешаем невидимый Item в MainTimeLine и читаем оттуда.
static void *g_tl_item = 0;
static void qml_tl_inject(void) {
    void *tl = qml_find("MainTimeLine_QMLTYPE");
    if (!tl) { Lf("TL: MainTimeLine не найден\n"); return; }
    { QListD k = q_childItems(tl);   // горячая перезагрузка
      if (sane_ptr(k.ptr))
          for (long i = 0; i < k.size; i++)
              if (has_vtable(k.ptr[i]) && strstr(q_cls(k.ptr[i]), "VEPluginTl")) q_setParentItem(k.ptr[i], 0); }
    g_tl_item = qml_inject(tl, ud("ve_tlprobe.qml"),
                           "qrc:/TimeLine/main/VEPluginTl.qml", "VEPluginTl");
    if (g_tl_item) qml_dump_prop(g_tl_item, "pluginDbg");
}

// Рамка живёт в плеере, панель — в MaterialPanel: разные деревья. Мост — C++: читаем
// roiJson свойством у рамки и кладём строкой в панель. Оба канала уже проверены.
static void qml_pump_roi(void *_) {
    if (!g_tab_item || !q_property || !q_toString) return;   // плейхед качаем и без рамки
    // рамка -> панель: геометрия долями канваса
    if (g_roi_item) { QVar32 var = q_property(g_roi_item, "roiJson");
      QStrD s = q_toString(&var);
      char b[256]; qstr_ascii(&s, b, sizeof b);   // JSON из чисел — чистый ASCII
      static char last[256] = {0};
      if (strcmp(last, b) != 0) { strncpy(last, b, sizeof last - 1); qml_set_str(g_tab_item, "roiFrac", b); } }
    // панель -> рамка: показывать ТОЛЬКО когда открыта наша вкладка и выбраны источник+follower.
    // Гоняем строкой "0"/"1", а не bool: QVariant(QString) -> bool молча конвертнётся как попало.
    if (g_roi_item) { QVar32 var = q_property(g_tab_item, "roiWanted");
      QStrD s = q_toString(&var);
      char b[16]; qstr_ascii(&s, b, sizeof b);
      static char lastw[16] = {0};
      if (strcmp(lastw, b) != 0) { strncpy(lastw, b, sizeof lastw - 1); qml_set_str(g_roi_item, "roiShow", b); } }
    // таймлайн -> панель: плейхед (мкс). Сверен с индикатором CapCut до микросекунды.
    if (g_tl_item) {
        QVar32 var = q_property(g_tl_item, "playheadUs");
        QStrD s = q_toString(&var);
        char b[32]; qstr_ascii(&s, b, sizeof b);
        static char lastp[32] = {0};
        if (strcmp(lastp, b) != 0) { strncpy(lastp, b, sizeof lastp - 1); qml_set_str(g_tab_item, "playheadUs", b); }
    }
}

// ПРОВЕРКА КОМПИЛЯЦИИ QML БЕЗ ИНЖЕКТА (гейт ve_qmlcheck.txt).
// Панель инжектится в редактор, а он существует только при ОТКРЫТОМ проекте — значит при закрытом
// (или при запертом экране) правку морды нечем проверить. Здесь мы берём ЛЮБОЕ окно ради движка и
// делаем только `setData`: он компилирует исходник целиком (синтаксис, резолв типов, присваивания
// несуществующим свойствам) и кладёт всё в errorString. `create()` НЕ зовём — иначе выполнится
// Component.onCompleted и морда полезет в сеть/дерево, чего при проверке не нужно.
static void qml_compile_check(void *_) {
    qml_capture_stderr();
    if (!qml_syms()) { Lf("QMLCHECK: символы Qt не резолвятся\n"); return; }
    QListD ws = q_allWindows();
    void *eng = 0;
    if (sane_ptr(ws.ptr))
        for (long i = 0; i < ws.size && !eng; i++) {
            void *w = ws.ptr[i];
            if (!has_vtable(w) || !q_inherits(w, "QQuickWindow")) continue;
            void *ci = q_contentItem(w);
            if (has_vtable(ci)) eng = q_qmlEngine(ci);
        }
    if (!eng) { Lf("QMLCHECK: QML-движок не найден (окон %ld)\n", ws.size); return; }
    // ВСЕ инжектируемые QML, а не только панель: кнопка в шапке дорожки раньше в проверку не
    // входила — правишь её и узнаёшь об опечатке только по пустой шапке на экране.
    // Имена, а не пути: ud() отдаёт кольцевой буфер, в static-массиве такой указатель
    // протухнет на следующих вызовах. Путь собираем в момент использования.
    static const char *FILES[5] = {
        "ve_tab.qml", "ve_roi.qml", "ve_tlprobe.qml", "ve_trackbtn.qml", "ve_caminsp.qml" };
    static const char *URLS[5] = {
        "qrc:/MaterialPanel/left_tab/base/VEPluginTab.qml",
        "qrc:/Player/view/area_select/VEPluginRoi.qml",
        "qrc:/TimeLine/main/VEPluginTl.qml",
        "qrc:/TimeLine/track/header/VEPluginTrackBtn.qml",
        "qrc:/FunctionPanel/common/transform/VEPluginCamInsp.qml" };
    for (int i = 0; i < 5; i++) {
        static char src[262144];
        int fd = open(ud(FILES[i]), O_RDONLY);
        if (fd < 0) { Lf("QMLCHECK %s: не читается\n", FILES[i]); continue; }
        long n = read(fd, src, sizeof src - 1); close(fd);
        if (n <= 0) { Lf("QMLCHECK %s: пусто\n", FILES[i]); continue; }
        src[n] = 0;
        void *comp = calloc(1, 512);
        q_compctor(comp, eng, 0);
        char ba[32]; memset(ba, 0, sizeof ba);
        q_bactor(ba, src, (long)strlen(src));
        QBAV v; v.size = (long)strlen(URLS[i]); v.data = URLS[i];
        QStrD us = q_fromUtf8(v);
        char url[16]; memset(url, 0, sizeof url);
        q_urlctor(url, &us, 0);
        q_setData(comp, ba, url);
        QStrD e = q_errString(comp); char b[900]; qstr_ascii(&e, b, sizeof b);
        Lf("QMLCHECK %s (%ld байт): %s\n", FILES[i], n,
           b[0] ? b : "ЧИСТО — компилируется");
        // ponytail: comp не освобождаем — проверка ручная и разовая, экспортного дтора нет
    }
}

static void qml_tabprobe_main(void *_) {
    qml_capture_stderr();
    if (!qml_syms()) { Lf("TABPROBE: символы Qt не резолвятся — стоп\n"); return; }
    g_tab_panel = g_tab_row = 0; g_tab_nodes = g_tab_logged = 0;
    QListD ws = q_allWindows();
    Lf("TABPROBE окон=%ld\n", ws.size);
    if (!sane_ptr(ws.ptr)) { Lf("TABPROBE: список окон пуст\n"); return; }
    for (int pass = 0; pass < 2 && !g_tab_row; pass++)
        for (long i = 0; i < ws.size && !g_tab_row; i++) {
            void *w = ws.ptr[i];
            if (!has_vtable(w)) continue;
            const char *wc = q_cls(w);
            int main_ish = strstr(wc, "MainWindow") || strstr(wc, "EditPanel");
            if (pass == 0 && !main_ish) continue;
            if (pass == 1 && main_ish) continue;
            Lf("TABPROBE окно[%ld] %s%s\n", i, wc, pass == 0 ? "  <== ГЛАВНОЕ" : "");
            if (!q_inherits(w, "QQuickWindow")) continue;
            void *ci = q_contentItem(w);
            if (has_vtable(ci)) qml_tab_walk(ci, 0, 0);
        }
    Lf("TABPROBE итог: panel=%p row=%p (узлов %d)\n", g_tab_panel, g_tab_row, g_tab_nodes);
    if (!g_tab_row) { Lf("TABPROBE: ряд вкладок не найден\n"); return; }
    // ГОРЯЧАЯ ПЕРЕЗАГРУЗКА: снимаем прошлую вкладку с Row → проверка идемпотентности в qml_inject
    // её не найдёт и создаст компонент заново из свежего ve_tab.qml. Т.е. правка морды =
    // rm+touch гейта, без пересборки dylib. Свою панель в content наш QML сносит сам по objectName.
    // ponytail: снятый item течёт (экспортного дтора нет, deleteLater из C++ дороже пользы).
    // Течёт только по re-trigger'у гейта, т.е. при разработке — на проде гейт дёргается один раз.
    { QListD k = q_childItems(g_tab_row);
      if (sane_ptr(k.ptr))
          for (long i = 0; i < k.size; i++)
              if (has_vtable(k.ptr[i]) && strstr(q_cls(k.ptr[i]), "VEPluginTab")) {
                  Lf("TABPROBE снимаю прошлую вкладку %p — перезагрузка QML\n", k.ptr[i]);
                  q_setParentItem(k.ptr[i], 0);
              } }
    g_tab_item = qml_inject(g_tab_row, ud("ve_tab.qml"),
                            "qrc:/MaterialPanel/left_tab/base/VEPluginTab.qml", "VEPluginTab");
    // setParentItem дописывает в КОНЕЦ ряда, а там вкладка уезжает за стрелку «…» — её надо
    // искать, что для продукта неприемлемо. Двигаем сразу за «Медиаматериалы» (индекс 1).
    if (g_tab_item && q_stackBefore) {
        QListD k = q_childItems(g_tab_row);
        if (sane_ptr(k.ptr) && k.size > 1) {
            void *second = 0; int seen = 0;
            for (long i = 0; i < k.size; i++) {
                if (!has_vtable(k.ptr[i]) || k.ptr[i] == g_tab_item) continue;
                if (++seen == 2) { second = k.ptr[i]; break; }
            }
            if (second) { q_stackBefore(g_tab_item, second); Lf("TAB переставлена на 2-ю позицию\n"); }
        }
    }
    // console.log CapCut глотает → результаты резолва (content/root.viewmodel/MainTabEnum)
    // наша QML складывает в строковое свойство, читаем его через QObject::property.
    if (g_tab_item) { qml_push_sel(0); qml_dump_prop(g_tab_item, "pluginDbg"); }
    qml_roi_inject();   // тем же гейтом — рамку в плеер
    qml_tl_inject();    // и невидимый Item в таймлайн — за плейхедом
    qml_trackbtn_inject();   // и кнопку привязки к 3D-камере в шапку каждой дорожки
}

static void *ovl_poll(void *_) {    // читает ve_progress.txt, шлёт обновление на главный поток
    for (;;) {
        // ve_op_probe.txt (rm+touch) -> разовый прямой опрос гейта. НЕ авто-фаер на старте:
        // op_mt инициализируем текущим mtime существующего файла (первый проход), плюс ждём
        // g_ready+4с чтобы флип-хук точно встал. Иначе гонка: проба стреляет до установки флипа.
        { static long op_mt = -1; struct stat ost;
          if (stat(ud("ve_op_probe.txt"), &ost) == 0) {
              long mt = (long)ost.st_mtimespec.tv_sec * 1000000000L + ost.st_mtimespec.tv_nsec;
              if (op_mt == -1) op_mt = mt;   // первый проход: запоминаем, НЕ стреляем
              else if (mt != op_mt) { op_mt = mt;
                  if (g_ready && (long)time(NULL) - g_ready >= 4) op_probe_gate();
                  else Lf("%ld  OPGATE отложен (хуки ещё не готовы)\n", (long)time(NULL)); } } }
        struct stat pst;   // mtime прогресса: по нему отличаем зависшую плашку от новой операции
        long pmt = (stat(ud("ve_progress.txt"), &pst) == 0)
            ? (long)pst.st_mtimespec.tv_sec * 1000000000L + pst.st_mtimespec.tv_nsec : -1;
        FILE *f = fopen(ud("ve_progress.txt"), "r");
        if (f) {
            char line[160] = {0};
            if (fgets(line, sizeof(line), f)) {
                double pct = atof(line);
                char *sp = strchr(line, ' '); char msg[128] = {0};
                if (sp) { strncpy(msg, sp + 1, 127); char *nl = strchr(msg, '\n'); if (nl) *nl = 0; }
                g_ovl_pct = pct; strncpy(g_ovl_msg, msg, 127); g_ovl_msg[127] = 0;
                g_ovl_visible = (pct >= 0.0 && pct < 100.0) && pmt != g_ovl_dis_mt;
            }
            fclose(f);
        } else g_ovl_visible = 0;
        dispatch_async_f(dispatch_get_main_queue(), 0, ovl_update);
        // ve_curve.txt готов -> читаем disk-id follower'а, вооружаемся; getseg_capture найдёт живой
        // Segment по совпадению id (get_segment зовётся и внутренне -> follower всплывёт сам, без клика).
        struct stat cst;
        if (stat(ud("ve_curve.txt"), &cst) == 0) {
            long mt = (long)cst.st_mtimespec.tv_sec * 1000000000L + cst.st_mtimespec.tv_nsec;
            if (mt != g_auto_mt) {
                g_auto_mt = mt; g_armed = 1; g_follower_seg = 0;
                readSegmentId(g_want_id, sizeof(g_want_id));
                for (int i = 0; i < g_nid; i++)   // follower уже в карте с загрузки -> находим сразу
                    if (strcmp(g_id_str[i], g_want_id) == 0) { g_follower_seg = g_id_seg[i]; break; }
                Lf("%ld ARMED want_id=%s follower=%p\n", (long)time(NULL), g_want_id, g_follower_seg);
            }
            if (g_armed && g_follower_seg) {   // follower найден по id
                g_armed = 0;
                Lf("%ld APPLY follower seg=%p\n", (long)time(NULL), g_follower_seg);
                dispatch_async_f(dispatch_get_main_queue(), 0, auto_apply_main);
            }
        }
        // LIVE-DRIVE: rig по mtime + резолв клипа камеры + пересчёт слоёв (всё через модель -> на main)
        struct stat rst;
        if (stat(ud("ve_rig.txt"), &rst) == 0) {
            long rmt = (long)rst.st_mtimespec.tv_sec * 1000000000L + rst.st_mtimespec.tv_nsec;
            // ре-драйв НА СМЕНУ РИГА: панель меняет глубину/привязку живьём, движения камеры при этом нет,
            // а без него новая глубина не применилась бы до следующего касания камеры. Первую загрузку
            // (g_rig_mt==0) пропускаем: там кривых камеры ещё нет, драйв всё равно вышел бы вхолостую.
            if (rmt != g_rig_mt) { int first = (g_rig_mt == 0); g_rig_mt = rmt; load_rig(); if (!first) g_drive_pending = 1; }
        } else if (g_rig.have) { g_rig.have = 0; g_nscene = 0; }
        { struct stat cst;   // ЖИВОЙ путь камеры из панели — тот же приём (mtime), кладём в кэш кривых
          if (stat(ud("ve_campath.txt"), &cst) == 0) {
              long m = (long)cst.st_mtimespec.tv_sec * 1000000000L + cst.st_mtimespec.tv_nsec;
              if (m != g_campath_mt) { g_campath_mt = m; if (g_rig.have) load_campath(); }
          } }
        if (g_drive_pending && !g_driving) { g_drive_pending = 0; dispatch_async_f(dispatch_get_main_queue(), 0, drive_layers_main); }
        { struct stat cst;   // CORNER-PIN: применяем на изменение ve_cornerpin.txt (в т.ч. первое появление после загрузки)
          if (stat(ud("ve_cornerpin.txt"), &cst) == 0) {
              long m = (long)cst.st_mtimespec.tv_sec * 1000000000L + cst.st_mtimespec.tv_nsec;
              if (m != g_cp_mt) { g_cp_mt = m; g_cp_pending = 1; g_cp_tries = 0; }
          } }
        if (g_cp_pending && !g_driving && has_vtable(g_seq)) {   // ждём живую секвенцию; ретраим пока сегмент не зарезолвится
            g_cp_pending = 0; dispatch_async_f(dispatch_get_main_queue(), 0, drive_cornerpin_main); }
        { struct stat kst;   // КЛЮЧ СВОЕЙ КОМАНДОЙ: выстрел на каждое изменение ve_kfreq.txt
          static long g_kf_mt = 0;
          if (stat(ud("ve_kfreq.txt"), &kst) == 0) {
              long m = (long)kst.st_mtimespec.tv_sec * 1000000000L + kst.st_mtimespec.tv_nsec;
              if (m != g_kf_mt && has_vtable(g_seq)) {   // mtime съедаем ТОЛЬКО когда реально стреляем:
                  g_kf_mt = m;                          // иначе выстрел в момент загрузки проекта теряется
                  dispatch_async_f(dispatch_get_main_queue(), 0, drive_kfreq_main); }
          } else g_kf_mt = 0; }
        { struct stat sst;   // СТАТИСТИКА: кто из источников секвенции вообще стреляет и что мешает кику
          static long g_ss_mt = 0;
          if (stat("/Users/naza.prod/Movies/CapCut/User Data/ve_seqstat.txt", &sst) == 0) {
              long m = (long)sst.st_mtimespec.tv_sec * 1000000000L + sst.st_mtimespec.tv_nsec;
              if (m != g_ss_mt) { g_ss_mt = m;
                  void *w = g_wrap_cap ? g_wrap_cap : (void *)g_screen_wrapper;
                  void *ws = has_vtable(w) ? *(void **)((char *)w + 0x408) : 0;
                  int live = 0; for (int i = 0; i < g_nid; i++)
                      if (has_vtable(g_id_seg[i]) && *(void **)g_id_seg[i] == g_id_vt[i]) live++;
                  Lf("%ld SEQSTAT g_seq=%p vtable=%d | srv=%p sid=%ld nid=%d живых=%d driving=%d | "
                     "хуки: setCurEditSeq=%ld prepare=%ld updateAvClip=%ld onFrameRendered=%ld "
                     "Seq.setKeyframe=%ld commit=%ld getDuration=%ld | wrapper=%p seq@408=%p\n",
                     (long)time(NULL), g_seq, has_vtable(g_seq) ? 1 : 0, g_srv, (long)g_sid, g_nid, live,
                     g_driving, g_hk[11], g_hk[12], g_hk[13], g_hk[14], g_hk[2], g_hk[3], g_hk[19], w, ws); }
          } else g_ss_mt = 0; }
        { struct stat dst;   // ИНЪЕКЦИЯ ОТКАЗА: роняем g_seq, как это делает загрузка секвенции —
          static long g_sd_mt = 0;   // иначе ветку подъёма не проверить, движок коммитит сам и слишком быстро
          if (stat("/Users/naza.prod/Movies/CapCut/User Data/ve_seqdrop.txt", &dst) == 0) {
              long m = (long)dst.st_mtimespec.tv_sec * 1000000000L + dst.st_mtimespec.tv_nsec;
              if (m != g_sd_mt) { g_sd_mt = m;
                  { void *w1 = g_wrap_cap, *w2 = (void *)g_screen_wrapper;
                    void *s1 = has_vtable(w1) ? *(void **)((char *)w1 + 0x408) : 0;
                    void *s2 = has_vtable(w2) ? *(void **)((char *)w2 + 0x408) : 0;
                    Lf("%ld SEQDROP: g_seq=%p | wrap_cap=%p seq@408=%p (%s) | screen=%p seq@408=%p (%s)\n",
                       (long)time(NULL), g_seq, w1, s1, s1 == g_seq ? "СОВПАЛ" : "нет",
                       w2, s2, s2 == g_seq ? "СОВПАЛ" : "нет"); }
                  g_seq = 0; g_seq_kick = 1; g_kick_tries = 0; g_seq_kick_at = (long)time(NULL); }
          } else g_sd_mt = 0; }
        // Подъём секвенции: условие — САМО СОСТОЯНИЕ, а не событие. Событие «секвенция загружена»
        // (setCurEditSeq, pre.setSequence) на этой сборке не стреляет НИ РАЗУ — замерено, оба хука
        // стоят, а вызовов ноль. Поэтому смотрим прямо: указателя нет, а проект есть — поднимаем.
        if (!has_vtable(g_seq) && g_srv && g_sid && g_nid > 0 && !g_driving
            && (long)time(NULL) - g_seq_kick_at >= 2) {
            g_seq_kick_at = (long)time(NULL);
            dispatch_async_f(dispatch_get_main_queue(), 0, kick_seq_main);
        }
        { struct stat wst;   // ЖИВОЙ ЗАВОРОТ: выстрел на изменение ve_wrap.txt
          static long g_w_mt = 0;
          if (stat("/Users/naza.prod/Movies/CapCut/User Data/ve_wrap.txt", &wst) == 0) {
              long m = (long)wst.st_mtimespec.tv_sec * 1000000000L + wst.st_mtimespec.tv_nsec;
              if (m != g_w_mt && has_vtable(g_seq)) { g_w_mt = m;
                  dispatch_async_f(dispatch_get_main_queue(), 0, drive_wrap_main); }
          } else g_w_mt = 0; }
        { struct stat rst2;   // РЕПЛЕЙ ПАРЫ: выстрел на изменение ve_replay2.txt
          static long g_r2_mt = 0;
          if (stat("/Users/naza.prod/Movies/CapCut/User Data/ve_replay2.txt", &rst2) == 0) {
              long m = (long)rst2.st_mtimespec.tv_sec * 1000000000L + rst2.st_mtimespec.tv_nsec;
              if (m != g_r2_mt && has_vtable(g_seq)) { g_r2_mt = m;
                  dispatch_async_f(dispatch_get_main_queue(), 0, replay_pair_main); }
          } else g_r2_mt = 0; }
        { struct stat tst;   // ГАБАРИТ НАДПИСИ: пробник ответа get-команд
          static long g_tb_mt = 0;
          if (stat("/Users/naza.prod/Movies/CapCut/User Data/ve_textbox.txt", &tst) == 0) {
              long m = (long)tst.st_mtimespec.tv_sec * 1000000000L + tst.st_mtimespec.tv_nsec;
              if (m != g_tb_mt && has_vtable(g_seq)) { g_tb_mt = m;
                  dispatch_async_f(dispatch_get_main_queue(), 0, drive_textbox_main); }
          } else g_tb_mt = 0; }
        { struct stat mst;   // МАССОВАЯ правка трека + одна команда (замер «весь трек или плейхед»)
          static long g_km_mt = 0;
          if (stat(ud("ve_kfmass.txt"), &mst) == 0) {
              long m = (long)mst.st_mtimespec.tv_sec * 1000000000L + mst.st_mtimespec.tv_nsec;
              if (m != g_km_mt && has_vtable(g_seq)) {   // см. выше: mtime съедаем только при выстреле
                  g_km_mt = m;
                  dispatch_async_f(dispatch_get_main_queue(), 0, drive_kfmass_main); }
          } else g_km_mt = 0; }
        g_cp_freeze = (access("/Users/naza.prod/Movies/CapCut/User Data/ve_cpfreeze.txt", F_OK) == 0);
        g_no_kick = (access("/Users/naza.prod/Movies/CapCut/User Data/ve_no_kick.txt", F_OK) == 0);
        g_cp_ease = (access(ud("ve_cpease.txt"), F_OK) == 0);
        g_cp_doc  = (access(ud("ve_cpdoc.txt"), F_OK) == 0);
        g_kfsnap  = (access(ud("ve_kfsnap.txt"), F_OK) == 0);
        g_trace = (access(ud("ve_cptrace.txt"), F_OK) == 0);   // трейс сигнала ре-рендера
        // QML-пробник: touch ve_qml.txt → дамп дерева + инжект; rm → перевзвод (гоняем повторно)
        { static int qm = 0; int has = (access(ud("ve_qml.txt"), F_OK) == 0);
          if (has && !qm) { qm = 1; dispatch_async_f(dispatch_get_main_queue(), 0, qml_probe_main); }
          else if (!has) qm = 0; }
        // АВТОИНЖЕКТ вкладки — без всяких файлов и команд: как только открыт проект (есть живая
        // секвенция) и вкладка ещё не стоит, пробуем поставить. На САМОМ старте это невозможно
        // (QML-дерева ещё нет: "TABPROBE окон=0"), поэтому именно ретрай, а не разовая попытка.
        // qml_inject идемпотентен (находит своего ребёнка -> пропуск), так что лишний вызов безвреден.
        // Проект переоткрыли (hook3 поднял флаг) -> инжектнутые item'ы умерли вместе с QML-деревом.
        // Обнуляем указатели, чтобы условие автоинжекта ниже снова стало истинным и вкладка вернулась.
        // Сами item'ы НЕ трогаем и не удаляем: они уже уничтожены движком, обращение к ним = краш.
        if (g_qml_dead) { g_qml_dead = 0; g_tab_item = 0; g_tab_panel = 0; g_tab_row = 0;
                          g_roi_item = 0; g_tl_item = 0; g_insp_item = 0; g_insp_host = 0; g_insp_seltick = 1;
                          Lf("%ld TAB: проект переоткрыт — жду пересборку QML и ставлю вкладку заново\n", (long)time(NULL)); }
        { static int tick = 0;
          if (!g_tab_item && has_vtable(g_seq) && ++tick >= 7) {   // 7 тиков × 300мс ≈ 2с
              tick = 0; dispatch_async_f(dispatch_get_main_queue(), 0, qml_tabprobe_main); } }
        // Файл-гейт ОСТАЁТСЯ, но теперь только для РАЗРАБОТКИ: rm+touch ve_tabqml.txt = горячая
        // перезагрузка ve_tab.qml/ve_roi.qml без пересборки dylib и без перезапуска CapCut.
        { static int tq = 0; int has = (access(ud("ve_tabqml.txt"), F_OK) == 0);
          if (has && !tq) { tq = 1; dispatch_async_f(dispatch_get_main_queue(), 0, qml_tabprobe_main); }
          else if (!has) tq = 0; }
        // отдельный гейт: ТОЛЬКО скомпилировать морду и написать ошибки в лог (работает и без проекта)
        { static int cq = 0; int has = (access(ud("ve_qmlcheck.txt"), F_OK) == 0);
          if (has && !cq) { cq = 1; dispatch_async_f(dispatch_get_main_queue(), 0, qml_compile_check); }
          else if (!has) cq = 0; }
        dispatch_async_f(dispatch_get_main_queue(), 0, qml_pump_roi);   // рамка -> панель (только на изменение)
        // АВТОИНЖЕКТ: выделили сегмент -> панель пересоздалась -> ставим строку. qml_inject
        // идемпотентен (уже стоит -> пропуск), поэтому лишний вызов безвреден.
        { static char lastsel[48] = {0};
          if (g_selected_segid[0] && strcmp(lastsel, g_selected_segid) != 0) {
              strncpy(lastsel, g_selected_segid, 47);
              dispatch_async_f(dispatch_get_main_queue(), 0, qml_probe_main);
              g_insp_item = 0;   // панель инспектора пересоздаётся на каждое выделение
              g_insp_seltick = 1;
              dispatch_async_f(dispatch_get_main_queue(), 0, qml_push_sel); } }   // выделение -> в панель
        // Панель инспектора собирается АСИНХРОННО уже после выделения, поэтому не разовый
        // инжект, а ретрай: пока что-то выделено и нашей панели там нет — пробуем.
        // has_vtable на самом item'е обязателен: родной инспектор пересобирается не только на
        // смену выделения (например при переключении глубины резкости), и тогда наш item умирает
        // вместе с деревом, а указатель остаётся ненулевым. Без этой проверки ретрай считал, что
        // панель на месте, и пользователь видел вместо параметров камеры родные «Масштаб/
        // Расположение/Поворот» — ровно та жалоба «наши настройки пропали».
        //
        // ⚠️ ПЕРИОД НЕ УСКОРЯТЬ. Пробовал 2026-08-09 гонять этот же инжект внутри 50-мс шага
        // (чтобы убрать вспышку родных свойств после клика) — CapCut падает через секунду после
        // ОТКРЫТИЯ проекта, дважды подряд, последняя строка лога `TRKHDR гео-символы` из начала
        // qml_insp_inject. Обход дерева попадает внутрь загрузки, где item'ы ещё строятся, и
        // has_vtable(g_seq) от этого не спасает. 600 мс — проверенный временем интервал.
        //
        // ПОТОЛОК ПОПЫТОК. Без него ретрай молотит полный обход QML-дерева ВЕЧНО в двух обычных
        // ситуациях: выделен клип без блока трансформации (звук, эффект) и сразу после загрузки
        // проекта, когда id в g_selected_segid остался от загрузки, а выделения на экране нет.
        // Замерено 2026-08-09: «родной блок не найден» три раза (дальше лог молчит по счётчику),
        // а обход продолжался бесконечно. 20 попыток × 600 мс = 12 секунд — с запасом на самую
        // медленную пересборку панели. Счётчик обнуляется сменой выделения, смертью нашего item'а
        // (панель пересобралась — это НОВОЕ событие) и пересборкой дерева, поэтому каждый клик
        // получает полный лимит заново.
        { static int it = 0, tries = 0;
          if (g_insp_item && !has_vtable(g_insp_item)) { g_insp_item = 0; tries = 0; }
          if (g_insp_seltick) { g_insp_seltick = 0; tries = 0; }
          if (!g_insp_item && g_selected_segid[0] && has_vtable(g_seq) && tries < 20 && ++it >= 2) {
              it = 0; tries++; dispatch_async_f(dispatch_get_main_queue(), 0, qml_insp_inject); } }
        dispatch_async_f(dispatch_get_main_queue(), 0, co_main);   // corner-pin оверлей: Ctrl-гейт + выделенный сегмент (co_main решает show/hide)
        { FILE *cf = fopen(ud("ve_cpcmd.txt"), "r");   // имя класса ReqStruct для захвата+реплея
          if (cf) { char b[80] = {0};
              if (fscanf(cf, "%79s", b) == 1) {
                  if (b[0] == '0' && (b[1] == 'x' || b[1] == 'X')) { g_cmd_off = strtol(b, 0, 16); g_cmd_type[0] = 0; }
                  else { g_cmd_off = 0;
                      char *comma = strchr(b, ',');
                      if (comma) { *comma = 0; strncpy(g_cmd2_type, comma + 1, sizeof g_cmd2_type - 1); }
                      else g_cmd2_type[0] = 0;
                      strncpy(g_cmd_type, b, sizeof g_cmd_type - 1); }
              } else { g_cmd_type[0] = 0; g_cmd2_type[0] = 0; g_cmd_off = 0; }
              fclose(cf);
          } else { g_cmd_type[0] = 0; g_cmd2_type[0] = 0; g_cmd_off = 0; } }
        // Тот же период 300 мс, но нарезанный: мышь опрашиваем каждые 50 мс, иначе крестик
        // появлялся бы с задержкой в треть секунды, а быстрый клик проскакивал бы между тиками.
        for (int i = 0; i < 6; i++) { ovl_mouse_tick(); usleep(50000); }
    }
    return 0;
}

__attribute__((constructor))
static void init(void) {
    char path[4096]; uint32_t sz = sizeof(path);
    if (_NSGetExecutablePath(path, &sz) != 0) return;
    size_t len = strlen(path); const char *suf = "/MacOS/CapCut";
    if (len < strlen(suf) || strcmp(path + len - strlen(suf), suf) != 0) return;
    Lf("%ld ve_hook loaded pid=%d\n", (long)time(NULL), getpid());
    unlink(ud("ve_hide_panels.txt"));   // очистить залипший флаг (Python убили в reopen)
    pthread_t th; pthread_create(&th, NULL, worker, NULL); pthread_detach(th);
    dispatch_async_f(dispatch_get_main_queue(), 0, ovl_create);   // окно прогресса на главном потоке
    struct stat cst;   // снимок текущего ve_curve.txt — авто-триггер сработает только на НОВЫЙ трек
    g_auto_mt = (stat(ud("ve_curve.txt"), &cst) == 0)
        ? (long)cst.st_mtimespec.tv_sec * 1000000000L + cst.st_mtimespec.tv_nsec : -1;
    pthread_t pt; pthread_create(&pt, NULL, ovl_poll, NULL); pthread_detach(pt);   // опрос прогресса
}
