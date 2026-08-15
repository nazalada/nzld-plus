#!/usr/bin/env python3
"""2.5D параллакс-камера: запекает виртуальную 3D-камеру в per-layer 2D-ключи (позиция/масштаб/поворот).

CapCut — 2D-композитор, настоящей 3D-камеры в модели нет (см. RE в памяти). Приём как в After Effects
«3D-слои + камера» для 2D-движка: каждый слой = плоская карточка на глубине Z, виртуальная пинхол-камера
двигается, а мы проецируем каждый слой в 2D (сдвиг + перспективный масштаб) и пишем это как обычные
ключи трансформа — через тот же движок, что и трекер (curves_by_target -> write_live_curve/inject_curves).

ПОТОЛОК (честно): это МУЛЬТИПЛЕЙН-параллакс (карточки на глубинах), а не объёмное 3D. Плоский слой в
CapCut нельзя перекосить (нет corner-pin/перспективного варпа), поэтому «поворот камеры вокруг сцены»
даёт сильный параллакс + масштаб, но не настоящий разворот плоскости. Для истинного 3D-объекта — рендер
в Blender и импорт видео-слоем (отдельный путь).

Значения CapCut (как в capcut_track.compute_curves_ml):
  position value = px_смещение_от_центра / (canvas/2);  y инвертируется (SIGN_Y=-1, CapCut y-вверх)
  scale value    = отношение (1.0 = 100%)
  rotation value = градусы
  time           = микросекунды источника
"""
import math

SIGN_Y = -1.0            # CapCut Y вверх, экран Y вниз (как в capcut_track)
FOCAL_FRAC = 1.15        # фокус = FOCAL_FRAC * высота_канваса (больше -> уже FOV -> меньше перспективы)
NEAR, FAR = 1.0, 5.0     # ползунок глубины 0..1 -> Z в [NEAR..FAR]; камера в Z=0, слои перед ней
Z_REF = 2.0              # опорная глубина (план «экрана»): панорама на ней = заданному сдвигу
# Плоскость фокуса по умолчанию, в тех же единицах z, что и глубина слоёв. Живёт в масштабе Y
# клипа камеры (uniform_scale выключен) — это её единственный свободный ключуемый канал.
DOF_FOCUS_DEFAULT = 1.96
SAMPLES = 60             # число ключей на слой поперёк движения
SCALE_CLAMP = (0.15, 12.0)   # только страховка от вырожденного кадра; наезд ограничивает превью (cz<z_min), а не этот кламп
POS_CLAMP = 4.0          # клип значения позиции (в half-canvas), чтобы улетевший слой не ломал проект


def depth_to_z(d):
    """ползунок глубины 0(перед)..1(даль) -> мировая Z."""
    d = max(0.0, min(1.0, d))
    return NEAR + (FAR - NEAR) * d


def project(bx, by, z, cx, cy, cz, f, roll=0.0):
    """Пинхол-проекция карточки на глубине z под камерой (cx,cy,cz), с РОЛЛОМ камеры (градусы).
    bx,by — базовое ЭКРАННОЕ смещение слоя от центра (px) при камере в 0.
    Возвращает (px', py', scale_factor). При камере в 0 и roll=0 -> тождество (px',py',1).
    ЗЕРКАЛО дилиба (drive_layers): ролл вращает СПРОЕЦИРОВАННУЮ позицию вокруг центра кадра,
    а к повороту слоя прибавляется тот же угол. Формула обязана совпадать 1:1 — иначе живой
    драйв и запекание разъедутся (у дилиба ролл был, здесь его не было)."""
    d = z - cz
    if d < 1e-3:
        d = 1e-3                      # слой на/за камерой -> прижать (иначе деление на ~0)
    # мир X слоя back-project из base-экрана: X = bx*z/f ; экран' = f*(X-cx)/(z-cz)
    px = (bx * z - f * cx) / d
    py = (by * z - f * cy) / d
    if roll:
        rr = math.radians(roll)
        cs, sn = math.cos(rr), math.sin(rr)
        px, py = px * cs - py * sn, px * sn + py * cs
    return px, py, z / d              # масштаб = z/(z-cz) относительно базового


