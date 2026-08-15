"""ML-трекинг через CoTracker3 (offline). Сеем сетку точек в рамке, трекаем их по
всему клипу (forward+backward от выбранного кадра), затем на каждом кадре подгоняем
устойчивое similarity-преобразование -> центр объекта, масштаб, поворот.

Возвращает то же, что и CSRT-путь по смыслу, но с настоящим масштабом и поворотом.
"""
import os
import sys
import threading
import time

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # если какой-то op не на MPS


def _mmss(s):
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def _draw_bar(frac, elapsed, eta, label="Трекинг"):
    filled = int(frac * 26)
    bar = "█" * filled + "░" * (26 - filled)
    sys.stderr.write(f"\r  {label} [{bar}] {int(frac * 100):3d}%  прошло {_mmss(elapsed)}  "
                     f"осталось ~{_mmss(eta)}   ")
    sys.stderr.flush()
    try:  # прогресс в оверлей внутри CapCut (ve_hook.dylib читает этот файл)
        pct = int(frac * 100)
        with open(os.path.expanduser("~/Movies/CapCut/User Data/ve_progress.txt"), "w") as _pf:
            _pf.write(f"{pct} {label}  {pct}%  осталось ~{_mmss(eta)}")
    except Exception:
        pass


class _Progress:
    """Прогресс-бар по времени (для одного большого вызова инференса, где нет %):
    крутится в фоне, показывает прошло/примерный остаток по оценке est_seconds."""

    def __init__(self, est_seconds, label="Трекинг"):
        self.t0 = time.time()
        self.est = max(est_seconds, 1.0)
        self.label = label
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import math
        while not self._stop.is_set():
            el = time.time() - self.t0
            r = el / self.est
            # не флэтлайним на 97%: 0..85% линейно за est, дальше медленно ползём к 99%
            frac = 0.85 * r if r < 1.0 else 0.85 + 0.14 * (1.0 - math.exp(-(r - 1.0)))
            _draw_bar(frac, el, max(0.0, self.est - el), self.label)
            self._stop.wait(0.4)

    def __enter__(self):
        self._th.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._th.join(timeout=1)
        _draw_bar(1.0, time.time() - self.t0, 0, self.label)
        sys.stderr.write("  готово\n")
        sys.stderr.flush()

# Уровень жёсткости трекинга -> (track_size, grid, max_frames). Выше = точнее/плотнее, медленнее.
_LEVELS = {
    1: (512, 5, 200),
    2: (640, 6, 240),
    3: (768, 7, 300),   # по умолчанию
    4: (896, 8, 340),
    5: (1024, 9, 400),  # макс: выше разрешение, больше точек, каждый кадр
}
_MODEL = None


def available():
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _load():
    global _MODEL
    if _MODEL is None:
        import torch
        _MODEL = torch.hub.load("facebookresearch/co-tracker", "cotracker3_offline")
        _MODEL = _MODEL.to(_device()).eval()
    return _MODEL


