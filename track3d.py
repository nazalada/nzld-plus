"""3D-ТРЕКИНГ: решение камеры → плоскость из облака → гомографии на кадр.

Это ГОЛОВА конвейера. Хвост не трогаем: на выходе те же `homos` + `times`, что даёт
`ml_track.track_roi`, поэтому `capcut_track.compute_cornerpin_ml` и `live_cornerpin`
подхватывают результат как есть — углы, KFTypeCornerPin*, живая команда, без переоткрытия.

Зачем вообще: планарному треку нужны признаки НА САМОЙ поверхности, а на зелёном экране их
нет (замер: консенсус 28 %, бортик оказался зеркалом). Здесь движение берётся из всей комнаты,
а поверхность задаётся один раз — плоскостью из облака.

Порядок и почему такой:
  1. ТЕСТ ПАРАЛЛАКСА ПЕРЕД СОЛВЕРОМ. Камера-солвер требует реального смещения камеры; если она
     крутилась на месте, триангулировать нечего. Дешёвая проверка (LK, доли секунды) избавляет
     от 40 секунд счёта ради мусора. Замерено: у «ИСКАЖЕНИЯ» H/F ≈ 1.0 и камера не решается.
  2. COLMAP: кадры → SIFT → плотное сопоставление → инкрементальная реконструкция.
  3. ПЛОСКОСТЬ ТОЛЬКО RANSAC. МНК нельзя: он ловится на отражениях глянцевого бортика и ставит
     плоскость почти ребром (нормаль к лучу 84.9°), углы улетают на тысячи пикселей, а числа
     при этом выглядят правдоподобно.
  4. Гомография, наведённая плоскостью: H = K (R + t·nᵀ/d) K⁻¹ — из поз соседних кадров.

Соотношение сторон цели тут НЕ нужно: плоскость приходит из облака вместе с расстоянием.
Знать его требовалось варианту «квад + K», который мы не берём.
"""
import math
import os
import shutil
import time

import numpy as np

SOLVE_STEP = 2          # каждый 2-й кадр
SOLVE_MAX = 100         # и не больше сотни: замер — 97 кадров, плотное сопоставление, 38 с
PARALLAX_MIN = 0.55     # H/F выше этого = камера крутилась на месте, решать нечего
PLANE_THR = 0.01        # порог RANSAC — доля габарита набора точек


def available():
    try:
        import pycolmap  # noqa: F401
        return True
    except ImportError:
        return False


# ---------- 1. параллакс ----------