def camera_at(preset, s, intensity, f, hw):
    """Состояние камеры (cx,cy,cz) в МИРОВЫХ единицах на норм. времени s∈[0,1].
    cx подобран так, чтобы ПОЛНЫЙ размах панорамы давал ~2*a*0.9 half-canvas сдвига на опорной глубине
    Z_REF (экранный сдвиг = f*cx/Z_REF; чтобы = a*0.9*hw px, нужно cx = a*0.9*hw*Z_REF/f)."""
    a = max(0.0, intensity)
    pan = a * 0.9 * hw * Z_REF / f    # мировой cx, дающий ~a*0.9 half-canvas сдвига на Z_REF
    if preset == "parallax_pan":
        return (pan * (s - 0.5) * 2.0, 0.0, 0.0)
    if preset == "parallax_tilt":
        return (0.0, pan * (s - 0.5) * 2.0, 0.0)
    if preset == "push_in":
        return (0.0, 0.0, a * 0.6 * NEAR * s)          # камера к сцене -> ближние растут
    if preset == "pull_out":
        return (0.0, 0.0, -a * 0.9 * NEAR * s)
    if preset == "orbit":
        th = 2.0 * math.pi * s
        return (pan * math.sin(th), 0.0, a * 0.35 * NEAR * (1.0 - math.cos(th)))
    if preset == "float":                               # лёгкий «ручной» дрейф (детерминированный)
        return (pan * 0.4 * math.sin(2*math.pi*s*1.3),
                pan * 0.3 * math.sin(2*math.pi*s*0.9 + 1.1),
                a * 0.12 * NEAR * math.sin(2*math.pi*s*0.7))
    return (0.0, 0.0, 0.0)


PRESETS = ("parallax_pan", "parallax_tilt", "push_in", "pull_out", "orbit", "float")


def _interp_path(kfs, t):
    """Свободный путь камеры kfs=[{t,cx,cy,cz,roll?}] (t — timeline-µs, отсортирован) -> (cx,cy,cz,roll).
    Линейная интерполяция; за краями держим крайний ключ (камера стоит до первого / после последнего)."""
    def pose(k):
        return (k["cx"], k["cy"], k["cz"], k.get("roll", 0.0))
    if not kfs:
        return (0.0, 0.0, 0.0, 0.0)
    if t <= kfs[0]["t"]:
        return pose(kfs[0])
    if t >= kfs[-1]["t"]:
        return pose(kfs[-1])
    for i in range(1, len(kfs)):
        a, b = kfs[i - 1], kfs[i]
        if t <= b["t"]:
            u = (t - a["t"]) / max(1, b["t"] - a["t"])
            pa, pb = pose(a), pose(b)
            return tuple(pa[j] + (pb[j] - pa[j]) * u for j in range(4))
    return pose(kfs[-1])


def bake_path(cw, ch, layers, campath, want_scale=True, want_rot=False):
    """Как bake(), но камера задана СВОБОДНЫМ путём (ключи из превью плагина), а не пресетом.
    Та же пинхол-проекция (project) и те же единицы, что в превью (layerMatrix в ui.html) — превью==бейк.
      layers  — [{id, bx, by, base_scale, base_rot, z, samples}], samples=[(t_src_us, t_tl_us)]:
                t_src — источник-µs (куда писать ключ), t_tl — timeline-µs (где взять позу камеры).
      campath — [{t,cx,cy,cz}] по timeline-µs (мировые единицы камеры, как camera_at)."""
    f = FOCAL_FRAC * ch
    hw, hh = cw / 2.0, ch / 2.0
    campath = sorted(campath, key=lambda k: k["t"])
    out = {}
    for L in layers:
        xs, ys, ss, rs = [], [], [], []
        for t_src, t_tl in L["samples"]:
            cx, cy, cz, roll = _interp_path(campath, t_tl)
            px, py, sc = project(L["bx"], L["by"], L["z"], cx, cy, cz, f, roll)
            xs.append((t_src, max(-POS_CLAMP, min(POS_CLAMP, px / hw))))
            ys.append((t_src, max(-POS_CLAMP, min(POS_CLAMP, SIGN_Y * py / hh))))
            if want_scale:
                ss.append((t_src, L["base_scale"] * max(SCALE_CLAMP[0], min(SCALE_CLAMP[1], sc))))
            if want_rot or roll:
                rs.append((t_src, L["base_rot"] + roll))   # ролл камеры добавляется к повороту слоя (как в дилибе)
        out[L["id"]] = (xs, ys, ss if want_scale else None, rs or None)
    return out