def _read_frames(cv2, path, t0_us, t1_us, track_size=None):
    """Читает кадры окна. track_size задан -> кадр уменьшается СРАЗУ при чтении.
    Это критично для памяти: раньше весь список держался в ИСХОДНОМ разрешении (для 300 кадров
    вертикального 1080x1920 это ~1.9 ГБ) и только потом уменьшался — то есть полноразмерные кадры
    и их уменьшенные копии жили одновременно. На 24 ГБ машине со занятым свопом это выбивало
    систему в page-out и заикался звук/видео в фоне.
    -> (frames, times, (src_w, src_h), dn), где dn — применённый коэффициент уменьшения."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"не смог открыть видео: {path}")
    cap.set(cv2.CAP_PROP_POS_MSEC, t0_us / 1000)
    frames, times = [], []
    src_w = src_h = 0
    dn = 1.0
    while True:
        t = cap.get(cv2.CAP_PROP_POS_MSEC) * 1000  # время кадра, который СЕЙЧАС прочитаем (не следующего!)
        ok, fr = cap.read()
        if not ok:
            break
        if t > t1_us + 1e6 / 60:
            break
        if not src_w:  # размеры исходника — с ПЕРВОГО кадра, до всякого ресайза
            src_h, src_w = fr.shape[:2]
            if track_size:
                dn = min(1.0, track_size / max(src_w, src_h))
        if dn < 1.0:
            fr = cv2.resize(fr, (int(src_w * dn), int(src_h * dn)))
        frames.append(fr)
        times.append(t)
    cap.release()
    return frames, times, (src_w, src_h), dn


OUTLIER_RATE = 0.35    # точка вылетает из консенсуса чаще этой доли кадров -> она не на плоскости
HOMO_GAIN = 0.80       # гомография должна выиграть у аффина хотя бы 20% RMS, иначе её 8 DOF гуляют


def track_roi(path, t0_us, t1_us, query_us, roi, level=3, quad=None, log=print):
    """roi в ИСХОДНЫХ px (x,y,w,h) на кадре query_us. level 1..5 — жёсткость трекинга
    (выше = точнее/плотнее, медленнее).
    quad: 4 угла поверхности [(x,y)]*4 в ИСХОДНЫХ px, порядок UL,UR,DL,DR — если задан, точки
    сеются внутри НЕГО, а не внутри прямоугольника. Прямоугольник — частный случай квада, поэтому
    у пользователя больше нет обязанности вписывать наклонённый экран в рамку по осям.
    -> (list[(t_us, cx, cy, scale, rot_deg)], meta)."""
    import cv2
    import numpy as np
    import torch

    track_size, grid, max_frames = _LEVELS.get(level, _LEVELS[3])
    frames, times, (src_w, src_h), dn = _read_frames(cv2, path, t0_us, t1_us, track_size)
    if len(frames) < 2:
        raise ValueError("мало кадров для трекинга")
    if len(frames) > max_frames:  # равномерно проредить
        step = len(frames) / max_frames
        keep = sorted(set(int(i * step) for i in range(max_frames)))
        frames = [frames[i] for i in keep]
        times = [times[i] for i in keep]

    # Кадры уже уменьшены при чтении (src_w/src_h/dn пришли оттуда же) — второго прохода
    # и второй копии в памяти больше нет.
    small = frames

    qf = min(range(len(times)), key=lambda i: abs(times[i] - query_us))

    x, y, w, h = (v * dn for v in roi)
    # Сетка засева — билинейно ВНУТРИ четырёхугольника (отступ 15..85%, как было у рамки).
    # Для прямоугольника это в точности прежняя сетка, поэтому старый путь не меняется.
    if quad is not None:
        A, B, C, D = (np.array(p, np.float64) * dn for p in quad)      # UL, UR, DL, DR
    else:
        A = np.array([x, y]); B = np.array([x + w, y])
        C = np.array([x, y + h]); D = np.array([x + w, y + h])
    uu = np.linspace(0.15, 0.85, grid)
    pts = np.array([[(1 - u) * (1 - v) * A + u * (1 - v) * B + (1 - u) * v * C + u * v * D]
                    for v in uu for u in uu], np.float32).reshape(-1, 2)
    queries = np.hstack([np.full((len(pts), 1), qf, np.float32), pts])  # (N,3): t,x,y

    dev = _device()
    nfr = len(frames)   # запоминаем ДО del frames (ниже он ещё нужен для оценки времени)
    log(f"ML-трек: {nfr} кадров, {len(pts)} точек, устройство {dev}")
    # Не занимать ВСЕ ядра: инференс раскручивался до ~400% CPU и вместе со скачком памяти
    # ронял фоновое воспроизведение (звук/видео заикались). Треть ядер хватает, время почти то же.
    try:
        torch.set_num_threads(max(2, (os.cpu_count() or 8) // 3))
    except Exception:
        pass
    # uint8 -> на устройство -> и только ТАМ во float: раньше float32-копия (×4 к размеру)
    # создавалась в RAM целиком и лишь потом уезжала на GPU.
    stacked = np.stack(small)
    del small, frames                       # полноразмерных копий больше не держим
    video = torch.from_numpy(stacked).permute(0, 3, 1, 2)[None].to(dev).float()
    del stacked
    q = torch.from_numpy(queries)[None].to(dev)
    model = _load()
    est = nfr * 0.06 * (track_size / 768)  # грубая оценка времени инференса, с (замерено)
    with torch.no_grad(), _Progress(est, "Трекинг"):
        tracks, vis = model(video, queries=q)  # [1,T,N,2], [1,T,N]
    tracks = tracks[0].cpu().numpy()
    vis = vis[0].cpu().numpy() > 0.5
    del video, q

    R = tracks[qf]
    cx0, cy0 = x + w / 2, y + h / 2
    inv = 1.0 / dn
    out = []
    # ПЛАНАРНЫЙ ТРЕК: помимо сведённых (центр/масштаб/поворот) копим ГОМОГРАФИЮ кадра —
    # только она описывает перспективу (наклон плоскости), а similarity её теряет.
    # Матрицы сразу переводим в ИСХОДНЫЕ пиксели: трекали на уменьшенных кадрах (масштаб dn),
    # а потребителю нужны координаты видео. H_src = S⁻¹ · H_small · S, где S = diag(dn, dn, 1).
    homos = []
    # КАЧЕСТВО ПЛОСКОСТИ покадрово: (инлаеров, RMS репроекции в px трекера). Раньше маска
    # инлаеров выбрасывалась, и «подогналось к не-плоскости» было неотличимо от честного трека:
    # RANSAC всегда что-нибудь вернёт. -1 в RMS = плоскость потеряна (кадр держит прошлую матрицу).
    quality = []
    S = np.array([[dn, 0, 0], [0, dn, 0], [0, 0, 1]], np.float64)
    Sinv = np.array([[inv, 0, 0], [0, inv, 0], [0, 0, 1]], np.float64)

    def _rms(M, src, dst):
        prj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), M).reshape(-1, 2)
        return float(np.sqrt(((prj - dst) ** 2).sum(axis=1).mean()))

    # ОТСЕВ НЕПЛОСКИХ ТОЧЕК — дешёвая замена сплайн-регионам и слоям-перекрытиям Mocha.
    # Первый проход НИЧЕГО не пишет, только считает, кто СИСТЕМАТИЧЕСКИ вылетает из консенсуса:
    # такая точка сидит на руке, на объёме или на блике, а не на плоскости. Во втором проходе её
    # нет вовсе, поэтому она не тянет гомографию и не портит соседние кадры.
    seen = np.zeros(len(pts), np.int32)
    outl = np.zeros(len(pts), np.int32)
    for t in range(len(times)):
        m = vis[t]
        if m.sum() < 4:
            continue
        H, mask = cv2.findHomography(R[m], tracks[t][m], cv2.RANSAC, 3.0)
        if H is None:
            continue
        idx = np.flatnonzero(m)
        seen[idx] += 1
        outl[idx[~mask.ravel().astype(bool)]] += 1
    good = ~((seen > 0) & (outl > OUTLIER_RATE * np.maximum(seen, 1)))
    systematic = int(len(pts) - good.sum())   # сколько точек систематически вне консенсуса
    if good.sum() < 8:      # отсеяли почти всё -> плоскости тут нет вовсе, считаем по всем точкам
        # Без этой строки отчёт ВРАЛ: «отсеяно 0» читалось как «плохих точек нет», хотя на деле
        # плохими оказались почти все, и предохранитель вернул их обратно.
        log(f"⚠️  вне консенсуса СИСТЕМАТИЧЕСКИ {systematic} из {len(pts)} точек — отсев отключён "
            f"(осталось бы меньше 8). Плоскости тут почти не на чем держаться.")
        good = np.ones(len(pts), bool)
    dropped = int(len(pts) - good.sum())
    if dropped:
        log(f"отсев неплоских точек: {dropped} из {len(pts)} (перекрытие/объём)")

    naff = 0
    for t in range(len(times)):
        m = vis[t] & good
        H = mask = None
        if m.sum() >= 4:   # гомография требует минимум 4 соответствия
            H, mask = cv2.findHomography(R[m], tracks[t][m], cv2.RANSAC, 3.0)
        if H is None:
            homos.append(homos[-1] if homos else np.eye(3))
            quality.append((0, -1.0))
            continue
        keep = mask.ravel().astype(bool)
        src, dst = R[m][keep], tracks[t][m][keep]
        if not len(src):
            homos.append(homos[-1] if homos else np.eye(3))
            quality.append((0, -1.0))
            continue
        rms = _rms(H, src, dst)
        # ИЕРАРХИЯ МОДЕЛЕЙ — то же, что галки translation/scale/rotation/shear/perspective в Mocha.
        # На фронтальном кадре без реальной перспективы 8 степеней свободы недоопределены, и углы
        # гуляют. Гомографию берём, только если она заметно точнее аффина НА ТЕХ ЖЕ точках.
        Ma, _ = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
        if Ma is not None:
            Ma3 = np.vstack([Ma, [0.0, 0.0, 1.0]])
            ra = _rms(Ma3, src, dst)
            if rms > HOMO_GAIN * ra:
                H, rms, naff = Ma3, ra, naff + 1
        quality.append((int(keep.sum()), rms))
        homos.append(Sinv @ H @ S)
    for t in range(len(times)):
        m = vis[t]
        if m.sum() >= 1:
            # ЦЕНТР — по МЕДИАНЕ смещений видимых точек (устойчиво к «уплывшим» точкам на
            # резком движении; аффинная трансляция там улетает). Масштаб/поворот — из RANSAC.
            med = np.median(tracks[t][m] - R[m], axis=0)
            cx, cy = cx0 + med[0], cy0 + med[1]
            sc, rot = 1.0, 0.0
            if m.sum() >= 3:
                M, _ = cv2.estimateAffinePartial2D(R[m], tracks[t][m], method=cv2.RANSAC)
                if M is not None:
                    sc = float(np.hypot(M[0, 0], M[1, 0]))
                    rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
        else:  # совсем нет видимых точек — держим предыдущее
            prev = out[-1] if out else (0, cx0, cy0, 1.0, 0.0)
            out.append((times[t], prev[1], prev[2], prev[3], prev[4]))
            continue
        out.append((times[t], cx * inv, cy * inv, sc, rot))
    return out, {"reason": "ML: весь клип", "covered": 1.0, "frames": len(out), "query_frame": qf,
                 # гомографии отдаём отдельно: старые вызовы их просто не читают
                 "homos": [h.tolist() for h in homos], "times": list(times),
                 "quality": [list(q) for q in quality], "npoints": int(good.sum()),
                 "dropped": dropped, "systematic": systematic, "affine_frames": naff}