def parallax_ratio(path, t0_us=0, t1_us=None, log=print):
    """H/F: доля соответствий, которые объясняет ОДНА гомография, к тем, что объясняет
    фундаментальная матрица. ~1.0 = чистое вращение (для SfM вырождение), низкое = параллакс.
    Точки — обычный LK по углам Ши-Томази: тяжёлая модель тут не нужна."""
    import cv2

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if t1_us:
        n = min(n, int(t1_us / 1e6 * fps))
    cap.set(cv2.CAP_PROP_POS_MSEC, t0_us / 1000)
    ok, first = cap.read()
    if not ok:
        cap.release()
        raise ValueError(f"не смог открыть видео: {path}")
    g0 = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
    p0 = cv2.goodFeaturesToTrack(g0, 1200, 0.01, 8)
    ratios, prev, pts = [], g0, p0
    for i in range(1, n):
        ok, fr = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        pts, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, pts, None)
        keep = st.ravel() == 1
        pts, p0 = pts[keep], p0[keep]
        prev = g
        if len(pts) < 40:
            break
        if i % max(1, n // 4) == 0:                     # четыре замера по клипу
            a, b = p0.reshape(-1, 2), pts.reshape(-1, 2)
            _, mh = cv2.findHomography(a, b, cv2.RANSAC, 3.0)
            _, mf = cv2.findFundamentalMat(a, b, cv2.FM_RANSAC, 3.0, 0.99)
            if mh is not None and mf is not None and mf.sum() > 20:
                ratios.append(float(mh.sum()) / float(mf.sum()))
    cap.release()
    r = float(np.median(ratios)) if ratios else 1.0
    log(f"параллакс: H/F = {r:.2f} (замеров {len(ratios)}), порог {PARALLAX_MIN}")
    return r


# ---------- 2. решение камеры ----------

def solve_camera(path, work, t0_us=0, t1_us=None, step=SOLVE_STEP, maxn=SOLVE_MAX,
                 log=print, progress=None):
    """COLMAP по кадрам клипа. Возвращает (reconstruction, {индекс кадра: имя}, fps)."""
    import cv2
    import pycolmap

    if os.path.exists(work):
        shutil.rmtree(work)
    img = os.path.join(work, "img")
    os.makedirs(img)
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, t0_us / 1000)
    idx, i, t_last = [], 0, (t1_us if t1_us else 1e18)
    while len(idx) < maxn:
        t = cap.get(cv2.CAP_PROP_POS_MSEC) * 1000
        ok, fr = cap.read()
        if not ok or t > t_last:
            break
        if i % step == 0:
            cv2.imwrite(os.path.join(img, f"{i:05d}.jpg"), fr, [cv2.IMWRITE_JPEG_QUALITY, 95])
            idx.append((i, t))
        i += 1
    cap.release()
    if len(idx) < 8:
        raise ValueError("мало кадров для решения камеры")
    log(f"3D: кадров подано {len(idx)} (каждый {step}-й)")
    if progress:
        progress(20, "Признаки на кадрах…")
    db = os.path.join(work, "db.db")
    pycolmap.extract_features(db, img)
    if progress:
        progress(45, "Сопоставление кадров…")
    pycolmap.match_exhaustive(db)          # замер: плотное даёт 8.6 px против 11.4 у последовательного
    if progress:
        progress(65, "Решение камеры…")
    recs = pycolmap.incremental_mapping(db, img, os.path.join(work, "out"))
    if not recs:
        raise ValueError("камера не решилась: движок не собрал ни одной реконструкции")
    rec = recs[max(recs, key=lambda k: recs[k].num_reg_images())]
    log(f"3D: зарегистрировано {rec.num_reg_images()} из {len(idx)}, "
        f"облако {rec.num_points3D()} точек, репроекция "
        f"{rec.compute_mean_reprojection_error():.2f} px")
    return rec, dict(idx), fps


# ---------- 3. плоскость ----------

def plane_ransac(P, thr_frac=PLANE_THR, iters=2000, seed=0):
    """Плоскость по КОНСЕНСУСУ. МНК на этих точках ломается: отражения глянца уезжают вдоль
    луча (замер: разброс вдоль 2.79 против 0.95 поперёк) и тянут плоскость ребром."""
    rng = np.random.default_rng(seed)
    thr = thr_frac * float(np.linalg.norm(P.max(0) - P.min(0)))
    best, bestk = None, -1
    for _ in range(iters):
        s = P[rng.choice(len(P), 3, replace=False)]
        nrm = np.cross(s[1] - s[0], s[2] - s[0])
        ln = np.linalg.norm(nrm)
        if ln < 1e-12:
            continue
        nrm = nrm / ln
        k = int((np.abs(P @ nrm + (-nrm @ s[0])) < thr).sum())
        if k > bestk:
            best, bestk = (nrm, -nrm @ s[0]), k
    nrm, d = best
    inl = np.abs(P @ nrm + d) < thr
    c = P[inl].mean(0)
    _, _, vt = np.linalg.svd(P[inl] - c)                 # доводка ТОЛЬКО по инлаерам
    nrm = vt[-1] / np.linalg.norm(vt[-1])
    return nrm, float(-nrm @ c), int(inl.sum())


def _pose(im):
    p = im.cam_from_world()
    return p.rotation.matrix(), np.asarray(p.translation, float)