def bake(cw, ch, layers, times, preset="parallax_pan", intensity=0.6, want_scale=True, want_rot=False):
    """Запечь камеру в ключи для набора слоёв.
      cw,ch    — размеры канваса.
      layers   — [{id, bx, by, base_scale, base_rot, z}] где bx,by — базовое ЭКРАННОЕ смещение (px),
                 base_scale/base_rot — текущие scale/rotation слоя, z — мировая глубина (depth_to_z).
      times    — [(t_us, s)] : t_us = микросекунды-источника ключа, s∈[0,1] норм. время камеры.
    Возвращает curves_by_target = {id: (xs, ys, ss, rs)} в ТОЧНО той форме, что ест write_live_curve/
    inject_curves: каждый из xs/ys/ss — список (t_us, value); rs = список или None."""
    f = FOCAL_FRAC * ch
    hw, hh = cw / 2.0, ch / 2.0
    out = {}
    for L in layers:
        xs, ys, ss, rs = [], [], [], []
        for t_us, s in times:
            cx, cy, cz = camera_at(preset, s, intensity, f, hw)
            px, py, sc = project(L["bx"], L["by"], L["z"], cx, cy, cz, f)
            xv = max(-POS_CLAMP, min(POS_CLAMP, px / hw))
            yv = max(-POS_CLAMP, min(POS_CLAMP, SIGN_Y * py / hh))
            xs.append((t_us, xv))
            ys.append((t_us, yv))
            if want_scale:
                k = max(SCALE_CLAMP[0], min(SCALE_CLAMP[1], sc))
                ss.append((t_us, L["base_scale"] * k))
            if want_rot:
                rs.append((t_us, L["base_rot"]))          # плоскую карточку не крутим (нет corner-pin)
        out[L["id"]] = (xs, ys, ss if want_scale else None, rs if want_rot else None)
    return out