def plane_from_quad(rec, ref, quad_src, grow=1.0, log=print):
    """Плоскость цели по точкам облака СТРОГО ВНУТРИ обведённого квада.

    ⚠️ Квад НЕ раздувать. Замерено на стенде: `grow=1.6` набирает 673 точки, из них большинство
    — комната за ноутбуком, RANSAC честно находит консенсус... этой комнаты, и углы промахиваются
    на 252 px (при 1.25 — на 290). Строго внутри остаётся 23 точки, из них 21 в консенсусе, и
    ошибка падает до 6.6 px. Большинство здесь решает не в нашу пользу: расширять область —
    значит подмешивать ЧУЖУЮ плоскость, которая ещё и многочисленнее.
    Мало точек внутри — честно отказываемся, а не добираем из фона."""
    import cv2

    q = np.array(quad_src, np.float64)
    if grow != 1.0:
        cen = q.mean(0)
        q = np.array([cen + (p - cen) * grow for p in q])
    poly = np.array([q[0], q[1], q[3], q[2]], np.int32)
    ids = []
    for p2 in ref.points2D:
        if not p2.has_point3D():
            continue
        if cv2.pointPolygonTest(poly, (float(p2.xy[0]), float(p2.xy[1])), False) >= 0:
            ids.append(p2.point3D_id)
    if len(ids) < 6:
        raise ValueError(
            f"внутри обведённой области всего {len(ids)} точек облака — плоскость строить не из "
            "чего. Захвати квадом кромку цели, где есть фактура (метки, край экрана, рамку), "
            "но НЕ фон вокруг: точки фона лежат в другой плоскости и уводят решение.")
    P = np.array([rec.points3D[i].xyz for i in ids])
    nrm, d, ninl = plane_ransac(P)
    log(f"3D: плоскость по {ninl} точкам из {len(P)} внутри квада (RANSAC)")
    return nrm, d, len(P), ninl


# ---------- 4. репер плоскости: она ЯКОРЬ, а не рамка для натяжки ----------
#
# Плоскость задаёт систему координат, к которой привязывается ЛЮБОЙ объект — в том числе вне
# обведённой области и НАД плоскостью (текст, висящий над коробкой на полу). Обведённый квад
# служит только тем, что задаёт начало и масштаб репера.
#
# Единицы: ex — сторона квада UL→UR, ey — сторона UL→DL, ez — нормаль ДЛИНОЙ |ex|. То есть
# z = 0.5 значит «на полширины квада над плоскостью», и репер не зависит от масштаба
# реконструкции (он у SfM произвольный).
#
# Потолок, зафиксировать честно: CapCut рисует ПЛОСКИЙ слой с corner-pin, поэтому «3D-объект» —
# это плоская карточка, ориентированная в пространстве. Для текста над коробкой хватает, для
# объёма нет. Перекрытий тоже нет: объект за коробкой не спрячется.

def ray_to_plane(K, R, t, px, nrm, d):
    """Пиксель опорного кадра → точка на плоскости (мир)."""
    C = -R.T @ t
    dirw = R.T @ (np.linalg.inv(K) @ np.array([px[0], px[1], 1.0]))
    den = float(nrm @ dirw)
    if abs(den) < 1e-12:
        raise ValueError("луч параллелен плоскости")
    return C + dirw * (-(float(nrm @ C) + d) / den)


def plane_basis(K, ref_pose, nrm, d, quad_src):
    """Репер по обведённому кваду: (O, ex, ey, ez). Начало — центр квада на плоскости."""
    R, t = ref_pose
    Q = np.array([ray_to_plane(K, R, t, p, nrm, d) for p in quad_src])   # UL UR DL DR
    O = Q.mean(0)
    ex = ((Q[1] - Q[0]) + (Q[3] - Q[2])) / 2.0                            # «вправо» по кваду
    ey = ((Q[2] - Q[0]) + (Q[3] - Q[1])) / 2.0                            # «вниз» по кваду
    exn = ex / np.linalg.norm(ex)
    ey = ey - (ey @ exn) * exn                                            # ортогонализуем
    n = nrm / np.linalg.norm(nrm)
    # ВЫСОТА СЧИТАЕТСЯ В СТОРОНУ НАБЛЮДАТЕЛЯ. Правило правой руки от (ex, ey) даёт нормаль ЗА
    # плоскость, и объект с z>0 уезжал БЕХИНД цели — на кадре карточка «над экраном» оказывалась
    # за ним. Для пола, снятого сверху, «к камере» и есть «вверх», а это и нужен сценарий
    # «текст над коробкой».
    if n @ (-R.T @ t - O) < 0:
        n = -n
    return O, ex, ey, n * float(np.linalg.norm(ex))


def resolve_obj(lens, obj):
    """Дополнить объект дефолтами и посчитать высоту, если её просили автоматом (h=0).
    lens — длины осей репера (|ex|, |ey|); в плоском предпросмотре это стороны квада.

    Прямоугольник объекта физически равен w·|ex| × h·|ey|, а corner-pin натягивает на него
    КОРОБКУ слоя. Совпали пропорции — картинка не искажена, разошлись — плющит ровно на
    отношение. Замер 2026-08-08: коробка 3.06:1 на объекте 4.53:1 (w=0.6 h=0.2) дала
    читаемый, но заметно сплюснутый текст. Поэтому высоту не крутят вслепую, а выводят.
    """
    o = {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0, "h": 1.0, **(obj or {})}
    if not o["h"] and o.get("aspect"):
        lx, ly = (float(v) for v in lens)
        o["h"] = o["w"] * lx / (ly * float(o["aspect"]))
    elif not o["h"]:
        o["h"] = 1.0
    return o


def object_corners(basis, x=0.0, y=0.0, z=0.0, w=1.0, h=1.0):
    """Углы объекта в мире: центр (x,y) в плоскости, высота z по нормали, размер (w,h).
    Всё в долях квада, поэтому (0,0,0,1,1) — это ровно обведённый квад."""
    O, ex, ey, ez = basis
    out = []
    for sy in (-0.5, 0.5):
        for sx in (-0.5, 0.5):
            out.append(O + ex * (x + sx * w) + ey * (y + sy * h) + ez * z)
    return np.array([out[0], out[1], out[2], out[3]])                     # UL UR DL DR


def project_points(K, pose, pts3d):
    """Мировые точки → пиксели кадра. Обычный пинхол: x = K (R X + t)."""
    R, t = pose
    pc = (R @ np.asarray(pts3d, float).T).T + t
    if (pc[:, 2] <= 0).any():
        raise ValueError("точка за камерой")
    return (K @ (pc / pc[:, 2:3]).T).T[:, :2]


# ---------- 5. гомографии ----------

def plane_homographies(rec, ref, K, nrm, d, frame_us, poses=None, log=print):
    """H_t: опорный кадр → кадр t для точек НА ПЛОСКОСТИ. Это ровно то, что ждёт
    compute_cornerpin_ml, поэтому дальше идёт готовый хвост."""
    R1, t1 = _pose(ref)
    n1 = R1 @ nrm                       # плоскость в системе опорной камеры
    d1 = float(n1 @ t1 - d)
    if abs(d1) < 1e-9:
        raise ValueError("плоскость проходит через центр камеры — решение вырождено")
    Kinv = np.linalg.inv(K)
    homos, times = [], []
    for im in sorted(rec.images.values(), key=lambda i: i.name):
        fi = int(os.path.splitext(im.name)[0])
        if fi not in frame_us:
            continue
        R2, t2 = (poses or {}).get(im.name, _pose(im))   # доведённая поза, если она есть
        R = R2 @ R1.T
        t = t2 - R @ t1
        H = K @ (R + np.outer(t, n1) / d1) @ Kinv
        if abs(H[2, 2]) < 1e-12:
            continue
        homos.append(H / H[2, 2])
        times.append(float(frame_us[fi]))
    order = np.argsort(times)
    log(f"3D: гомографий {len(homos)} на диапазоне "
        f"{times[order[0]] / 1e6:.2f}…{times[order[-1]] / 1e6:.2f} с")
    return [homos[i] for i in order], [times[i] for i in order]


# ---------- 6. доводка ПОЗЫ по кадру (уровень 3) ----------
#
# Решение камеры даёт устойчивость, но упирается в свой потолок (замер: даже углы,
# триангулированные по всем наблюдениям, дают 4.6–5.9 px). Пиксельную точность добирают
# выравниванием ПО САМОМУ КАДРУ — ECC по градиентам изображения, без всякого «найди зелёное»:
# детектор цвета остаётся эталоном и в конвейер не входит, иначе мерить будет нечем.
#
# ⚠️ Уточняем ПОЗУ, а не только целевой квад. Иначе выигрыш достался бы натянутому слою, а
# объект НАД плоскостью остался бы с прежней ошибкой. Из уточнённой позы проецируется всё.
#
# ⚠️ Доводка ОБЯЗАНА уметь отказаться: цель перекрыта рукой или сходимость плохая — берём позу
# из решения и идём дальше. Это сочетание и даёт устойчивость плюс точность.

ECC_MIN_CC = 0.72       # ниже этой корреляции доводке не верим
ECC_MAX_SHIFT = 60.0    # px: сдвиг угла больше этого = не доводка, а срыв
ECC_PAD = 1.0           # шаблон = сам квад. Шире брать нельзя: за кромкой глянцевый
                        # бортик, а он ЗЕРКАЛО — его движение не движение плоскости.