def demo():
    """Самопроверка математики параллакса (без CapCut). Запуск: python camera3d.py"""
    cw, ch = 1080.0, 1920.0
    f = FOCAL_FRAC * ch
    # два слоя в одной экранной точке, но на разной глубине: ближний и дальний
    near = {"id": "n", "bx": 100.0, "by": 0.0, "base_scale": 1.0, "base_rot": 0.0, "z": depth_to_z(0.0)}
    far = {"id": "f", "bx": 100.0, "by": 0.0, "base_scale": 1.0, "base_rot": 0.0, "z": depth_to_z(1.0)}
    times = [(int(1e6 * s), s) for s in (0.0, 0.5, 1.0)]

    # --- ПАРАЛЛАКС-ПАН: ближний слой должен сдвигаться СИЛЬНЕЕ дальнего ---
    c = bake(cw, ch, [near, far], times, preset="parallax_pan", intensity=0.6)
    nx = [v for _, v in c["n"][0]]
    fx = [v for _, v in c["f"][0]]
    near_travel = abs(nx[-1] - nx[0])
    far_travel = abs(fx[-1] - fx[0])
    assert near_travel > far_travel > 0, f"параллакс сломан: near={near_travel} far={far_travel}"
    # при s=0 (камера в 0) -> тождество, x ~ base 100/(1080/2)
    assert abs(nx[1] - 100.0 / (cw / 2)) < 1e-6, f"нет тождества в центре: {nx[1]}"

    # --- PUSH-IN: ближний слой должен МАСШТАБИРОВАТЬСЯ сильнее дальнего ---
    c = bake(cw, ch, [near, far], times, preset="push_in", intensity=0.6)
    n_scale = c["n"][2][-1][1]
    f_scale = c["f"][2][-1][1]
    assert n_scale > f_scale > 1.0, f"push-in сломан: near_scale={n_scale} far_scale={f_scale}"

    # --- клипы позиции/масштаба работают ---
    c = bake(cw, ch, [{"id": "x", "bx": 0.0, "by": 0.0, "base_scale": 1.0, "base_rot": 0.0, "z": 0.05}],
             [(0, 0.0), (1, 1.0)], preset="push_in", intensity=1.0)
    assert c["x"][2][-1][1] <= SCALE_CLAMP[1] + 1e-9, "scale clamp не сработал"

    # --- СВОБОДНЫЙ ПУТЬ: камера едет вправо (cx: 0 -> +) — ближний слой параллаксит сильнее дальнего ---
    fpx = FOCAL_FRAC * ch
    pan_cx = 0.6 * 0.9 * (cw / 2) * Z_REF / fpx      # тот же масштаб размаха, что у пресета pan
    campath = [{"t": 0, "cx": 0.0, "cy": 0.0, "cz": 0.0},
               {"t": 1_000_000, "cx": pan_cx, "cy": 0.0, "cz": 0.0}]
    nL = {**near, "samples": [(0, 0), (1_000_000, 1_000_000)]}
    fL = {**far, "samples": [(0, 0), (1_000_000, 1_000_000)]}
    c = bake_path(cw, ch, [nL, fL], campath)
    n_tr = abs(c["n"][0][-1][1] - c["n"][0][0][1])
    f_tr = abs(c["f"][0][0][1] - c["f"][0][-1][1])
    assert n_tr > f_tr > 0, f"свободный путь сломан: near={n_tr} far={f_tr}"
    assert abs(c["n"][0][0][1] - 100.0 / (cw / 2)) < 1e-6, "нет тождества при камере в 0"
    # интерполяция в середине = половина хода
    mid = _interp_path(campath, 500_000)
    assert abs(mid[0] - pan_cx / 2) < 1e-6, f"интерп пути сломан: {mid}"

    # --- РОЛЛ: зеркало дилиба. Слой на +X при ролле 90° уезжает на +Y (экранные оси, до SIGN_Y),
    # масштаб не меняется, а к повороту слоя прибавляется угол камеры. Расхождение с дилибом
    # означало бы, что превью/бейк и живой драйв разъедутся — ловим здесь, а не глазами.
    px0, py0, sc0 = project(100.0, 0.0, Z_REF, 0, 0, 0, f, roll=0.0)
    px9, py9, sc9 = project(100.0, 0.0, Z_REF, 0, 0, 0, f, roll=90.0)
    assert abs(px0 - 100.0) < 1e-9 and abs(py0) < 1e-9, (px0, py0)
    assert abs(px9) < 1e-9 and abs(py9 - 100.0) < 1e-9, f"ролл не вращает позицию: {px9},{py9}"
    assert abs(sc9 - sc0) < 1e-12, "ролл не должен менять масштаб"
    rollpath = [{"t": 0, "cx": 0.0, "cy": 0.0, "cz": 0.0, "roll": 0.0},
                {"t": 1_000_000, "cx": 0.0, "cy": 0.0, "cz": 0.0, "roll": 30.0}]
    cr = bake_path(cw, ch, [{**near, "samples": [(0, 0), (1_000_000, 1_000_000)]}], rollpath)
    assert cr["n"][3] is not None and abs(cr["n"][3][-1][1] - 30.0) < 1e-9, "ролл не попал в поворот слоя"
    assert abs(_interp_path(rollpath, 500_000)[3] - 15.0) < 1e-9, "ролл не интерполируется"

    print("camera3d demo OK — параллакс (near>far travel), push-in (near>far scale), клипы, свободный путь, РОЛЛ")
    print(f"  parallax near_travel={near_travel:.4f} far_travel={far_travel:.4f}")
    print(f"  push-in  near_scale={n_scale:.3f} far_scale={f_scale:.3f}")


if __name__ == "__main__":
    demo()