def refine_poses(path, rec, ref, K, nrm, d, frame_us, quad_src, log=print, progress=None):
    """ECC-доводка позы каждого кадра. Возвращает {имя кадра: (R, t)} и статистику отказов."""
    import cv2

    R1, t1 = _pose(ref)
    n1 = R1 @ nrm
    d1 = float(n1 @ t1 - d)
    Kinv = np.linalg.inv(K)

    # шаблон — окрестность квада на опорном кадре (сам кадр, никаких масок по цвету)
    q = np.array(quad_src, np.float64)
    cen = q.mean(0)
    pad = np.array([cen + (p - cen) * ECC_PAD for p in q])
    x0, y0 = np.floor(pad.min(0)).astype(int)
    x1, y1 = np.ceil(pad.max(0)).astype(int)
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    need = {int(os.path.splitext(im.name)[0]): im for im in rec.images.values()
            if int(os.path.splitext(im.name)[0]) in frame_us}
    frames = {}
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i in need:
            frames[i] = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        i += 1
    cap.release()
    refi = int(os.path.splitext(ref.name)[0])
    H, W = frames[refi].shape
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    tmpl = frames[refi][y0:y1, x0:x1].astype(np.float32) / 255.0
    T = np.array([[1, 0, x0], [0, 1, y0], [0, 0, 1]], np.float64)     # кроп → опорный кадр

    # 3D-точки плоскости под PnP: сетка внутри квада, лучом на плоскость (точно, без шума)
    gx, gy = np.meshgrid(np.linspace(0.1, 0.9, 6), np.linspace(0.1, 0.9, 6))
    uv = np.stack([gx.ravel(), gy.ravel()], 1)
    M = cv2.getPerspectiveTransform(np.array([[0, 0], [1, 0], [0, 1], [1, 1]], np.float32),
                                    np.array(quad_src, np.float32))
    grid2d = cv2.perspectiveTransform(uv.reshape(-1, 1, 2).astype(np.float32), M).reshape(-1, 2)
    grid3d = np.array([ray_to_plane(K, R1, t1, p, nrm, d) for p in grid2d])

    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
    poses, refined, refused = {}, 0, 0
    for k, (fi, im) in enumerate(sorted(need.items())):
        R2, t2 = _pose(im)
        poses[im.name] = (R2, t2)
        if fi == refi:
            continue
        R = R2 @ R1.T
        t = t2 - R @ t1
        H0 = K @ (R + np.outer(t, n1) / d1) @ Kinv
        Hc = (H0 @ T) / (H0 @ T)[2, 2]
        try:
            _, Wm = cv2.findTransformECC(tmpl, frames[fi].astype(np.float32) / 255.0,
                                         Hc.astype(np.float32), cv2.MOTION_HOMOGRAPHY, crit,
                                         None, 5)
        except cv2.error:
            refused += 1
            continue
        Hr = np.array(Wm, np.float64) @ np.linalg.inv(T)
        Hr /= Hr[2, 2]
        before = cv2.perspectiveTransform(q.reshape(-1, 1, 2), H0).reshape(-1, 2)
        after = cv2.perspectiveTransform(q.reshape(-1, 1, 2), Hr).reshape(-1, 2)
        if np.linalg.norm(after - before, axis=1).max() > ECC_MAX_SHIFT:
            refused += 1
            continue
        pts2 = cv2.perspectiveTransform(grid2d.reshape(-1, 1, 2).astype(np.float64), Hr).reshape(-1, 2)
        ok, rv, tv = cv2.solvePnP(grid3d.astype(np.float64), pts2.astype(np.float64), K, None,
                                  cv2.Rodrigues(R2)[0].copy(), t2.reshape(3, 1).copy(),
                                  useExtrinsicGuess=True, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            refused += 1
            continue
        Rn = cv2.Rodrigues(rv)[0]
        tn = tv.ravel()
        prj = (K @ ((Rn @ grid3d.T).T + tn).T).T
        rms = float(np.sqrt((((prj[:, :2] / prj[:, 2:3]) - pts2) ** 2).sum(1).mean()))
        if rms > 2.0:                       # PnP не лёг на доведённую гомографию — не верим
            refused += 1
            continue
        poses[im.name] = (Rn, tn)
        refined += 1
        if progress and k % 12 == 0:
            progress(80 + int(12 * k / max(1, len(need))), "Доводка по кадру…")
    log(f"3D: доводка позы по кадру — уточнено {refined}, отказов {refused} из {len(need) - 1}")
    return poses, refined, refused


# ---------- всё вместе ----------

def prepare_plane(path, quad_src, work, t0_us=0, t1_us=None, ref_us=None, log=print, progress=None):
    """Решить камеру и плоскость ОДИН раз и вернуть состояние для живого предпросмотра.
    Дальше `object_at` пересчитывает объект на любом кадре за микросекунды — крутилки живые."""
    t0 = time.time()
    if progress:
        progress(5, "Проверка параллакса…")
    r = parallax_ratio(path, t0_us, t1_us, log=log)
    if r > PARALLAX_MIN:
        raise ValueError(f"камера почти не смещается (H/F={r:.2f}) — 3D-трекинг тут не решается")
    rec, frame_us, fps = solve_camera(path, work, t0_us, t1_us, log=log, progress=progress)
    if progress:
        progress(80, "Плоскость…")
    ref = min((im for im in rec.images.values() if int(os.path.splitext(im.name)[0]) in frame_us),
              key=lambda im: abs(frame_us[int(os.path.splitext(im.name)[0])] - (ref_us or 0)))
    got = frame_us[int(os.path.splitext(ref.name)[0])]
    if abs(got - (ref_us or 0)) > 0.25e6:
        raise ValueError(f"кадр, на котором обведена плоскость, не попал в решение камеры "
                         f"(ближайший на {got / 1e6:.2f} с) — обведи заново на решённом кадре")
    K = rec.cameras[ref.camera_id].calibration_matrix()
    nrm, d, npts, ninl = plane_from_quad(rec, ref, quad_src, log=log)
    poses, nref, nrefused = refine_poses(path, rec, ref, K, nrm, d, frame_us, quad_src,
                                         log=log, progress=progress)
    st = {"rec": rec, "ref": ref, "K": K, "nrm": nrm, "d": d, "frame_us": frame_us,
          "poses": poses, "basis": plane_basis(K, _pose(ref), nrm, d, quad_src),
          "ref_src_us": float(got), "quad_src": quad_src,
          "meta": {"registered": rec.num_reg_images(), "cloud": rec.num_points3D(),
                   "plane_points": npts, "plane_inliers": ninl, "parallax": round(r, 2),
                   "refined": nref, "refused": nrefused, "seconds": round(time.time() - t0, 1)}}
    log(f"3D: предпросмотр готов за {st['meta']['seconds']} с")
    return st


def object_at(st, obj, t_src):
    """Объект в репере → (гомография опорный→кадр t, квад объекта в опорном кадре).
    Микросекунды: тяжёлое (солв, плоскость, доводка) уже посчитано в prepare_plane."""
    o = resolve_obj([np.linalg.norm(v) for v in st["basis"][1:3]], obj)
    corners = object_corners(st["basis"], o["x"], o["y"], o["z"], o["w"], o["h"])
    quad_obj = project_points(st["K"], _pose(st["ref"]), corners)
    d_obj = st["d"] - o["z"] * float(np.linalg.norm(st["basis"][1]))
    # кадр, ближайший к запрошенному времени
    fi = min(st["frame_us"], key=lambda k: abs(st["frame_us"][k] - t_src))
    im = next(x for x in st["rec"].images.values() if int(os.path.splitext(x.name)[0]) == fi)
    homos, _ = plane_homographies(st["rec"], st["ref"], st["K"], st["nrm"], d_obj,
                                  {fi: st["frame_us"][fi]}, poses=st["poses"], log=lambda *_: None)
    if not homos:
        raise ValueError("кадр не решён — подвинь плейхед туда, где камера решена")
    return homos[0], quad_obj


def solve_quad(path, quad_src, work, t0_us=0, t1_us=None, ref_us=None, obj=None,
               log=print, progress=None):
    """Голова целиком: клип + обведённый квад (px ИСХОДНИКА) → (homos, times, quad_ref, meta).

    obj — объект в репере плоскости: {x, y, z, w, h} в долях квада. None или всё по умолчанию
    (0,0,0,1,1) = сам квад, то есть сегодняшнее поведение один в один.

    Объект на высоте z лежит в ПАРАЛЛЕЛЬНОЙ плоскости, а её гомография — такая же по форме,
    просто со сдвинутым расстоянием. Поэтому и объект над плоскостью, и объект вне квада едут
    через тот же готовый хвост, без единой правки в нём."""
    t0 = time.time()
    if progress:
        progress(5, "Проверка параллакса…")
    r = parallax_ratio(path, t0_us, t1_us, log=log)
    if r > PARALLAX_MIN:
        raise ValueError(
            f"камера почти не смещается (H/F={r:.2f}) — 3D-трекинг тут не решается: "
            "нечего триангулировать. Так же ведёт себя AE. Снимай с проходом вбок или "
            "гони обычный планарный трек по размеченной поверхности.")
    rec, frame_us, fps = solve_camera(path, work, t0_us, t1_us, log=log, progress=progress)
    if progress:
        progress(80, "Плоскость и углы…")
    # ОПОРНЫЙ КАДР ДОЛЖЕН БЫТЬ ТОТ, НА КОТОРОМ РИСОВАЛСЯ КВАД. Квад — пиксели конкретного кадра;
    # взяли другой — и он показывает уже не на цель. Берём ближайший зарегистрированный и
    # проверяем, что он рядом: иначе молча считаем плоскость по чужому месту (замер: в кваде
    # осталась 1 точка вместо 20, и это выглядело как «мало фактуры»).
    ref = min((im for im in rec.images.values() if int(os.path.splitext(im.name)[0]) in frame_us),
              key=lambda im: abs(frame_us[int(os.path.splitext(im.name)[0])] - (ref_us or 0)))
    got_us = frame_us[int(os.path.splitext(ref.name)[0])]
    off = abs(got_us - (ref_us or 0))
    log(f"3D: опорный кадр {ref.name} на {got_us / 1e6:.2f} с "
        f"(просили {(ref_us or 0) / 1e6:.2f} с, промах {off / 1e6:.2f} с)")
    if off > 0.25e6:
        raise ValueError(
            f"кадр, на котором обведена плоскость ({(ref_us or 0) / 1e6:.2f} с), не попал в решение "
            f"камеры — ближайший решённый на {got_us / 1e6:.2f} с. Квад показывает не туда. "
            "Поставь плейхед на кадр, где цель видна целиком, и обведи заново.")
    K = rec.cameras[ref.camera_id].calibration_matrix()
    nrm, d, npts, ninl = plane_from_quad(rec, ref, quad_src, log=log)

    basis = plane_basis(K, _pose(ref), nrm, d, quad_src)
    o = resolve_obj([np.linalg.norm(v) for v in basis[1:3]], obj)
    corners = object_corners(basis, o["x"], o["y"], o["z"], o["w"], o["h"])
    quad_ref = project_points(K, _pose(ref), corners)
    # объект висит на высоте z ⇒ его собственная плоскость параллельна нашей и сдвинута
    d_obj = d - o["z"] * float(np.linalg.norm(basis[1]))
    if any(abs(o[k] - v) > 1e-9 for k, v in (("x", 0), ("y", 0), ("z", 0), ("w", 1), ("h", 1))):
        log(f"3D: объект в репере x={o['x']:+.2f} y={o['y']:+.2f} z={o['z']:+.2f} "
            f"размер {o['w']:.2f}×{o['h']:.2f} (в долях квада)")
    poses, nref, nrefused = refine_poses(path, rec, ref, K, nrm, d, frame_us, quad_src,
                                         log=log, progress=progress)
    homos, times = plane_homographies(rec, ref, K, nrm, d_obj, frame_us, poses=poses, log=log)
    meta = {"reason": "3D: решение камеры", "covered": 1.0, "frames": len(homos),
            "homos": [h.tolist() for h in homos], "times": times,
            "registered": rec.num_reg_images(), "cloud": rec.num_points3D(),
            "reproj": round(rec.compute_mean_reprojection_error(), 3),
            "plane_points": npts, "plane_inliers": ninl, "parallax": round(r, 2),
            "obj": o, "refined": nref, "refused": nrefused, "ref_frame_us": float(frame_us[int(os.path.splitext(ref.name)[0])]),
            "seconds": round(time.time() - t0, 1)}
    log(f"3D: голова готова за {meta['seconds']} с")
    return homos, times, quad_ref, meta
