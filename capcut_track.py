#!/usr/bin/env python3
"""CapCut object tracker — core logic (shared by CLI and the UI server app.py).

Reverse-engineered draft format (CapCut 8.9.1, draft version 360000):
  - transform.x/y units: half of canvas width/height (x=1.0 => center on right edge)
  - keyframe time_offset: SOURCE-media microseconds (segment source_timerange.start
    maps to segment start on the timeline); texts/stickers have no source => 0-based
  - rotation: degrees, scale: 1.0 = fitted-to-canvas size
  - store lives in BOTH draft_info.json and Timelines/<id>/draft_info.json
"""
import copy
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capcut_ui
import ml_track

DRAFTS = os.path.expanduser("~/Movies/CapCut/User Data/Projects/com.lveditor.draft")
US = 1_000_000
# CapCut: ось Y направлена ВВЕРХ (экранная — вниз), поэтому -1.0 (подтверждено: объект вниз -> слой вниз)
SIGN_Y = -1.0
SIGN_ROT = 1.0  # знак поворота CapCut vs экранного (подтверждено: -1 давал обратную сторону)

# Крутилки качества трекинга (калибруются под материал):
NCC_MIN = 0.30        # ниже — считаем, что объект потерян/перекрыт (стоп трека)
DRIFT_FRAMES = 8      # столько подряд «плохих» кадров = стоп
SIGMA_POS = 1.2       # лёгкое сглаживание позиции (кадры); 0 = сырой трек как в AE
SIGMA_SCALE = 2.5     # сглаживание масштаба (шумнее позиции)
SCALE_CLAMP = (0.4, 2.5)   # во сколько раз масштаб может уйти от старта
DECIMATE = False      # False = ключ на КАЖДЫЙ кадр (AE-style); True = прореживать
EPS_POS = 0.004       # прореживание: макс. отклонение позиции (доли полуканваса)
EPS_SCALE = 0.01      # прореживание масштаба


# ---------- draft parsing ----------

def read_draft_path(path):
    """Read a draft json, retrying briefly in case CapCut is mid-autosave."""
    for _ in range(6):
        try:
            with open(path, "rb") as f:
                return json.loads(f.read().decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            time.sleep(0.3)
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def read_draft(proj_dir):
    return read_draft_path(os.path.join(proj_dir, "draft_info.json"))


def material_index(draft):
    idx = {}
    for kind, arr in draft["materials"].items():
        if isinstance(arr, list):
            for m in arr:
                if isinstance(m, dict) and "id" in m:
                    idx[m["id"]] = (kind, m)
    return idx


def seg_name(kind, mat):
    if kind == "texts":
        try:
            return json.loads(mat.get("content", "{}")).get("text", "") or "текст"
        except (json.JSONDecodeError, TypeError):
            return "текст"
    return mat.get("material_name") or kind


def list_projects():
    out = []
    for p in sorted(glob.glob(DRAFTS + "/*/draft_info.json"), key=os.path.getmtime, reverse=True):
        d = os.path.dirname(p)
        out.append({"name": os.path.basename(d), "dir": d,
                    "open": os.path.exists(os.path.join(d, ".locked"))})
    return out


def load_timeline(proj_dir):
    """Draft -> UI-friendly timeline (tracks/segments with times, kinds, names)."""
    draft = read_draft(proj_dir)
    mats = material_index(draft)
    cc = draft["canvas_config"]
    tracks = []
    end = 0
    for ti, t in enumerate(draft.get("tracks", [])):
        segs = []
        for s in t.get("segments", []):
            kind, mat = mats.get(s["material_id"], ("?", {}))
            tr = s["target_timerange"]
            end = max(end, tr["start"] + tr["duration"])
            segs.append({
                "id": s["id"], "kind": kind, "name": str(seg_name(kind, mat)),
                "start": tr["start"], "duration": tr["duration"],
                "has_kf": bool(s.get("common_keyframes")),
                "is_video": bool(mat.get("path")),
                "path": seg_object_path(draft, s) or "",   # СТАБИЛЬНЫЙ ключ объекта (id меняется на сохранении, путь — нет)
            })
        if segs:
            tracks.append({"index": ti, "type": t.get("type"), "segments": segs})
    return {"name": os.path.basename(proj_dir), "dir": proj_dir,
            "canvas": {"w": cc["width"], "h": cc["height"]},
            "fps": draft.get("fps", 30), "duration": draft.get("duration") or end,
            "open": os.path.exists(os.path.join(proj_dir, ".locked")),
            "tracks": tracks}


# ---------- time / keyframe math ----------

def kf_time(seg, timeline_us):
    """timeline time -> keyframe time_offset (source-media time)."""
    src = seg.get("source_timerange")
    start = src["start"] if src else 0
    return start + (timeline_us - seg["target_timerange"]["start"]) * seg.get("speed", 1.0)


def to_timeline(seg, source_us):
    src = seg.get("source_timerange")
    start = src["start"] if src else 0
    return seg["target_timerange"]["start"] + (source_us - start) / seg.get("speed", 1.0)


def overlap_window(vid, target):
    a, b = vid["target_timerange"], target["target_timerange"]
    return max(a["start"], b["start"]), min(a["start"] + a["duration"], b["start"] + b["duration"])


def make_keyframe(t_us, value, src=None):
    kf = {
        "id": str(uuid.uuid4()).upper(), "curveType": "Line", "graphID": "",
        "left_control": {"x": 0.0, "y": 0.0}, "right_control": {"x": 0.0, "y": 0.0},
        # Обычные свойства (позиция/масштаб/поворот) — одно число. У углов corner-pin значение
        # ДВУХкомпонентное (x, y), поэтому список пропускаем как есть.
        "time_offset": int(round(t_us)),
        "values": [float(v) for v in value] if isinstance(value, (list, tuple)) else [float(value)],
        "string_value": "",
    }
    if src:   # перенести тип кривой (easing) с исходного ключа (напр. с ключа слоя-камеры на объект)
        for key in ("curveType", "graphID", "left_control", "right_control"):
            if key in src:
                kf[key] = src[key]
    return kf


def set_curve(seg, prop, pairs):
    # pairs: (t, v) или (t, v, src_kf) — 3-й элемент несёт тип кривой для easing
    seg["common_keyframes"] = [c for c in seg.get("common_keyframes", []) if c["property_type"] != prop]
    seg["common_keyframes"].append({
        "id": str(uuid.uuid4()).upper(), "material_id": "", "property_type": prop,
        "keyframe_list": [make_keyframe(p[0], p[1], p[2] if len(p) > 2 else None) for p in pairs],
    })


def smooth(vals, sigma):
    """Гауссово сглаживание ряда (края — отражением)."""
    import numpy as np

    if sigma <= 0 or len(vals) < 5:
        return list(vals)
    r = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    a = np.pad(np.asarray(vals, float), r, mode="reflect")
    return np.convolve(a, k, mode="valid").tolist()


def decimate(times, channels, eps):
    """Дуглас-Пекер по времени: оставить точки, где линейная интерполяция между
    соседями врёт больше eps[c] хоть по одному каналу (dev нормируется на eps)."""
    n = len(times)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        span = times[hi] - times[lo]
        worst, wi = 0.0, -1
        for i in range(lo + 1, hi):
            f = (times[i] - times[lo]) / span if span else 0
            dev = max(abs(ch[i] - (ch[lo] + f * (ch[hi] - ch[lo]))) / e
                      for ch, e in zip(channels, eps))
            if dev > worst:
                worst, wi = dev, i
        if worst > 1.0 and wi > 0:
            keep[wi] = True
            stack += [(lo, wi), (wi, hi)]
    return [i for i in range(n) if keep[i]]


def compute_curves(draft, vid, target, points):
    """Tracked (t, cx, cy, w, h) -> position + scale curves (сглажены, прорежены)."""
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    _, vmat = material_index(draft)[vid["material_id"]]
    clip = vid["clip"]
    s = min(cw / vmat["width"], ch / vmat["height"]) * clip["scale"]["x"]

    ttr = target["target_timerange"]
    seen, rows = -1, []
    for t_src, cx, cy, w, h in points:
        tl = to_timeline(vid, t_src)
        if not (ttr["start"] <= tl < ttr["start"] + ttr["duration"]):
            continue
        tk = kf_time(target, tl)
        if tk <= seen:
            continue
        seen = tk
        rows.append((tk, cx, cy, w * h))
    if len(rows) < 2:
        return [], [], []

    tks = [r[0] for r in rows]
    cxs = smooth([r[1] for r in rows], SIGMA_POS)
    cys = smooth([r[2] for r in rows], SIGMA_POS)
    areas = smooth([r[3] for r in rows], SIGMA_SCALE)

    def off(px, py):
        return ((px - vmat["width"] / 2) * s + clip["transform"]["x"] * cw / 2,
                (py - vmat["height"] / 2) * s + clip["transform"]["y"] * SIGN_Y * ch / 2)

    x0, y0 = off(cxs[0], cys[0])
    base = target["clip"]["transform"]
    bscale = target["clip"]["scale"]["x"]
    a0 = areas[0] or 1.0
    xv, yv, sv = [], [], []
    for cx, cy, ar in zip(cxs, cys, areas):
        dx, dy = off(cx, cy)
        xv.append(base["x"] + (dx - x0) / (cw / 2))
        yv.append(base["y"] + SIGN_Y * (dy - y0) / (ch / 2))
        ratio = min(max((ar / a0) ** 0.5, SCALE_CLAMP[0]), SCALE_CLAMP[1])
        sv.append(bscale * ratio)

    idx = decimate(tks, [xv, yv, sv], [EPS_POS, EPS_POS, EPS_SCALE]) if DECIMATE else range(len(tks))
    return ([(tks[i], xv[i]) for i in idx],
            [(tks[i], yv[i]) for i in idx],
            [(tks[i], sv[i]) for i in idx])


def compute_curves_ml(draft, vid, target, mlpts, roi):
    """ML-точки (t, cx, cy, scale, rot_deg) -> position + scale + rotation.
    Якорь (нулевой сдвиг) — центр рамки на выбранном кадре, т.к. трек двунаправленный."""
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    _, vmat = material_index(draft)[vid["material_id"]]
    clip = vid["clip"]
    s = min(cw / vmat["width"], ch / vmat["height"]) * clip["scale"]["x"]

    def off(px, py):
        return ((px - vmat["width"] / 2) * s + clip["transform"]["x"] * cw / 2,
                (py - vmat["height"] / 2) * s + clip["transform"]["y"] * SIGN_Y * ch / 2)

    ttr = target["target_timerange"]
    seen, rows = -1, []
    for t_src, cx, cy, sc, rot in mlpts:
        tl = to_timeline(vid, t_src)
        if not (ttr["start"] <= tl < ttr["start"] + ttr["duration"]):
            continue  # ключи только в пределах этого слоя (важно для мультивыбора)
        tk = kf_time(target, tl)
        if tk <= seen:
            continue
        seen = tk
        rows.append((tk, cx, cy, sc, rot))
    if len(rows) < 2:
        return [], [], [], []

    tks = [r[0] for r in rows]
    cxs = smooth([r[1] for r in rows], SIGMA_POS)
    cys = smooth([r[2] for r in rows], SIGMA_POS)
    scs = smooth([r[3] for r in rows], SIGMA_SCALE)
    rts = smooth([r[4] for r in rows], SIGMA_SCALE)

    # ПЛАНАРНОЕ «ПРИКЛЕИВАНИЕ» к поверхности: follower жёстко привязан к объекту. К его
    # смещению от объекта (offset) применяем ВСЁ преобразование поверхности — масштаб+поворот,
    # а не только сдвиг. Тогда при приближении/повороте поверхности слой едет вместе с ней
    # (не «заходит» на объект). Всё в off-координатах (экранные px от центра канваса).
    x0, y0 = off(roi[0] + roi[2] / 2, roi[1] + roi[3] / 2)  # объект на выбранном кадре
    base = target["clip"]["transform"]
    bscale = target["clip"]["scale"]["x"]
    brot = target["clip"].get("rotation", 0.0)
    ox0 = base["x"] * cw / 2 - x0                 # смещение follower'а от объекта (кадр привязки)
    oy0 = SIGN_Y * base["y"] * ch / 2 - y0
    xs, ys, ss, rs = [], [], [], []
    for tk, cx, cy, sc, rot in zip(tks, cxs, cys, scs, rts):
        dx, dy = off(cx, cy)                      # объект на кадре t
        k = min(max(sc, SCALE_CLAMP[0]), SCALE_CLAMP[1])
        rr = math.radians(rot)                    # поворот поверхности (экранное простр., y-вниз)
        c, sn = math.cos(rr), math.sin(rr)
        rox = k * (c * ox0 - sn * oy0)            # масштаб+поворот смещения
        roy = k * (sn * ox0 + c * oy0)
        xs.append((tk, (dx + rox) / (cw / 2)))
        ys.append((tk, SIGN_Y * (dy + roy) / (ch / 2)))
        ss.append((tk, bscale * k))
        rs.append((tk, brot + SIGN_ROT * rot))
    return xs, ys, ss, rs


CORNER_PROPS = ("KFTypeCornerPinUpLeft", "KFTypeCornerPinUpRight",
                "KFTypeCornerPinDownLeft", "KFTypeCornerPinDownRight")

# Материалы, у которых движок ХРАНИТ corner_pin. Замерено 2026-08-08: у `videos` поле переживает
# перезаход и `enable_corner_pin` работает; у `texts` CapCut его молча ВЫБРАСЫВАЕТ при загрузке —
# ключи углов ложатся, канал включить нечем, и трек «проходит», но слой стоит на месте.
CORNER_PIN_KINDS = ("videos",)


def ink_rect(draft, seg, ink):
    """ПРЯМОУГОЛЬНИК ЧЕРНИЛ надписи в NDC коробки слоя: (cx, cy, fx, fy) — центр и полуразмеры.
    (0, 0, 1, 1) = коробка и есть объект (обычное видео/фото), пересчёт углов становится тождеством.

    Чернила живут ВНУТРИ компаунда, в его собственном канвасе, с масштабом и СМЕЩЕНИЕМ
    внутреннего сегмента. Нормированные координаты внутреннего канваса и коробки прокси
    совпадают (канвас компаунда рендерится в коробку целиком), поэтому считаем всё внутри —
    от размера прокси и от растяжки это не зависит.

    ⚠️ Центр обязателен, одних долей размера мало. Живой заворот оставляет смещение надписи
    ВНУТРИ компаунда (прокси движок ставит по центру), файловый — наоборот, выносит его на прокси.
    Пока центр считался нулевым, у любой нецентрированной надписи углы уезжали ровно на её
    смещение, а замер этого не ловил, потому что текст на стенде стоял по центру.

    ink — (ширина, высота) чернил ПРИ МАСШТАБЕ 1, как их отдаёт движок (`text_ink_box`)."""
    if not ink:
        return 0.0, 0.0, 1.0, 1.0
    drafts = {m["id"]: m for m in (draft.get("materials", {}).get("drafts") or [])}
    combo = next((drafts[r] for r in seg.get("extra_material_refs", []) if r in drafts), None)
    if not combo:
        return 0.0, 0.0, 1.0, 1.0
    inner = combo.get("draft") or {}
    icw = float(inner.get("canvas_config", {}).get("width") or 0)
    ich = float(inner.get("canvas_config", {}).get("height") or 0)
    if icw <= 0 or ich <= 0:
        return 0.0, 0.0, 1.0, 1.0
    imats = {m["id"]: (k, m) for k, arr in inner.get("materials", {}).items() if isinstance(arr, list)
             for m in arr if isinstance(m, dict) and "id" in m}
    k, cx, cy = 1.0, 0.0, 0.0
    for t in inner.get("tracks", []):
        for s in t.get("segments", []):
            if imats.get(s.get("material_id"), ("?",))[0] != "texts":
                continue
            clip = s.get("clip") or {}
            k = float(clip.get("scale", {}).get("x", 1.0)) or 1.0
            tr = clip.get("transform") or {}
            # transform — в полукадрах, y вверх; NDC углов — те же полукадры, но y ВНИЗ
            cx = float(tr.get("x", 0.0))
            cy = SIGN_Y * float(tr.get("y", 0.0))
    return cx, cy, min(ink[0] * k / icw, 1e6), min(ink[1] * k / ich, 1e6)


def ink_fractions(draft, seg, ink):
    """Только доли размера, без центра — оставлено для прежних вызовов (`test_text_stretch`)."""
    return ink_rect(draft, seg, ink)[2:]


def compound_text_seg_id(draft, seg):
    """id ТЕКСТОВОГО сегмента внутри компаунда либо None. Нужен, чтобы спросить у движка габарит
    чернил у слоя, который завернули РАНЬШЕ: карты заворота к этому моменту уже нет."""
    drafts = {m["id"]: m for m in (draft.get("materials", {}).get("drafts") or [])}
    combo = next((drafts[r] for r in seg.get("extra_material_refs", []) if r in drafts), None)
    if not combo:
        return None
    inner = combo.get("draft") or {}
    imats = {m["id"]: (k, m) for k, arr in inner.get("materials", {}).items() if isinstance(arr, list)
             for m in arr if isinstance(m, dict) and "id" in m}
    for t in inner.get("tracks", []):
        for s in t.get("segments", []):
            if imats.get(s.get("material_id"), ("?",))[0] == "texts":
                return s.get("id")
    return None


def quad_for_ink(quad, fx, fy, cx=0.0, cy=0.0):
    """Квад «куда встанет КОРОБКА» из квада «куда должны встать ЧЕРНИЛА».

    Углы движку задают положение коробки слоя, а объект внутри коробки — только её часть
    (у завёрнутого текста надпись занимает доли процента площади). Поэтому квад, посчитанный
    трекером для ОБЪЕКТА, нельзя слать как есть: движок натянет на него коробку целиком, и
    чернила сядут в ту же долю от цели, во сколько раз коробка их больше. Ровно это и мерили:
    промах 5.97× по горизонтали и 27.3× по вертикали.

    Берём гомографию «прямоугольник чернил → целевой квад» и применяем её к УГЛАМ КОРОБКИ.
    Тогда чернила садятся на цель точно, а куда при этом уехала коробка — неважно: она
    прозрачна. Размер коробки перестаёт влиять на результат, поэтому растяжка внутри компаунда
    (`text_stretch`) для углов больше не нужна.

    quad — 4 точки (UL, UR, DL, DR) в NDC коробки; (cx, cy, fx, fy) — прямоугольник чернил
    из `ink_rect`. Центр по умолчанию нулевой: у видео чернила и есть коробка."""
    import numpy as np

    if abs(fx - 1.0) < 1e-6 and abs(fy - 1.0) < 1e-6 and abs(cx) < 1e-9 and abs(cy) < 1e-9:
        return [(float(x), float(y)) for x, y in quad]
    src = np.array([[cx - fx, cy - fy], [cx + fx, cy - fy],
                    [cx - fx, cy + fy], [cx + fx, cy + fy]], float)
    # DLT по четырём соответствиям: 8 уравнений на 8 неизвестных (h33 = 1)
    A, b = [], []
    for (sx, sy), q in zip(src, quad):
        dx, dy = float(q[0]), float(q[1])
        A.append([sx, sy, 1, 0, 0, 0, -dx * sx, -dx * sy]); b.append(dx)
        A.append([0, 0, 0, sx, sy, 1, -dy * sx, -dy * sy]); b.append(dy)
    h = np.linalg.solve(np.array(A, float), np.array(b, float))
    H = np.append(h, 1.0).reshape(3, 3)
    out = []
    for bx, by in ((-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)):
        p = H @ np.array([bx, by, 1.0])
        if abs(p[2]) < 1e-12:                      # коробка ушла в бесконечность — квад не годится
            return [(float(x), float(y)) for x, y in quad]
        out.append((float(p[0] / p[2]), float(p[1] / p[2])))
    return out


def cornerpin_capable(draft, seg):
    """Можно ли гнать этот слой по углам. Проверяем ДО трека: иначе пользователь ждёт минуту
    ради результата, который заведомо мёртв, и без единой ошибки."""
    ent = material_index(draft).get(seg.get("material_id"))
    return (ent[0] if ent else "?") in CORNER_PIN_KINDS, (ent[0] if ent else "?")


# Порог RANSAC в ml_track — 3 px трекера. Медиана выше PLANE_RMS_WARN означает, что точки
# держатся в консенсусе еле-еле: гомография скорее ПОДОГНАНА, чем найдена.
PLANE_RMS_WARN = 2.0
PLANE_INLIER_WARN = 0.5      # доля точек, удержавшихся в консенсусе


def _plane_report(meta, log=print):
    """Честный разбор качества плоскости по покадровым (инлаеры, RMS) из ml_track.

    Зачем: маска инлаеров раньше выбрасывалась, и трек по НЕ-плоскости (лицо, куст, объёмный
    предмет) выглядел ровно так же, как удачный — RANSAC всегда что-нибудь вернёт. У обычного
    режима защита есть («ТРЕК ВЕРНУЛ СТАТИКУ»), у планарного не было никакой."""
    q = [tuple(x) for x in (meta.get("quality") or [])]
    npts = meta.get("npoints") or 0
    if not q:
        return {}
    lost = [i for i, (inl, _) in enumerate(q) if inl < 4]
    ok = [(inl, r) for inl, r in q if inl >= 4 and r >= 0]
    out = {"plane_frames": len(q), "lost": len(lost), "points": npts,
           "dropped": meta.get("dropped", 0), "affine_frames": meta.get("affine_frames", 0)}
    if out["affine_frames"]:
        log(f"модель: на {out['affine_frames']} из {len(q)} кадров перспектива не окупала себя — "
            f"взят аффин (иначе углы гуляют на недоопределённой геометрии)")
    if ok:
        rms = sorted(r for _, r in ok)
        med = rms[len(rms) // 2]
        frac = (sum(i for i, _ in ok) / len(ok) / npts) if npts else 0.0
        out.update({"rms": round(med, 2), "rms_max": round(rms[-1], 2), "inliers": round(frac, 2)})
        log(f"плоскость: точек {npts}, в консенсусе {frac:.0%}, ошибка репроекции "
            f"медиана {med:.2f} px, худшая {rms[-1]:.2f} px (порог RANSAC 3.0)")
        if med > PLANE_RMS_WARN or frac < PLANE_INLIER_WARN:
            out["suspect"] = True
            log("⚠️  ПОХОЖЕ, ЭТО НЕ ПЛОСКОСТЬ: точки не сходятся к одной гомографии. "
                "Обведи ПЛОСКИЙ участок (экран, вывеску, стену), а не объёмный предмет.")
    if lost:
        log(f"⚠️  плоскость ПОТЕРЯНА на {len(lost)} из {len(q)} кадров (<4 видимых точек) — "
            f"там углы держат последний удачный кадр")
    return out


def _mat3(m):
    """2x3 аффин из _layer_matrix -> однородная 3x3."""
    return [[m[0][0], m[0][1], m[0][2]], [m[1][0], m[1][1], m[1][2]], [0.0, 0.0, 1.0]]


def compute_cornerpin_ml(draft, vid, target, homos, times, quad_src=None, ink=None):
    """ПЛАНАРНЫЙ ТРЕК: гомографии поверхности -> кривые четырёх углов follower'а.

    Similarity (позиция+масштаб+поворот) не умеет перспективу: наклонённый экран/вывеска
    «плывёт». Гомография описывает плоскость целиком, поэтому вместо трансформа слоя гоняем
    его УГЛЫ — движок CapCut умеет corner_pin нативно (KFTypeCornerPin*).

    SURFACE ОТДЕЛЬНО ОТ ТРЕК-РЕГИОНА (как в Mocha). Раньше через гомографию гнались углы самой
    рамки ROI, из-за чего слой насильно натягивало на обведённый прямоугольник, куда бы его ни
    положил пользователь. Теперь рамка отвечает только за то, ГДЕ ИСКАТЬ ТЕКСТУРУ, а едет
    собственный прямоугольник follower'а: при H = единица углы выходят ровно единичными, то есть
    слой остаётся там, где его поставили, и дальше просто едет вместе с плоскостью.

    СИСТЕМА КООРДИНАТ УГЛОВ — NDC [-1..1] относительно СОБСТВЕННОЙ коробки слоя, y вниз
    (ЗАМЕРЕНО на экране: слой с clip-масштабом 0.366 и статичным квадом [-0.9..-0.1] лёг в
    x 719..786 px при предсказании «коробка слоя» 718..786 и «канвас» 565..754). Канвасные NDC,
    которые писались раньше, совпадают с этими только при масштабе слоя ≈1 — потому перспектива
    камеры на полноэкранных слоях выглядела верной, а follower на 0.37 уезжал.
    Поэтому NDC углов = просто координаты материала follower'а, нормированные в [-1..1].
    ЗАМЕРЕНО отдельно: corner-pin живёт в ЛОКАЛЬНОЙ системе слоя, clip-поворот накладывается
    ПОВЕРХ (единичные углы при rotation=30° дают ровно то же, что и полное их отсутствие) — потому
    и правомерно гонять всё через _layer_matrix, который поворот уже содержит.

    quad_src: 4 угла поверхности в px ИСХОДНОГО видео (UL,UR,DL,DR). Задан — слой НАТЯГИВАЕТСЯ на
    этот четырёхугольник и дальше едет с плоскостью (замена экрана в один клик). Не задан — едет
    собственный прямоугольник слоя, то есть слой остаётся там, где его положили.
    ponytail: трек-регион и Surface тут одно и то же. Развести их (трекать меньший кусок, чем
    накрываем) понадобится, когда экран начнут заслонять пальцем.
    """
    import numpy as np

    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    _, vmat = material_index(draft)[vid["material_id"]]
    _, tmat = material_index(draft)[target["material_id"]]
    # ponytail: у текста/стикера в материале нет своих размеров — берём канвас, коробка слоя
    # тогда считается по его clip, чего для позиционирования углов достаточно.
    fw = float(tmat.get("width") or cw)
    fh = float(tmat.get("height") or ch)
    P = np.array(_mat3(_layer_matrix(vid["clip"], vmat["width"], vmat["height"], cw, ch)))
    F = np.array(_mat3(_layer_matrix(target["clip"], fw, fh, cw, ch)))
    Pinv, Finv = np.linalg.inv(P), np.linalg.inv(F)
    # Что именно едет по плоскости, в px ИСХОДНОГО видео (столбцы UL, UR, DL, DR):
    # выбранный четырёхугольник либо, если его нет, собственный прямоугольник follower'а.
    if quad_src is not None:
        src0 = np.array([[p[0] for p in quad_src], [p[1] for p in quad_src], [1.0] * 4], float)
    else:
        src0 = Pinv @ F @ np.array([[0.0, fw, 0.0, fw], [0.0, 0.0, fh, fh], [1.0, 1.0, 1.0, 1.0]])

    icx, icy, fx, fy = ink_rect(draft, target, ink)   # прямоугольник чернил в NDC коробки
    ttr = target["target_timerange"]
    seen = -1
    rows = []
    for t_src, H in zip(times, homos):
        tl = to_timeline(vid, t_src)
        if not (ttr["start"] <= tl < ttr["start"] + ttr["duration"]):
            continue
        tk = kf_time(target, tl)
        if tk <= seen:
            continue
        seen = tk
        # px исходника -> варп плоскостью -> канвас -> px follower'а -> его же NDC
        q = Finv @ P @ np.array(H) @ src0
        if np.any(np.abs(q[2]) < 1e-9):      # вырожденная гомография — кадр пропускаем
            continue
        q = q / q[2]
        quad = [(2.0 * q[0][k] / fw - 1.0, 2.0 * q[1][k] / fh - 1.0) for k in range(4)]
        rows.append((tk, quad_for_ink(quad, fx, fy, icx, icy)))   # цель ставим ЧЕРНИЛАМ, не коробке
    if len(rows) < 2:
        return {}
    # Сглаживаем каждую координату отдельно: рывок одного угла читается как «дрожь плоскости»,
    # а пользователь просил стабильность в приоритет над скоростью.
    out = {}
    for ci, prop in enumerate(CORNER_PROPS):
        xs = smooth([r[1][ci][0] for r in rows], SIGMA_POS)
        ys = smooth([r[1][ci][1] for r in rows], SIGMA_POS)
        out[prop] = [(r[0], [xv, yv]) for r, xv, yv in zip(rows, xs, ys)]
    return out


def compute_curves_reframe(draft, vid, mlpts, roi, want_scale=True):
    """СЛЕЖКА ЗА ОБЪЕКТОМ (авто-рефрейм): двигаем САМ видеоклип, чтобы объект оставался там,
    где он был на выбранном кадре — компенсируем его движение (объект «залипает» в кадре).
    + авто-зум, чтобы сдвиг не открывал чёрные края. Возвращает (xs, ys, ss, None) для vid."""
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    _, vmat = material_index(draft)[vid["material_id"]]
    clip = vid["clip"]
    s = min(cw / vmat["width"], ch / vmat["height"]) * clip["scale"]["x"]

    def off(px, py):   # пиксель исходника -> экранный офсет от центра канваса (px, y-вниз)
        return ((px - vmat["width"] / 2) * s + clip["transform"]["x"] * cw / 2,
                (py - vmat["height"] / 2) * s + clip["transform"]["y"] * SIGN_Y * ch / 2)

    ttr = vid["target_timerange"]
    seen, rows = -1, []
    for t_src, cx, cy, sc, rot in mlpts:
        tl = to_timeline(vid, t_src)
        if not (ttr["start"] <= tl < ttr["start"] + ttr["duration"]):
            continue
        tk = kf_time(vid, tl)
        if tk <= seen:
            continue
        seen = tk
        rows.append((tk, cx, cy))
    if len(rows) < 2:
        return [], [], [], []
    tks = [r[0] for r in rows]
    cxs = smooth([r[1] for r in rows], SIGMA_POS)
    cys = smooth([r[2] for r in rows], SIGMA_POS)
    dx0, dy0 = off(roi[0] + roi[2] / 2, roi[1] + roi[3] / 2)   # объект на кадре-якоре
    bx, by, bscale = clip["transform"]["x"], clip["transform"]["y"], clip["scale"]["x"]
    ds = [off(cx, cy) for cx, cy in zip(cxs, cys)]
    # Авто-зум: покрыть максимальный сдвиг, чтобы не открылись чёрные поля (клампим 1.0..1.6).
    # Зум РАСТИТ и сам сдвиг (он в NDC, а слой уже увеличен), поэтому нужное покрытие — не
    # 1+сдвиг, а 1/(1-сдвиг): при сдвиге 0.5 полукадра увеличение 1.5 всё ещё открывает край.
    maxshift = max((abs((dx - dx0) / (cw / 2)) for dx, _ in ds), default=0.0)
    maxshift = max(maxshift, max((abs((dy - dy0) / (ch / 2)) for _, dy in ds), default=0.0))
    zoom = min(1.6, 1.0 / (1.0 - maxshift)) if want_scale and maxshift < 1.0 else \
        (1.6 if want_scale else 1.0)
    xs, ys, ss = [], [], []
    for tk, (dx, dy) in zip(tks, ds):
        # ⚠️ ×zoom обязателен: off() считает смещение при БАЗОВОМ масштабе, а рендер идёт при
        # scale*zoom. Без множителя компенсация недобирает ровно в zoom раз — замер 2026-08-09:
        # объект уходил на 268 px из 429 при зуме ×1.6, вместо 11 px без зума.
        xs.append((tk, bx - (dx - dx0) * zoom / (cw / 2)))         # видео едет ПРОТИВ объекта
        ys.append((tk, by - SIGN_Y * (dy - dy0) * zoom / (ch / 2)))
        ss.append((tk, bscale * zoom))
    return xs, ys, ss, None


def native_boxes(pts, roi, src_wh):
    """Трек CoTracker -> боксы РОДНОГО формата CapCut (materials/videoTracking/<GUID>/data.json).
    pts: [(t_us, cx, cy, scale, rot_deg)] — центр в ПИКСЕЛЯХ ИСХОДНИКА; roi=(x,y,w,h) там же.
    Формат сверен численно с их файлом: доли кадра, y ВНИЗ (top<bottom), pts в мкс.
    (проверка: cache.json box/(image_width,image_height) совпал с data.json до 1e-6)"""
    sw, sh = src_wh
    rw, rh = roi[2], roi[3]
    out = []
    for t_us, cx, cy, sc, rot in pts:
        # float() обязателен: CoTracker отдаёт numpy.float32, а json его не сериализует
        cx, cy, sc = float(cx), float(cy), float(sc)
        # Тот же зажим, что и в кейфреймовом пути (compute_curves_ml): без него сырой масштаб
        # трекера раздувал бокс к концу клипа, top уходил в минус и объект улетал за кадр.
        sc = min(max(sc, SCALE_CLAMP[0]), SCALE_CLAMP[1])
        hw, hh = rw * sc / 2.0, rh * sc / 2.0
        out.append({"angle": float(rot),
                    "bottom": float((cy + hh) / sh), "left": float((cx - hw) / sw),
                    "pts": int(round(float(t_us))), "right": float((cx + hw) / sw),
                    "status": 1, "top": float((cy - hh) / sh)})
    return out


def write_native_tracking(proj_dir, vid_seg_id, flw_seg_id, boxes, baseline_half, baseline_pts,
                          enable_scale=True, enable_distance=True, log=print):
    """Кладёт трек РОДНЫМ путём CapCut: материал video_tracking + JSON-файлы траектории,
    БЕЗ кейфреймов (их трекер работает именно так — см. память capcut-native-tracking-format).
    baseline_half=(hx,hy) — полугабарит follower'а в долях канваса, y ВВЕРХ (как у них).
    Возвращает id созданного материала."""
    mat_id, track_guid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    tdir = os.path.join(proj_dir, "materials", "videoTracking", track_guid)
    os.makedirs(tdir, exist_ok=True)
    t0 = min((b["pts"] for b in boxes), default=0)
    t1 = max((b["pts"] for b in boxes), default=0)
    hx, hy = baseline_half
    json.dump({"baseline": [{"angle": 0.0, "bottom": -hy, "left": -hx, "pts": int(baseline_pts),
                             "right": hx, "status": 4, "top": hy}],
               "data": boxes}, open(os.path.join(tdir, "data.json"), "w"))
    json.dump({"baselinePts": [], "endTime": int(t1), "resType": 1, "startTime": -1},
              open(os.path.join(tdir, "desc.json"), "w"))
    # ponytail: cache.json НЕ пишем — это сырьё в пикселях для ИХ панели, движку он не нужен.
    # Если окажется обязательным, добавить: {"image_width","image_height","track_boxes":[[сек,[x1,y1,x2,y2]]]}.
    mat = {"id": mat_id, "type": "video_tracking", "result_path": "", "map_path": "",
           "config": {"width": 0.0, "height": 0.0, "center_x": 0.0, "center_y": 0.0, "rotation": 0.0},
           "version": "v3", "tracker_type": 1,
           "enable_scale": bool(enable_scale), "enable_relative_distance": bool(enable_distance),
           "tracking_time_range": 0, "enable_video_tracking": True, "track_covert_keyframe": False,
           "trackers": [{"target_segment_id": vid_seg_id, "src_segment_id": flw_seg_id,
                         "data_path": f"##_draftpath_placeholder_{PLACEHOLDER_GUID}_##"
                                      f"/materials/videoTracking/{track_guid}",
                         "type": 0, "surface_id": "",
                         "surface_second_edit_config": {"left_up_x": 0.0, "left_up_y": 0.0,
                                                       "left_down_x": 0.0, "left_down_y": 0.0,
                                                       "right_down_x": 0.0, "right_down_y": 0.0,
                                                       "right_up_x": 0.0, "right_up_y": 0.0},
                         "second_edit_media_time": 0.0}]}
    root = os.path.join(proj_dir, "draft_info.json")
    n = 0
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(path)
        segs = {s["id"]: s for t in d["tracks"] for s in t.get("segments", [])}
        if flw_seg_id not in segs:
            continue
        d["materials"].setdefault("video_trackings", []).append(json.loads(json.dumps(mat)))
        refs = segs[flw_seg_id].setdefault("extra_material_refs", [])
        if mat_id not in refs:
            refs.append(mat_id)
        with open(path, "w") as f:
            json.dump(d, f, ensure_ascii=False)
        n += 1
    log(f"родной трекинг: материал {mat_id[:8]}…, {len(boxes)} боксов, файлов проекта: {n}")
    return mat_id


def inject_curves(proj_dir, curves_by_target):
    """curves_by_target: {target_id: (xs, ys, ss, rs)}. Пишет все слои за один проход
    по каждому файлу (root + Timelines/*). Возвращает число записанных файлов."""
    root = os.path.join(proj_dir, "draft_info.json")
    written = 0
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(path)
        by_id = {s["id"]: s for t in d["tracks"] for s in t.get("segments", [])}
        hit = False
        for tid, (xs, ys, ss, rs) in curves_by_target.items():
            if tid not in by_id:
                continue
            seg = by_id[tid]
            wrote = False
            if xs:
                set_curve(seg, "KFTypePositionX", xs)
                wrote = True
            if ys:
                set_curve(seg, "KFTypePositionY", ys)
                wrote = True
            if ss:
                set_curve(seg, "KFTypeScaleX", ss)
                seg.setdefault("uniform_scale", {"on": True, "value": 1.0})["on"] = True
                # убрать залипший KFTypeScaleY (от прошлого live-персиста): при uniform_scale.on ScaleX сам тянет Y,
                # а лишний ScaleY компаундится (ScaleX×ScaleY) → «миллион процентов» масштаба после перезахода
                seg["common_keyframes"] = [c for c in seg.get("common_keyframes", []) if c["property_type"] != "KFTypeScaleY"]
                wrote = True
            if rs:
                set_curve(seg, "KFTypeRotation", rs)
                wrote = True
            hit = hit or wrote
        if hit:
            shutil.copy2(path, path + ".pretrack.bak")
            with open(path, "wb") as f:
                f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            written += 1
    return written


TRACK_PROPS = ("KFTypePositionX", "KFTypePositionY", "KFTypeScaleX", "KFTypeScaleY",
               "KFTypeRotation") + CORNER_PROPS


def count_track_keys(proj_dir, target_ids):
    """Сколько ключей, поставленных трекингом, уже висит на этих слоях.
    Нужно, чтобы спросить «перезаписать?» ДО долгого трека, а не после."""
    draft = read_draft(proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", []) or []}
    n = 0
    for tid in target_ids:
        seg = by_id.get(tid)
        if not seg:
            continue
        for c in seg.get("common_keyframes") or []:
            if c["property_type"] in TRACK_PROPS:
                n += len(c.get("keyframe_list") or [])
    return n


def clear_track_keys(proj_dir, target_ids):
    """Снять с указанных слоёв ВСЁ, что наложил трекинг (позиция/масштаб/поворот/углы).
    Чужие свойства (прозрачность, маска и т.п.) не трогаем — снимаем только свои."""
    root = os.path.join(proj_dir, "draft_info.json")
    removed, written = 0, 0
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(path)
        by_id = {s["id"]: s for t in d["tracks"] for s in t.get("segments", []) or []}
        hit = False
        for tid in target_ids:
            seg = by_id.get(tid)
            if not seg:
                continue
            keep = []
            for c in seg.get("common_keyframes") or []:
                if c["property_type"] in TRACK_PROPS:
                    removed += len(c.get("keyframe_list") or [])
                    hit = True
                else:
                    keep.append(c)
            seg["common_keyframes"] = keep
        if hit:
            shutil.copy2(path, path + ".preclear.bak")
            with open(path, "wb") as f:
                f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            written += 1
    return {"removed": removed, "files": written}


# Единичный квад канала corner-pin: NDC [-1..1] от центра СОБСТВЕННОЙ коробки слоя, y вниз.
# Не UV [0..1], как считалось раньше: с единичным UV-квадратом слой уезжал ровно в правый нижний
# квадрант своей коробки — то есть (0,0)…(1,1) движок читает как NDC, а не как «весь кадр».
CORNER_PIN_IDENTITY = {"upper_left_x": -1.0, "upper_left_y": -1.0,
                       "upper_right_x": 1.0, "upper_right_y": -1.0,
                       "lower_left_x": -1.0, "lower_left_y": 1.0,
                       "lower_right_x": 1.0, "lower_right_y": 1.0}


def enable_corner_pin(draft, seg, pin_id):
    """Включить канал corner-pin на материале слоя.

    БЕЗ ЭТОГО КЛЮЧИ KFTypeCornerPin* МЁРТВЫ. Замерено: при corner_pin=null слой рендерится
    ровно по своему clip, углы движок игнорирует молча — 180 ключей на четырёх свойствах не
    давали на экране ничего. Стоит включить канал — те же ключи начинают его двигать.
    pin_id передаётся снаружи: обе копии драфта обязаны получить ОДИНАКОВЫЙ id."""
    ent = material_index(draft).get(seg.get("material_id"))
    if not ent:
        return False
    mat = ent[1]
    if isinstance(mat.get("corner_pin"), dict):
        mat["corner_pin"]["enable_corner_pin"] = True     # уже есть (напр. от наклона) — не трогаем квад
    else:
        mat["corner_pin"] = {"id": pin_id, "sub_type": "video", "enable_corner_pin": True,
                             "corner_pin_info": dict(CORNER_PIN_IDENTITY)}
    return True


def inject_cornerpin(proj_dir, corners_by_target):
    """corners_by_target: {target_id: {KFTypeCornerPin*: [(t,[x,y]), ...]}}.
    Пишет углы во ВСЕ копии драфта (root + Timelines/*) — как и обычные кривые: иначе CapCut
    перечитает авторитетную копию из Timelines/ и правки исчезнут."""
    root = os.path.join(proj_dir, "draft_info.json")
    written = 0
    # id квада чеканим ОДИН раз на слой, снаружи файлового цикла: разойдись он между копиями —
    # получили бы ту же невидимую поломку, что ловит test_compound_ids.
    pin_ids = {tid: str(uuid.uuid4()).upper() for tid in corners_by_target}
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(path)
        by_id = {s["id"]: s for t in d["tracks"] for s in t.get("segments", [])}
        hit = False
        for tid, props in corners_by_target.items():
            seg = by_id.get(tid)
            if not seg or not props:
                continue
            for prop, pairs in props.items():
                set_curve(seg, prop, pairs)
            enable_corner_pin(d, seg, pin_ids[tid])
            hit = True
        if hit:
            shutil.copy2(path, path + ".preplanar.bak")
            with open(path, "wb") as f:
                f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            written += 1
    return written


CAM_RZ_MARK = 7.777   # распознаваемый поворот-маркер слоя-камеры (невидим: alpha 0) — дилиб ловит слой по нему


def cam_png_path(cw, ch, tok=None):
    """Путь прозрачного PNG слоя-камеры. tok — токен сцены: УНИКАЛЬНЫЙ путь на сцену (дилиб различает камеры по пути).
    Без tok — старый однокамерный путь (назад-совместимость)."""
    suf = f"_{tok}" if tok else ""
    return os.path.expanduser(f"~/Movies/CapCut/User Data/plugin_assets/cam3d_{int(cw)}x{int(ch)}{suf}.png")


def _make_cam_png(cw, ch, tok=None):
    """PNG-носитель слоя-камеры. На РЕНДЕР его содержимое не влияет вообще: клип камеры невидим
    через `clip.alpha = 0` (см. add_camera_layer), а не через прозрачность картинки. Поэтому
    рисуем в нём фирменную метку — CapCut берёт её в миниатюру клипа, и дорожка камеры на
    таймлайне сразу отличается от обычного медиа. Это и есть пометка из спеки D5, без единого
    хука: чужой UI не трогаем, пользуемся их же миниатюрой."""
    import cv2
    import numpy as np

    p = cam_png_path(cw, ch, tok)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    w, h = int(cw), int(ch)
    img = np.zeros((h, w, 4), np.uint8)
    img[:, :, :3] = (46, 34, 24)          # BGR: тёмно-синий фон, чтобы метка читалась
    img[:, :, 3] = 255
    TEAL = (205, 193, 0, 255)             # BGRA = #00C1CD, тот же бирюзовый, что у иконок NZLD+
    # спираль — тот же знак, что на кнопке в шапке дорожки
    r0, cx, cy = min(w, h) * 0.30, w // 2, h // 2
    a = np.linspace(0, 4.2 * np.pi, 260)
    pts = np.stack([cx + np.cos(a) * r0 * a / a[-1], cy + np.sin(a) * r0 * a / a[-1]], 1)
    cv2.polylines(img, [pts.astype(np.int32)], False, TEAL, max(2, h // 90), cv2.LINE_AA)
    fs, th = h / 520.0, max(1, h // 300)
    (tw, tht), _ = cv2.getTextSize("NZLD+ CAMERA", cv2.FONT_HERSHEY_DUPLEX, fs, th)
    cv2.putText(img, "NZLD+ CAMERA", (cx - tw // 2, int(cy + r0 * 1.3) + tht),
                cv2.FONT_HERSHEY_DUPLEX, fs, TEAL, th, cv2.LINE_AA)
    cv2.imwrite(p, img)   # перезаписываем всегда: у старых камер носитель ещё пустой
    return p


def _make_handle_png(cw, ch):
    """Уникальная прозрачная PNG-«ручка» для БЕСПУТНОГО объекта (текст/группа): даёт компаунду резолвимый путь.
    CapCut рендерит содержимое combination, НЕ ручку (проверено микро-тестом). Дилиб резолвит+драйвит proxy по ней."""
    import cv2
    import numpy as np
    p = os.path.expanduser("~/Movies/CapCut/User Data/plugin_assets/obj_%s.png" % uuid.uuid4().hex[:10])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cv2.imwrite(p, np.zeros((int(ch), int(cw), 4), np.uint8))
    return p


def add_camera_layer(proj_dir, cw, ch, w0, w1, campath, scene_tok, log=print):
    """Вживить прозрачный слой-камеру СЦЕНЫ scene_tok = ДУБЛИКАТ существующего сегмента (свежие UUID, alpha=0)
    с ключами позиции/масштаба = путь камеры. Имя трека «Камера 3D · <tok>» и PNG уникальны на сцену →
    несколько камер сосуществуют, дилиб различает по пути. Удаляет ТОЛЬКО трек ЭТОЙ сцены. Возвращает id слоя."""
    import camera3d
    cam_name = f"Камера 3D · {scene_tok}"
    root = os.path.join(proj_dir, "draft_info.json")
    d0 = read_draft_path(root)
    template = next((s for t in d0["tracks"] for s in t.get("segments", [])
                     if t.get("type") == "video" and s.get("clip") and s.get("material_id")), None)
    if not template:
        log("нет видео-сегмента-шаблона — слой-камеру не создать"); return None
    matidx = {}
    for arr, items in d0["materials"].items():
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    matidx[it["id"]] = arr
    # дублируем сегмент и КАЖДЫЙ его материал (video + extra_material_refs) с новым UUID
    idmap, new_mats = {}, {}   # old id -> new id ; arr -> [new material dicts]

    def dup_mat(old_id):
        if old_id in idmap:
            return idmap[old_id]
        arr = matidx.get(old_id)
        if not arr:
            return old_id   # ссылка не на материал (или нет в индексе) — оставить
        src = next(m for m in d0["materials"][arr] if m.get("id") == old_id)
        m = copy.deepcopy(src); m["id"] = str(uuid.uuid4()).upper()
        idmap[old_id] = m["id"]; new_mats.setdefault(arr, []).append(m)
        return m["id"]

    seg = copy.deepcopy(template)
    seg["id"] = str(uuid.uuid4()).upper()
    seg["material_id"] = dup_mat(template["material_id"])
    seg["extra_material_refs"] = [dup_mat(r) for r in template.get("extra_material_refs", [])
                                  if matidx.get(r) != "drafts"]   # без combination — камера теперь простой прозрачный фото-слой
    seg["clip"] = copy.deepcopy(template.get("clip", {})); seg["clip"]["alpha"] = 0.0   # прозрачный
    seg["desc"] = cam_name
    seg["common_keyframes"], seg["keyframe_refs"] = [], []
    dur = max(1, int(w1 - w0))
    src_dur = int(template.get("source_timerange", {}).get("duration", dur) or dur)
    seg["target_timerange"] = {"start": int(w0), "duration": dur}
    seg["source_timerange"] = {"start": 0, "duration": min(dur, src_dur)}
    # ключи слоя-камеры (позиция в CapCut-единицах = сдвиг/(canvas/2); масштаб = зум) = путь камеры
    f = camera3d.FOCAL_FRAC * ch
    hw, hh = cw / 2.0, ch / 2.0
    px = [(kf_time(seg, k["t"]), -k["cx"] * f / (camera3d.Z_REF * hw)) for k in campath]
    py = [(kf_time(seg, k["t"]), -k["cy"] * f / (camera3d.Z_REF * hh)) for k in campath]
    sc = [(kf_time(seg, k["t"]), camera3d.Z_REF / (camera3d.Z_REF - k["cz"])) for k in campath]
    set_curve(seg, "KFTypePositionX", px); set_curve(seg, "KFTypePositionY", py); set_curve(seg, "KFTypeScaleX", sc)
    # rz (поворот) НЕ трогаем — он теперь свободен под РОЛЛ камеры (юзер крутит слой → объекты вращаются вокруг центра)
    # uniform_scale ВЫКЛЮЧЕН: масштаб Y отвязывается от X и становится каналом ФОКУСА глубины
    # резкости (дилиб читает его как sy). Это последний свободный ключуемый канал камеры, и
    # благодаря ему фокус получает родные ключи и кривые вместо отдельной дорожки-контроллера.
    # Клип камеры невидим (alpha=0), так что растяжение PNG по вертикали ни на что не влияет.
    # Замерено: 948 углов corner-pin совпали до девятого знака — в наезд Y не протекает.
    seg["uniform_scale"] = {"on": False, "value": 1.0}
    seg["clip"]["scale"]["y"] = camera3d.DOF_FOCUS_DEFAULT
    png = _make_cam_png(cw, ch, scene_tok)   # УНИКАЛЬНЫЙ на сцену прозрачный PNG (дилиб различает камеры по пути)
    for items in new_mats.values():   # перенаправить материал камеры на PNG + подписать
        for m in items:
            if m["id"] == seg["material_id"]:
                m["material_name"] = cam_name
                m["type"] = "photo"; m["path"] = png; m["media_path"] = png
                m["has_audio"] = False; m["width"] = int(cw); m["height"] = int(ch)
                m["duration"] = 10800000000   # фото: длинная длительность
    new_track = {"attribute": 0, "flag": 0, "id": str(uuid.uuid4()).upper(),
                 "is_default_name": False, "name": cam_name, "segments": [seg], "type": "video"}
    written = 0
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(path)
        d["tracks"] = [t for t in d["tracks"] if t.get("name") != cam_name]   # убрать ТОЛЬКО прошлый слой ЭТОЙ сцены (другие камеры целы)
        for arr, items in new_mats.items():
            d["materials"].setdefault(arr, []).extend(copy.deepcopy(items))
        d["tracks"].append(copy.deepcopy(new_track))
        shutil.copy2(path, path + ".precam.bak")
        with open(path, "wb") as fp:
            fp.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        written += 1
    log(f"слой-камера вживлён: seg={seg['id'][:8]}, материалов {sum(len(v) for v in new_mats.values())}, файлов {written}")
    return seg["id"]


def _make_dof_png(cw, ch):
    """Прозрачный PNG слоя-контроллера DoF (ОТДЕЛЬНЫЙ путь → дилиб отличает от камеры)."""
    import cv2
    import numpy as np
    p = os.path.expanduser(f"~/Movies/CapCut/User Data/plugin_assets/dof3d_{int(cw)}x{int(ch)}.png")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        cv2.imwrite(p, np.zeros((int(ch), int(cw), 4), np.uint8))
    return p


def add_dof_layer(proj_dir, cw, ch, w0, w1, log=print, focus=1.96, aperture=0.3):
    """Слой-контроллер глубины резкости: прозрачный слой, uniform_scale OFF (X/Y независимы).
    Дилиб читает его sx (диафрагма) и sy (фокус) вживую. Клон видео-сегмента-шаблона (как слой-камера),
    seed ключей px=0 / sx=1 / sy=1 (px нужен для матча по пути в hook0). Пишет root + Timelines. Возвращает id.
    ВНИМАНИЕ: писать при ЗАКРЫТОМ проекте (бейк)."""
    root = os.path.join(proj_dir, "draft_info.json")
    d0 = read_draft_path(root)
    template = next((s for t in d0["tracks"] for s in t.get("segments", [])
                     if t.get("type") == "video" and s.get("clip") and s.get("material_id")), None)
    if not template:
        log("нет видео-сегмента-шаблона — DoF-слой не создать"); return None
    matidx = {}
    for arr, items in d0["materials"].items():
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    matidx[it["id"]] = arr
    idmap, new_mats = {}, {}

    def dup_mat(old_id):
        if old_id in idmap:
            return idmap[old_id]
        arr = matidx.get(old_id)
        if not arr:
            return old_id
        src = next(m for m in d0["materials"][arr] if m.get("id") == old_id)
        m = copy.deepcopy(src); m["id"] = str(uuid.uuid4()).upper()
        idmap[old_id] = m["id"]; new_mats.setdefault(arr, []).append(m)
        return m["id"]

    seg = copy.deepcopy(template)
    seg["id"] = str(uuid.uuid4()).upper()
    seg["material_id"] = dup_mat(template["material_id"])
    seg["extra_material_refs"] = [dup_mat(r) for r in template.get("extra_material_refs", [])
                                  if matidx.get(r) != "drafts"]
    seg["clip"] = copy.deepcopy(template.get("clip", {})); seg["clip"]["alpha"] = 0.0   # прозрачный
    seg["desc"] = "DoF контроллер"
    seg["common_keyframes"], seg["keyframe_refs"] = [], []
    dur = max(1, int(w1 - w0))
    src_dur = int(template.get("source_timerange", {}).get("duration", dur) or dur)
    seg["target_timerange"] = {"start": int(w0), "duration": dur}
    seg["source_timerange"] = {"start": 0, "duration": min(dur, src_dur)}
    set_curve(seg, "KFTypePositionX", [(0, 0.0), (dur, 0.0)])   # px нужен для матча по пути в hook0
    # Стартовые значения — с ползунков панели, а не единицы. При 1.0/1.0 диафрагма выходит
    # максимальной, дальние слои сразу упираются в кламп и на ползунок не реагируют — снаружи
    # это «ничего не работает» (замер 2026-08-09). Дальше рулит сам слой, ключами.
    set_curve(seg, "KFTypeScaleX", [(0, aperture), (dur, aperture)])   # ДИАФРАГМА (горизонталь)
    set_curve(seg, "KFTypeScaleY", [(0, focus), (dur, focus)])         # ГЛУБИНА, плоскость фокуса
    seg["uniform_scale"] = {"on": False, "value": 1.0}          # X/Y НЕЗАВИСИМЫ
    png = _make_dof_png(cw, ch)
    for items in new_mats.values():
        for m in items:
            if m["id"] == seg["material_id"]:
                m["material_name"] = "DoF контроллер"
                m["type"] = "photo"; m["path"] = png; m["media_path"] = png
                m["has_audio"] = False; m["width"] = int(cw); m["height"] = int(ch)
                m["duration"] = 10800000000
    new_track = {"attribute": 0, "flag": 0, "id": str(uuid.uuid4()).upper(),
                 "is_default_name": False, "name": "DoF контроллер", "segments": [seg], "type": "video"}
    written = 0
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(path)
        d["tracks"] = [t for t in d["tracks"] if t.get("name") != "DoF контроллер"]   # убрать старые
        for arr, items in new_mats.items():
            d["materials"].setdefault(arr, []).extend(copy.deepcopy(items))
        d["tracks"].append(copy.deepcopy(new_track))
        shutil.copy2(path, path + ".predoflayer.bak")
        with open(path, "wb") as fp:
            fp.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        written += 1
    log(f"DoF-слой вживлён: seg={seg['id'][:8]}, файлов {written}")
    return seg["id"]


def set_dof_rig_line(dof_id, dof_path):
    """Установить ГЛОБАЛЬНУЮ строку DOF в ve_rig.txt (один слой-контроллер на все сцены), сохранив все блоки сцен."""
    header, _dof, blocks = _rig_parse()
    _rig_write(header, f"DOF {dof_id} {dof_path}", blocks)


def _cam_kf(cam_seg, prop):
    """Ключи свойства слоя-камеры: [(time_offset, value, keyframe_dict)] (keyframe_dict несёт тип кривой)."""
    for c in cam_seg.get("common_keyframes", []):
        if c["property_type"] == prop:
            return [(k["time_offset"], k["values"][0], k) for k in c["keyframe_list"]]
    return []


def commit_camera(proj_dir, layer_ids, depths, want_scale=True, log=print):
    """ЗАФИКСИРОВАТЬ: прочитать ключи слоя «Камера 3D» (юзер правил в CapCut), инвертить в путь камеры,
    спроецировать на слои-объекты и записать РЕАЛЬНЫЕ документные ключи (inject_curves — на таймлайне,
    переживают сохранение/экспорт). Знаки/проекция ТОЧЬ-в-точь как live-drive дилиба (cx=−px, cy=+py, cz).
    Ключи объектов ложатся на ВРЕМЕНА ключей камеры (3 ключа камеры → 3 на слое) с их типом кривой (easing)."""
    import camera3d
    if isinstance(layer_ids, str):
        layer_ids = [layer_ids]
    depths = list(depths) + [0.5] * max(0, len(layer_ids) - len(depths))
    name = os.path.basename(proj_dir)
    was_open = _project_open(proj_dir)
    _t0 = time.time()
    write_progress(5, "Читаю ключи камеры…")
    # require_closed, а НЕ close_to_home: та пропускает работу, когда _project_open() говорит
    # «закрыт», а тот основан на ОТСУТСТВИИ улик и врёт (свежий проект без .locked, unlink'нутый
    # .locked после pkill). Тогда inject_curves пишет в файл, который CapCut держит, и автосейв
    # стирает ключи молча. Здесь нужно ПОЛОЖИТЕЛЬНОЕ доказательство закрытости (is_home).
    # Побочно делает то же, что делала close_to_home: сохраняет правки камеры на диск.
    require_closed(proj_dir, log)
    draft = read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    cam_seg = next((s for t in draft["tracks"] if t.get("name") == "Камера 3D"
                    for s in t.get("segments", [])), None)
    if not cam_seg:
        raise ValueError("слой «Камера 3D» не найден — сначала запеки камеру")
    px = _cam_kf(cam_seg, "KFTypePositionX")
    py = _cam_kf(cam_seg, "KFTypePositionY")
    sx = _cam_kf(cam_seg, "KFTypeScaleX")
    if not px:
        raise ValueError("на слое камеры нет ключей позиции — подвигай камеру и поставь ключи")

    def interp(kfs, t_src):   # значение свойства камеры в момент t_src (source-µs камеры)
        if not kfs:
            return None
        if t_src <= kfs[0][0]:
            return kfs[0][1]
        if t_src >= kfs[-1][0]:
            return kfs[-1][1]
        for i in range(1, len(kfs)):
            if t_src <= kfs[i][0]:
                a, b = kfs[i - 1], kfs[i]
                u = (t_src - a[0]) / max(1, b[0] - a[0])
                return a[1] + (b[1] - a[1]) * u
        return kfs[-1][1]

    f = camera3d.FOCAL_FRAC * ch
    hw, hh, Z, SY = cw / 2.0, ch / 2.0, camera3d.Z_REF, camera3d.SIGN_Y
    # путь камеры: каждый ключ px камеры -> (timeline-время, инверсия в cx/cy/cz + тип кривой этого ключа)
    path = []
    for t_src, pxv, kf in px:
        t_tl = to_timeline(cam_seg, t_src)
        pyv = interp(py, t_src) or 0.0
        sxv = interp(sx, t_src) or 1.0
        path.append({"t_tl": t_tl, "kf": kf,
                     "cx": -pxv * Z * hw / f, "cy": pyv * Z * hh / f, "cz": Z * (1.0 - 1.0 / max(0.05, sxv))})
    write_progress(45, "Проецирую на объекты…")
    curves = {}
    for lid, dp in zip(layer_ids, depths):
        seg = by_id.get(lid)
        if not seg:
            continue
        tr = seg["clip"]["transform"]
        bx, by = tr["x"] * cw / 2.0, SY * tr["y"] * ch / 2.0
        base, z = seg["clip"]["scale"]["x"], camera3d.depth_to_z(dp)
        st, dur = seg["target_timerange"]["start"], seg["target_timerange"]["duration"]
        xs, ys, ss = [], [], []
        for p in path:
            if not (st <= p["t_tl"] <= st + dur):
                continue
            t_o = kf_time(seg, p["t_tl"])   # timeline -> source-время ЭТОГО слоя
            d = max(z - p["cz"], 1e-3)
            xs.append((t_o, (bx * z - f * p["cx"]) / d / hw, p["kf"]))
            ys.append((t_o, SY * (by * z - f * p["cy"]) / d / hh, p["kf"]))
            ss.append((t_o, base * z / d, p["kf"]))
        if xs:
            curves[lid] = (xs, ys, ss if want_scale else None, None)
    nkf = sum(len(c[0]) for c in curves.values())
    write_progress(90, "Пишу ключи на объекты…")
    written = inject_curves(proj_dir, curves)
    if was_open or capcut_ui.is_running():
        write_progress(97, "Открываю проект…")
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001
            log(f"не открыл ({e}) — открой вручную")
    write_progress(100, "Готово")
    log(f"зафиксировано: {len(curves)} слоёв, {nkf} ключей, {written} файлов, {time.time() - _t0:.0f}с")
    return {"layers": len(curves), "keyframes": nkf, "files": written}


def restore_camera_layer(proj_dir):
    """Откат add_camera_layer: вернуть все draft_info.json из .precam.bak. Число восстановленных."""
    n = 0
    for bak in [os.path.join(proj_dir, "draft_info.json.precam.bak")] + \
            glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json.precam.bak")):
        if os.path.exists(bak):
            shutil.copy2(bak, bak[:-len(".precam.bak")]); n += 1
    return n


# Плейсхолдер корня проекта в путях материалов CapCut (снят с их же draft_info.json).
PLACEHOLDER_GUID = "0E685133-18CE-45ED-8CB8-2904A212EC80"
VE_CURVE_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_curve.txt")
VE_RENDER_STATUS_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_render_status.txt")
VE_KFREQ_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_kfreq.txt")
VE_CORNERPIN_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_cornerpin.txt")
VE_LOG_PATH = os.path.expanduser("~/Movies/CapCut/User Data/plugin_probe.log")
VE_NO_LIVECMD_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_no_livecmd.txt")
VE_TEXTBOX_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_textbox.txt")
VE_TEXTBOX_OUT_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_textbox_out.txt")


def _log_wait(needle, since, timeout=8.0):
    """Ждём строку в логе дилиба. Лог — единственный обратный канал из инжекта в Python.

    Шаг 10 мс, а не 200: на живой камере этих ожиданий два на каждое движение ползунка, и
    крупный шаг добавлял ~100 мс к задержке превью ни за что (замер 2026-08-09: 614 мс на тик
    против 505 мс, которые видел замерочный скрипт со своим шагом 5 мс). Читаем хвост с `since`,
    так что опрос стоит копейки."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(VE_LOG_PATH, errors="ignore") as f:
                f.seek(since)
                if needle in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.01)
    return False


def _log_size():
    try:
        return os.path.getsize(VE_LOG_PATH)
    except OSError:
        return 0


def live_sync(seg, prop, t_src, log=print):
    """ЖИВОЙ СИНК ДОКУМЕНТ→РЕНДЕР вместо переоткрытия проекта: одна родная команда
    `addCommonKeyframe` на плейхеде. Порядок принципиален — ключи уже должны лежать в треке
    документа (их туда кладёт дилиб по ve_curve.txt): команда значения НЕ несёт, движок берёт
    его из трека и синкает рендер сам.

    Замерено 2026-08-08 на треке в 299 ключей (стенд TRACKING, костыль пуша в TEClip выключен):
      · ОДНА команда тянет ВЕСЬ трек (рендер получил кривую 14 КБ), а не ключ на плейхеде;
      · и ВСЕ свойства сегмента разом (px/py/sx/rz), а не только запрошенное — выстрел один;
      · `play_head` — время ТАЙМЛАЙНА, движок сам считает `kf_time`: ключ, посланный на 1503355
        при tgt.start=500000/src.start=300000, лёг ровно на 1303355.
    Поэтому шлём время ключа, обратно переведённое в таймлайн: снимок садится НА наш ключ и
    лишнего не создаёт. Кил-свитч ve_no_livecmd.txt возвращает старое переоткрытие."""
    if os.path.exists(VE_NO_LIVECMD_PATH):
        return False
    ph = int(round(to_timeline(seg, t_src)))
    js = ('{"commit_immediately":true,"params":{"seg_id":"%s","keyframeType":"%s",'
          '"dataParam":{"play_head":%d,"curveType":"Line"}}}' % (seg["id"], prop, ph))
    since = _log_size()
    with open(VE_KFREQ_PATH, "w") as f:
        f.write(f"addCommonKeyframe {seg['id']} {ph}\n{js}\n")
    ok = _log_wait("MKREQ отправлена api=addCommonKeyframe", since)
    # Файл убираем ОБЯЗАТЕЛЬНО: гейт стреляет на mtime, и оставленный выстрел повторится
    # при следующем открытии ЛЮБОГО проекта — по протухшему id, вслепую.
    try:
        os.remove(VE_KFREQ_PATH)
    except OSError:
        pass
    log(f"живой синк рендера: {'ок' if ok else 'команда не ушла'} ({prop} @ {ph})")
    return ok


def _live_prop(cur):
    """Чем стрелять: берём ТОЛЬКО реально записанное свойство. Выстрел по пустому создал бы
    одинокий ключ на свойстве, которого трек не трогал. Какое именно — не важно, движок всё
    равно синкает весь сегмент.

    cur — либо кортеж обычного трека (xs, ys, ss, rs), либо словарь планарного
    {KFTypeCornerPin*: [(t, [x, y]), …]}."""
    if isinstance(cur, dict):                       # планарный трек: четыре трека углов
        for name in CORNER_PROPS:
            if cur.get(name):
                return name, cur[name]
        return None, None
    xs, _ys, ss, rs = cur
    for name, pairs in (("KFTypePositionX", xs), ("KFTypeScaleX", ss), ("KFTypeRotation", rs)):
        if pairs:
            return name, pairs
    return None, None


def write_live_corners(tid, corners, path=VE_CURVE_PATH, log=print):
    """Четыре трека углов в ve_curve.txt для дилиба: «<время> <x> <y>» — пара, ключ corner-pin.
    Обычные кривые пишутся тем же файлом одним числом (write_live_curve), парсер дилиба
    различает их по числу разобранных величин."""
    lines = [f"SEGMENT {tid}"]
    for prop in CORNER_PROPS:
        pairs = corners.get(prop)
        if not pairs:
            continue
        lines.append(prop)
        lines += [f"{int(round(t))} {float(v[0]):.6f} {float(v[1]):.6f}" for t, v in pairs]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"LIVE ve_curve.txt (углы): {len(lines)} строк → {path}")
    return len(lines)


def live_cornerpin(seg, corners, log=print):
    """ЖИВОЙ ПЛАНАРНЫЙ ТРЕК: углы едут в ОТКРЫТЫЙ проект, без переоткрытия.

    Порядок обязателен и проверен:
      1. `ve_cornerpin.txt` — дилиб зовёт cp_apply, тот включает `enable_corner_pin`.
         БЕЗ включённого канала ключи углов мертвы: движок рендерит слой по clip и молча
         игнорирует углы. Делаем это ДО записи треков — пока ключей нет, cp_sync_curves
         внутри дилиба выходит сразу и не подмешивает статичные углы в наш трек.
      2. `ve_curve.txt` — четыре секции KFTypeCornerPin*, дилиб заливает их в трек документа.
      3. одна `addCommonKeyframe` — движок снимает уже наше значение и синкает рендер целиком.

    Кривые пишем ЛИНЕЙНЫМИ (curveType Line): парный cubic на углах роняет CapCut.
    Возврат False = живой путь не вышел, зовущий откатывается на файлы драфта + переоткрытие."""
    if os.path.exists(VE_NO_LIVECMD_PATH):
        return False
    prop, pairs = _live_prop(corners)
    if not prop:
        return False
    first = []                                   # 8 величин: UL.x UL.y UR.x UR.y DL.x DL.y DR.x DR.y
    for p in CORNER_PROPS:
        v = corners.get(p)
        first += [float(v[0][1][0]), float(v[0][1][1])] if v else [0.0, 0.0]

    since = _log_size()
    with open(VE_CORNERPIN_PATH, "w") as f:
        f.write(seg["id"] + " " + " ".join(f"{v:.6f}" for v in first) + "\n")
    applied = _log_wait("CORNERPIN applied=", since, timeout=15.0)
    # Файл убираем: гейт стреляет на mtime, и оставленный набор углов применится ЗАНОВО при
    # следующем открытии проекта — статичные углы затрут ключ на плейхеде уже хорошего трека.
    try:
        os.remove(VE_CORNERPIN_PATH)
    except OSError:
        pass
    if not applied:
        log("живой планарный: канал углов не включился")
        return False

    since = _log_size()
    write_live_corners(seg["id"], corners, log=log)
    ok = _log_wait("AUTO created", since)
    # Гейт снимаем ПОСЛЕ подтверждения от дилиба. Оставленный `ve_curve.txt` стреляет на
    # открытии ЛЮБОГО проекта и заливает треки углов по протухшему id вслепую — ровно та же
    # грабля, что уже закрыта у `ve_cornerpin.txt` и `ve_kfreq.txt`. Ловил живьём: после
    # сквозного 3D-трека файл оставался лежать.
    try:
        os.remove(VE_CURVE_PATH)
    except OSError:
        pass
    if not ok:
        log("живой планарный: дилиб не залил треки углов")
        return False

    return live_sync(seg, prop, pairs[len(pairs) // 2][0], log=log)


def _render_pushed(timeout=6.0):
    """Сколько свойств дилиб реально протолкнул в рендер после живой записи.
    Файл пишет render_sync; ждём его появления, т.к. дилиб опрашивает гейт ~раз в 300мс.
    -1 = не дождались (дилиба нет / рендер-часть выключена ve_no_render.txt) — тогда не лезем."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(VE_RENDER_STATUS_PATH) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            time.sleep(0.2)
    return -1


def _reopen_project(proj_dir, name, tries=2, log=print):
    """Мгновенный авто-перезаход после трека: сохранить и открыть проект заново.
    Нужен потому, что рендер не видит наших живых ключей (живой путь не найден: см. память
    capcut-preview-refresh), а на ЗАГРУЗКЕ CapCut синкает документ в рендер сам.
    Проверяем ФАКТ по .locked, а не по возврату UI-автоматики: она может отработать «успешно»,
    а проект не открыться (окно потеряло фокус, всплывашка перехватила клик)."""
    lock = os.path.join(proj_dir, ".locked")
    for attempt in range(1, tries + 1):
        try:
            if os.path.exists(lock):                      # открыт -> закрываем (go_home = сохранение)
                write_progress(97, "Сохраняю проект…")
                capcut_ui.go_home()
                for _ in range(100):                      # ждём снятия блокировки: до 30с
                    if not os.path.exists(lock):
                        break
                    time.sleep(0.3)
                if os.path.exists(lock):
                    log(f"попытка {attempt}: проект не закрылся"); continue
            write_progress(98, "Открываю проект…")
            capcut_ui.reopen(name, lock)
            for _ in range(60):                           # ждём, что реально открылся
                if os.path.exists(lock):
                    log("проект переоткрыт — превью актуально")
                    return True
                time.sleep(0.3)
            log(f"попытка {attempt}: проект не открылся")
        except Exception as e:  # noqa: BLE001 — ключи уже записаны; UI-сбой не должен терять трек
            log(f"попытка {attempt}: сбой автоматики ({e})")
        time.sleep(1.0)
    log("не удалось переоткрыть автоматически — открой проект вручную (ключи уже записаны)")
    return False
VE_PROGRESS_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_progress.txt")
# ВЫБОР ПОВЕРХНОСТИ ПО УГЛАМ. Гейт (наличие файла = режим включён) несёт стартовый квад, дилиб
# рисует по нему 4 ручки поверх превью и на каждый драг переписывает второй файл. Оба — 8 чисел:
# UL,UR,DL,DR в ДОЛЯХ КАНВАСА (0..1, y вниз), чтобы дилибу не нужно было знать размер канваса.
VE_SURFPICK_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_surfpick.txt")
VE_SURFQUAD_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_surfquad.txt")


def surface_pick(on, quad_frac=None):
    """Включить/выключить режим выбора поверхности. quad_frac — стартовый квад в долях канваса."""
    if not on:
        for p in (VE_SURFPICK_PATH, VE_SURFQUAD_PATH):
            try:
                os.remove(p)
            except OSError:
                pass
        return {"on": False}
    q = quad_frac or [(0.35, 0.30), (0.65, 0.30), (0.35, 0.70), (0.65, 0.70)]
    with open(VE_SURFPICK_PATH, "w") as f:
        f.write(" ".join("%.5f" % v for p in q for v in p) + "\n")
    return {"on": True, "quad": q}


def surface_quad():
    """Квад, который пользователь натянул ручками. None — ещё не трогал (или режим выключен)."""
    for p in (VE_SURFQUAD_PATH, VE_SURFPICK_PATH):    # ещё не двигал -> отдаём стартовый
        try:
            with open(p) as f:
                v = [float(x) for x in f.read().split()]
            if len(v) >= 8:
                return [(v[i], v[i + 1]) for i in (0, 2, 4, 6)]
        except (OSError, ValueError):
            continue
    return None
VE_RIG_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_rig.txt")


def _rig_layer_line(L):
    line = (f"LAYER {L['id']} {L['z']:.6f} {L['bx']:.3f} {L['by']:.3f} {L['base_scale']:.6f} "
            f"{L['base_rot']:.6f} {int(L['tstart'])} {int(L['sstart'])} {L['speed']:.6f} {L['path']}")
    # Наклон плоскости — ОТДЕЛЬНОЙ строкой: у LAYER в хвосте путь, а он может содержать пробелы,
    # дописать числа туда нельзя. Ноль не пишем вовсе, чтобы риг не пух на обычных слоях.
    tx, ty = float(L.get("tiltx") or 0.0), float(L.get("tilty") or 0.0)
    if tx or ty:
        line += f"\nTILT {L['id']} {tx:.4f} {ty:.4f}"
    return line


# ── МУЛЬТИКАМЕРА: ve_rig.txt = CANVAS/PROJECT + один глоб. DOF + N блоков (CAMERA + её LAYER'ы). Ключ блока = cam_png ──
def _rig_parse():
    """ve_rig.txt -> (header[CANVAS/PROJECT], dof_line|None, blocks[[cam_png, [строки блока]]]).
    Блок сцены = строка CAMERA + её LAYER'ы до следующей CAMERA."""
    header, dof, blocks, cur = [], None, [], None
    try:
        lines = open(VE_RIG_PATH, encoding="utf-8").read().splitlines()
    except OSError:
        return header, dof, blocks
    for l in lines:
        if l.startswith("CAMERA "):
            toks = l.split(None, 3); png = toks[3].strip() if len(toks) >= 4 else ""
            cur = [l]; blocks.append([png, cur])
        elif (l.startswith(("LAYER ", "TILT ", "PAN ", "CAMEASE "))) and cur is not None:
            cur.append(l)   # всё, что принадлежит сцене, живёт в ЕЁ блоке (иначе мерж теряет у чужих)
        elif l.startswith("DOF "):
            dof = l
        elif l.startswith("CANVAS ") or l.startswith("PROJECT "):
            header.append(l)
    return header, dof, blocks


def _rig_write(header, dof, blocks):
    out = list(header)
    if dof:
        out.append(dof)
    for png, blk in blocks:
        out += blk
    with open(VE_RIG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def _rig_set_keepin(on):
    """Переписать только строку KEEPIN в шапке рига, не трогая сцены."""
    header, dof, blocks = _rig_parse()
    header = [l for l in header if not l.startswith("KEEPIN ")] + [f"KEEPIN {1 if on else 0}"]
    _rig_write(header, dof, blocks)


def rig_upsert_scene(cw, ch, name, cam_id, cam_w0, cam_png, layers, log=print, keepin=None, pan=None,
                     cam_ease=None, roll=None):
    """Вставить/ЗАМЕНИТЬ блок ОДНОЙ сцены (ключ = cam_png), СОХРАНив блоки других сцен (мерж, не перезатирание).
    keepin: «не выходить за рамку» (None = не менять текущее состояние в файле)."""
    prev_keep, dof, blocks = None, None, None
    _, dof, blocks = _rig_parse()
    if keepin is None:   # сохранить то, что уже стояло: header переписывается целиком
        try:
            with open(VE_RIG_PATH, encoding="utf-8") as f:
                prev_keep = next((l.split()[1] for l in f if l.startswith("KEEPIN ")), None)
        except OSError:
            prev_keep = None
        keepin = prev_keep == "1"
    header = [f"CANVAS {int(cw)} {int(ch)}", f"PROJECT {name}", f"KEEPIN {1 if keepin else 0}"]   # header всегда свежий
    block = [f"CAMERA {cam_id} {int(cam_w0)} {cam_png}"]
    # PAN <вкл> <градусов на единицу панорамы> — камера облетает сцену вместо простого сдвига
    if pan and float(pan.get("gain") or 0) > 0:
        import camera3d
        pv = pan.get("pivot")
        zp = camera3d.depth_to_z(float(pv)) if pv is not None else 0.0   # 0 = считать по слоям сцены
        block.append(f"PAN 1 {float(pan['gain']):.3f} {zp:.4f}")
    # CAMROLL <градусы> — завал горизонта всей сцены. Отдельная строка, а не поворот клипа камеры:
    # драйв читает ролл только из кривой ключей rz, статический поворот клипа он не смотрит.
    if roll:
        block.append(f"CAMROLL {float(roll):.4f}")
    if cam_ease:   # ручки кривой камеры из документа — стабильный источник easing для перспективы
        import camera
        block += camera._rig_camease_lines(cam_ease)
    block += [_rig_layer_line(L) for L in layers]
    blocks = [b for b in blocks if b[0] != cam_png]              # выкинуть старый блок этой сцены
    blocks.append([cam_png, block])
    _rig_write(header, dof, blocks)
    log(f"rig: сцена {cam_id[:8]} ({len(layers)} слоёв), всего сцен {len(blocks)}")


def rig_append_to_scene(cam_png, new_layers, log=print):
    """Дописать LAYER-строки в блок сцены cam_png (дедуп по пути ВНУТРИ блока), не трогая другие сцены."""
    header, dof, blocks = _rig_parse()
    new_paths = {L["path"] for L in new_layers}
    for b in blocks:
        if b[0] == cam_png:
            b[1] = [ln for ln in b[1] if not (ln.startswith("LAYER ") and ln.split(None, 10)[-1].strip() in new_paths)]
            b[1] += [_rig_layer_line(L) for L in new_layers]
            _rig_write(header, dof, blocks)
            log(f"rig: +{len(new_layers)} слоёв в сцену")
            return True
    log("rig: сцена не найдена — слои не дописаны")
    return False


# ── СЦЕНА: сохранённый сетап тула (слои по ПУТИ + глубины, путь камеры, DoF) для перезагрузки ──
SCENE_DIR = os.path.expanduser("~/Movies/CapCut/User Data/plugin_scenes")


def _scene_path(proj_dir):
    return os.path.join(SCENE_DIR, os.path.basename(proj_dir) + ".json")


def seg_object_path(draft, seg, _depth=0):
    """Путь исходника объекта: РЕКУРСИВНО сквозь ВЛОЖЕННЫЕ компаунды до самого внутреннего видео.
    Путь стабилен через заворот (id меняется, путь — нет), поэтому по нему и ключим сцену/риг.
    Компаунд-в-компаунде (добавление уже-компаунда): спускаемся до реального исходника."""
    mats = draft.get("materials", {})
    draft_ids = {m["id"]: m for m in mats.get("drafts", []) or [] if isinstance(m, dict)}
    for r in seg.get("extra_material_refs", []):
        m = draft_ids.get(r)
        if m and isinstance(m.get("draft"), dict) and _depth < 6:
            inner = m["draft"]
            for t in inner.get("tracks", []):
                if t.get("type") == "video":
                    for s2 in t.get("segments", []):
                        p = seg_object_path(inner, s2, _depth + 1)   # вложенный сегмент может сам быть компаундом
                        if p:
                            return p
            break   # компаунд без внутреннего видео (текст/эффект) → падаем на СОБСТВЕННЫЙ путь proxy (ручка, если есть)
    mid = seg.get("material_id")   # прямой материал ИЛИ ручка-путь proxy беспутного компаунда
    for kind in ("videos", "photos"):
        for m in mats.get(kind, []) or []:
            if m.get("id") == mid:
                return (m.get("path") or m.get("media_path") or "").strip() or None
    return None


def seg_object_id(draft, seg, _depth=0):
    """id сегмента, который РЕАЛЬНО несёт объект: сквозь сборные клипы до внутреннего видео.

    Зачем отдельно от seg_object_path: дилиб держит слои по id, и драйв (параллакс, corner-pin,
    блюр) должен адресовать именно ВНУТРЕННИЙ сегмент — у внешнего прокси нет ни исходника, ни
    трансформа объекта. Пока риг строился ДО заворачивания, это выходило само собой: сегменты
    сохраняли свои id, переезжая внутрь компаунда. А если пересоздать камеру ПОСЛЕ заворачивания,
    в риг попадали id прокси, дилиб не находил по ним ничего и писал «DRIVE wrote 0/3 layers» —
    параллакс и глубина резкости молча умирали. Зеркалит логику seg_object_path.
    """
    mats = draft.get("materials", {})
    draft_ids = {m["id"]: m for m in mats.get("drafts", []) or [] if isinstance(m, dict)}
    for r in seg.get("extra_material_refs", []):
        m = draft_ids.get(r)
        if m and isinstance(m.get("draft"), dict) and _depth < 6:
            inner = m["draft"]
            for t in inner.get("tracks", []):
                if t.get("type") == "video":
                    for s2 in t.get("segments", []):
                        got = seg_object_id(inner, s2, _depth + 1)
                        if got:
                            return got
            break   # компаунд без внутреннего видео — остаёмся собой
    return seg.get("id")


def _is_cam_track(name):
    return bool(name) and name.startswith("Камера 3D")


def cam_name_of_track(draft, track):
    """Эффективное имя камеры дорожки. CapCut МОЖЕТ СБРОСИТЬ track['name'] в пустую строку
    (поймано живьём: подпись «Камера 3D · <tok>» осталась только в материале сегмента, а имя
    самой дорожки стало ''). Тогда детект по имени дорожки врал «камеры нет», хотя клип на месте
    и движок его драйвит (он матчит по пути PNG, не по имени). Поэтому: нет имени у дорожки —
    берём имя материала первого сегмента, оно уникально на сцену и стабильно.
    Возвращает '' для НЕ-камерной дорожки."""
    name = (track.get("name") or "").strip()
    if name.startswith("Камера 3D"):
        return name
    for s in (track.get("segments") or []):
        _, mat = material_index(draft).get(s.get("material_id"), (None, {}))
        mn = (mat.get("material_name") or "").strip()
        if mn.startswith("Камера 3D"):
            return mn
        d = (s.get("desc") or "").strip()   # запасной канал: desc сегмента мы тоже пишем
        if d.startswith("Камера 3D"):
            return d
        break   # камера всегда односегментна: чужой первый сегмент = не камера
    return ""


def _scene_tok_of_track(name):
    """«Камера 3D · <tok>» -> tok (или '' для старого «Камера 3D»)."""
    return name.split("·", 1)[1].strip() if name and "·" in name else ""


def _tok_from_png(cam_png):
    """cam3d_WxH_<tok>.png -> tok (или '' для старого cam3d_WxH.png)."""
    base = os.path.basename(cam_png or "")
    if base.startswith("cam3d_") and base.endswith(".png"):
        core = base[6:-4]
        if "_" in core:
            return core.split("_", 1)[1]
    return ""


# Сайдкар правят из нескольких мест (панель, спираль в шапке, трекинг) и сервер многопоточный,
# так что «прочитал → поправил → записал» надо держать целиком под замком: иначе две правки
# наезжают друг на друга и одна теряется.
SCENE_LOCK = threading.RLock()


def _scene_load_side(proj_dir):
    fp = _scene_path(proj_dir)
    if os.path.exists(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def _scene_write_side(proj_dir, data):
    """Сайдкар сцен — АТОМАРНО. Здесь лежат привязки дорожек и глубины, то есть вся ручная
    работа пользователя по сцене. Прямой open(...,"w") обрезает файл до записи, и параллельный
    читатель (панель опрашивает состояние каждые 2 секунды) получал пустоту: _scene_load_side
    ловит ValueError и молча отдаёт {}. Дальше первый же _save_scene писал этот {} поверх —
    привязки исчезали без единого сообщения. Поймано живьём."""
    try:
        os.makedirs(SCENE_DIR, exist_ok=True)
        p = _scene_path(proj_dir)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def scene_save(proj_dir, tok, cam_id, layers, camKfs=None, dof=None):
    """Апсертнуть ОДНУ сцену (по токену) в сайдкар {scenes:{tok:{camId,layers,camKfs,dof}}}."""
    with SCENE_LOCK:
        data = _scene_load_side(proj_dir)
        data.setdefault("scenes", {})[tok] = {"camId": cam_id, "layers": layers,
                                              "camKfs": camKfs or [], "dof": dof or {}}
        _scene_write_side(proj_dir, data)


def scene_add_layers(proj_dir, tok, layers):
    """Дописать слои (по пути) в сцену tok сайдкара (дедуп по пути)."""
    with SCENE_LOCK:
        data = _scene_load_side(proj_dir)
        sc = data.setdefault("scenes", {}).setdefault(tok, {"camId": "", "layers": [], "camKfs": [], "dof": {}})
        add = {L["path"] for L in layers}
        sc["layers"] = [L for L in sc.get("layers", []) if L.get("path") not in add] + layers
        _scene_write_side(proj_dir, data)


def _rig_scene_depths(proj_dir):
    """{tok: {путь: глубина 0..1}} из блоков ve_rig.txt (если риг для ЭТОГО проекта). tok — из имени cam_png.
    Риг — источник правды глубин дилиба; даёт восстановить сцены даже без сайдкара."""
    import camera3d
    header, _dof, blocks = _rig_parse()
    proj = next((h[8:].strip() for h in header if h.startswith("PROJECT ")), "")
    if proj != os.path.basename(proj_dir):
        return {}
    span = camera3d.FAR - camera3d.NEAR or 1.0
    out = {}
    for cam_png, blk in blocks:
        m = {}
        for ln in blk:
            if ln.startswith("LAYER "):
                toks = ln.split(None, 10)
                if len(toks) >= 11:
                    try:
                        z = float(toks[2])
                    except ValueError:
                        continue
                    m[toks[10].strip()] = max(0.0, min(1.0, (z - camera3d.NEAR) / span))
        out[_tok_from_png(cam_png)] = m
    return out


def _cam_seg_material(draft):
    vids = {m["id"]: m for m in draft["materials"].get("videos", []) or []}

    def mpath(s):
        m = vids.get(s.get("material_id"), {})
        return m.get("path", "") or m.get("media_path", "")
    return mpath


def load_scene(proj_dir):
    """ВСЕ сцены проекта — детект по КАМЕРА-СЕГМЕНТАМ (материал cam3d_*.png), группировка по токену PNG.
    Так работает и когда сцены на отдельных треках «Камера 3D · tok», и когда их РАЗМНОЖИЛИ на одном треке
    (дубли-копии — имена треков CapCut обнуляет). Объекты сцены — из блока рига по её токену.
    → {scenes:[{tok,camId,name,camKfs,dof,layers:[{id,path,depth,name}]}], hasCamera} или None если камер нет."""
    draft = read_draft(proj_dir)
    mpath = _cam_seg_material(draft)
    cam_segs = {}   # tok -> первый камера-сегмент этого токена
    cam_total = 0   # ВСЕГО камера-сегментов (больше числа сцен = дубли одной сцены на таймлайне)
    for t in draft.get("tracks", []):
        for s in t.get("segments", []):
            if os.path.basename(mpath(s)).startswith("cam3d"):
                cam_total += 1
                cam_segs.setdefault(_tok_from_png(mpath(s)), s)
    if not cam_segs:
        return None
    side = _scene_load_side(proj_dir).get("scenes", {})
    rig_by_tok = _rig_scene_depths(proj_dir)
    # путь объекта -> верхний сегмент (пропускаем служебные камера/DoF PNG)
    seg_by_objpath = {}
    for t in draft.get("tracks", []):
        if t.get("type") != "video":
            continue
        for s in t.get("segments", []):
            b = os.path.basename(mpath(s))
            if b.startswith("cam3d") or b.startswith("dof3d"):
                continue
            p = seg_object_path(draft, s)
            if p:
                seg_by_objpath.setdefault(p, s)
    scenes = []
    for tok, cseg in cam_segs.items():
        depth_by_path = dict(rig_by_tok.get(tok, {}))
        depth_by_path.update({L["path"]: L["depth"] for L in side.get(tok, {}).get("layers", []) if L.get("path")})
        layers = [{"id": (seg_by_objpath.get(p) or {}).get("id", ""), "path": p,
                   "depth": d, "name": os.path.basename(p or "")} for p, d in depth_by_path.items()]
        scenes.append({"tok": tok, "camId": cseg["id"], "name": "Камера 3D · " + (tok or "1"),
                       "camKfs": side.get(tok, {}).get("camKfs", []),
                       "dof": side.get(tok, {}).get("dof", {}), "layers": layers})
    # дубли: камера-сегментов БОЛЬШЕ, чем уникальных токенов → сцену размножили на таймлайне (можно разделить)
    return {"scenes": scenes, "hasCamera": True, "camSegs": cam_total, "hasDuplicates": cam_total > len(cam_segs)}


LIVE_POS_K = 1.0  # document-значение позиции = draft-значение * K. 1.0 = как обычный режим (тот верен;
# 0.5 давал вдвое меньший офсет -> follower «залезал» на объект). Калибровка живого пути: 0.5|1.0


TRACK3D_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track3d_worker.py")
_T3D = None          # долгоживущий процесс-солвер (pycolmap живёт ТОЛЬКО в нём)
_T3D_LOCK = threading.Lock()


def track3d_available():
    """Есть ли pycolmap — БЕЗ импорта: импорт затянул бы вторую libomp в наш процесс, а это
    ровно то, от чего мы уходим. find_spec модуль не исполняет."""
    import importlib.util
    return importlib.util.find_spec("pycolmap") is not None


def track3d_call(req, log=print, timeout=3600):
    """Запрос в процесс-солвер: pycolmap НЕ ДОЛЖЕН оказаться в этом процессе.

    torch (обычный трекер) и pycolmap несут каждый свою libomp, и второй импорт в одном
    процессе — `OMP: Error #15` и abort. Глушить это через KMP_DUPLICATE_LIB_OK нельзя:
    официально это «небезопасный обход», разрешающий молча выдавать неверные числа, а у нас
    на кону солв камеры — ошибку в нём по картинке не увидеть.

    Процесс ДОЛГОЖИВУЩИЙ: в нём остаётся решённая сцена для предпросмотра. Умер — поднимаем
    заново, а зовущий получает внятный отказ, а не молчание.
    """
    global _T3D
    with _T3D_LOCK:
        if _T3D is None or _T3D.poll() is not None:
            env = {k: v for k, v in os.environ.items() if k != "KMP_DUPLICATE_LIB_OK"}
            _T3D = subprocess.Popen([sys.executable, TRACK3D_WORKER],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    cwd=os.path.dirname(TRACK3D_WORKER), env=env,
                                    text=True, bufsize=1)
            log(f"3D: поднят процесс-солвер pid={_T3D.pid}")
        try:
            _T3D.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            _T3D.stdin.flush()
        except (BrokenPipeError, ValueError):
            _T3D = None
            raise ValueError("процесс-солвер закрылся — повтори операцию")
        while True:
            line = _T3D.stdout.readline()
            if not line:
                _T3D = None
                raise ValueError("процесс-солвер умер, не ответив (смотри лог сервера)")
            msg = json.loads(line)
            if "log" in msg:
                log(msg["log"])
                continue
            if "error" in msg:
                raise ValueError(msg["error"])
            return msg


def solve_quad_3d(path, quad_src, work, t0_us=0, t1_us=None, ref_us=None, obj=None, log=print):
    """`track3d.solve_quad`, посчитанный в процессе-солвере. Возврат тот же:
    (homos, times, quad_ref, meta). Прогресс пишет сам солвер — в тот же файл, что и мы."""
    import numpy as np

    r = track3d_call({"op": "solve_quad", "path": path, "quad": [list(p) for p in quad_src],
                      "work": work, "t0_us": t0_us, "t1_us": t1_us, "ref_us": ref_us,
                      "obj": obj, "progress_path": VE_PROGRESS_PATH}, log=log)
    return ([np.array(h, float) for h in r["homos"]], r["times"],
            np.array(r["quad_ref"], float), r["meta"])


def write_progress(pct, msg=""):
    """Единый прогресс для оверлея в CapCut И веб-морды (/api/progress). pct 0..100.
    Формат ve_progress.txt: '<pct> <msg>'. Пишем сразу на старте, чтобы бар появлялся
    мгновенно (модель CoTracker грузится ~10с — этот пробел и закрываем)."""
    try:
        with open(VE_PROGRESS_PATH, "w") as f:
            f.write(f"{int(max(0, min(100, pct)))} {msg}")
    except OSError:
        pass


def write_live_curve(curves_by_target, path=VE_CURVE_PATH, pos_k=LIVE_POS_K, log=print):
    """ЖИВАЯ запись через инъекцию: пишем ve_curve.txt (секции по property_type +
    строки '<time_us> <value>'), инъектнутый ve_hook.dylib читает и заливает в ОТКРЫТЫЙ
    проект. Один follower для v1 (первый непустой target). Позиция домножается на pos_k."""
    lines = []
    for _tid, (xs, ys, ss, rs) in curves_by_target.items():
        if not any((xs, ys, ss, rs)):
            continue
        lines.append(f"SEGMENT {_tid}")  # id сегмента follower'а — для автосоздания треков дилибом

        def sec(name, pairs, k=1.0):
            if not pairs:
                return
            lines.append(name)
            for t, v in pairs:
                lines.append(f"{int(round(t))} {v * k:.6f}")

        sec("KFTypePositionX", xs, pos_k)
        sec("KFTypePositionY", ys, pos_k)
        sec("KFTypeScaleX", ss or [])
        sec("KFTypeRotation", rs or [])
        break
    with open(path, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    log(f"LIVE ve_curve.txt: {len(lines)} строк → {path}")
    return len(lines)


# ---------- video ----------

def get_frame(proj_dir, seg_id, timeline_us=None, max_w=520):
    """JPEG bytes of a frame from a segment's source video. Returns (jpeg, orig_w, orig_h)."""
    import cv2

    draft = read_draft(proj_dir)
    seg = next((s for t in draft["tracks"] for s in t.get("segments", []) if s["id"] == seg_id), None)
    if not seg:
        raise ValueError("сегмент не найден")
    _, mat = material_index(draft)[seg["material_id"]]
    path = mat.get("path")
    if not path or not os.path.exists(path):
        raise ValueError("исходник видео не найден")
    if timeline_us is None:
        timeline_us = seg["target_timerange"]["start"]
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_MSEC, kf_time(seg, timeline_us) / 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError("не смог прочитать кадр")
    h, w = frame.shape[:2]
    scale = min(1.0, max_w / w)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes(), w, h


def _patch(cv2, frame, box):
    """Нормализованный 32x32 серый патч из рамки (для сравнения внешнего вида)."""
    x, y, w, h = (int(v) for v in box)
    crop = frame[max(0, y):max(0, y) + max(h, 1), max(0, x):max(0, x) + max(w, 1)]
    if crop.size == 0:
        return None
    return cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (32, 32)).astype("float32")


def _layer_matrix(clip, mw, mh, cw, ch, override=None):
    """Аффинная матрица материал->канвас по clip-трансформации CapCut.
    override: (tx, ty, scale, rot) в тех же единицах, чтобы задать позицию из тула."""
    fit = min(cw / mw, ch / mh)
    tfx = override[0] if override else clip["transform"]["x"]
    tfy = override[1] if override else clip["transform"]["y"]
    sc = override[2] if override else clip["scale"]["x"]
    rot = override[3] if override else clip.get("rotation", 0.0)
    sx = sy = fit * sc
    if not override:
        sy = fit * clip["scale"]["y"]
    r = math.radians(rot)
    cos, sin = math.cos(r), math.sin(r)
    l00, l01 = sx * cos, -sy * sin
    l10, l11 = sx * sin, sy * cos
    ccx = cw / 2 + tfx * cw / 2
    ccy = ch / 2 + SIGN_Y * tfy * ch / 2
    return [[l00, l01, ccx - (l00 * mw / 2 + l01 * mh / 2)],
            [l10, l11, ccy - (l10 * mw / 2 + l11 * mh / 2)]]


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
IMG_EXT = (".png", ".webp", ".tif", ".tiff", ".gif", ".bmp", ".jpg", ".jpeg", ".heic")
_probe_cache = {}


def _probe_video(path):
    """(есть_альфа, w, h) для видеофайла, кэшируется. Без ffprobe -> (False,0,0)."""
    if path not in _probe_cache:
        alpha, w, h = False, 0, 0
        if FFPROBE:
            import subprocess
            r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height,pix_fmt", "-of", "csv=p=0", path],
                               capture_output=True, text=True)
            try:
                p = r.stdout.strip().split(",")
                w, h, pf = int(p[0]), int(p[1]), p[2]
                alpha = any(a in pf for a in ("yuva", "rgba", "bgra", "argb", "abgr", "ya8", "ya16", "pal8"))
            except (ValueError, IndexError):
                pass
        _probe_cache[path] = (alpha, w, h)
    return _probe_cache[path]


def _video_frame_bgra(cv2, path, t_sec):
    """Кадр видео как BGRA. Для видео с альфой (стикеры .mov) — через ffmpeg (OpenCV альфу теряет)."""
    import numpy as np

    alpha, w, h = _probe_video(path)
    if alpha and w and h and FFMPEG:
        import subprocess
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t_sec:.4f}", "-i", path,
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                           capture_output=True)
        buf = np.frombuffer(r.stdout, np.uint8)
        if buf.size >= w * h * 4:
            return cv2.cvtColor(buf[:w * h * 4].reshape(h, w, 4), cv2.COLOR_RGBA2BGRA)
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_MSEC, t_sec * 1000)
    ok, img = cap.read()
    cap.release()
    return cv2.cvtColor(img, cv2.COLOR_BGR2BGRA) if ok else None


def _material_image(cv2, np, mats, kind, mat, seg, timeline_us, depth=0):
    """BGRA-картинка материала на данный момент. Тип определяем по РАСШИРЕНИЮ файла,
    т.к. CapCut хранит и картинки как материал типа 'videos' (иначе теряется альфа PNG).
    Сборный клип: сегмент 'videos' без файла, а вложенный драфт — в extra_material_refs (type=drafts)."""
    if depth <= 3:
        nested = mat.get("draft") if kind == "drafts" else None
        for rid in seg.get("extra_material_refs", []):
            rk, rm = mats.get(rid, (None, None))
            if rk == "drafts" and isinstance(rm.get("draft"), dict):
                nested = rm["draft"]
                break
        if isinstance(nested, dict):
            return _composite_draft(cv2, np, nested, kf_time(seg, timeline_us), depth=depth + 1)
    path = mat.get("path")
    if not path or not os.path.exists(path):
        return None  # текст / эффекты / без файла — не рендерим
    if os.path.splitext(path)[1].lower() in IMG_EXT:
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.ndim == 2:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        if img.shape[2] == 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        return img
    return _video_frame_bgra(cv2, path, kf_time(seg, timeline_us) / 1e6)


def _composite_draft(cv2, np, draft, timeline_us, exclude=None, depth=0):
    """Свести все слои драфта в BGRA-холст на момент timeline_us (straight-alpha over)."""
    mats = material_index(draft)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    acc = np.zeros((ch, cw, 4), np.float32)  # BGRA
    for track in draft.get("tracks", []):  # 0 = низ, рисуем снизу вверх
        for seg in track.get("segments", []):
            if seg["id"] == exclude:
                continue
            tr = seg["target_timerange"]
            if not (tr["start"] <= timeline_us < tr["start"] + tr["duration"]):
                continue
            kind, mat = mats.get(seg["material_id"], ("?", {}))
            img = _material_image(cv2, np, mats, kind, mat, seg, timeline_us, depth)
            if img is None:
                continue
            mh, mw = img.shape[:2]
            M = np.array(_layer_matrix(seg["clip"], mw, mh, cw, ch), np.float32)
            warp = cv2.warpAffine(img, M, (cw, ch), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0)).astype(np.float32)
            sa = (warp[:, :, 3] / 255.0) * seg["clip"].get("alpha", 1.0)
            da = acc[:, :, 3] / 255.0
            oa = sa + da * (1 - sa)
            denom = np.maximum(oa, 1e-6)[..., None]
            acc[:, :, :3] = (warp[:, :, :3] * sa[..., None] + acc[:, :, :3] * (da * (1 - sa))[..., None]) / denom
            acc[:, :, 3] = oa * 255
    return np.clip(acc, 0, 255).astype(np.uint8)


def render_composite(proj_dir, timeline_us, exclude=None, max_w=520):
    """Свести все слои проекта в один кадр (как рендерит CapCut), поверх чёрного.
    Читает прозрачность PNG/альфа-видео, рендерит сборные клипы рекурсивно.
    Пропускает текст/эффекты (нет файла). Возвращает (jpeg, canvas_w, canvas_h)."""
    import cv2
    import numpy as np

    draft = read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    acc = _composite_draft(cv2, np, draft, timeline_us, exclude=exclude)
    a = acc[:, :, 3:4].astype(np.float32) / 255.0
    out = (acc[:, :, :3].astype(np.float32) * a).astype(np.uint8)  # поверх чёрного
    if cw > max_w:
        out = cv2.resize(out, (max_w, int(ch * max_w / cw)))
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes(), cw, ch


def _text_style(mat):
    """Грубый разбор текст-материала CapCut: (text, [r,g,b] 0..1 или None). Для превью-плейсхолдера."""
    try:
        c = json.loads(mat.get("content", "{}"))
        col = None
        st = ((c.get("styles") or [{}])[0])
        fill = (((st.get("fill") or {}).get("content") or {}).get("solid") or {}).get("color")
        if isinstance(fill, list) and len(fill) >= 3:
            col = fill[:3]
        return c.get("text", "") or "", col
    except (json.JSONDecodeError, TypeError, AttributeError, IndexError):
        return "", None


def layers_at(proj_dir, timeline_us, max_w=720):
    """Слои проекта на момент t как ОТДЕЛЬНЫЕ текстуры + трансформы — для КЛИЕНТСКОГО композита.
    Картинки/видео → BGRA PNG data-url (даунскейл до max_w px, трансформ считается по этим же mw/mh —
    масштаб на канвасе сохраняется). Текст/эффекты без файла → tex=None + text/color для JS-плейсхолдера.
    Порядок = снизу вверх (как рисует CapCut)."""
    import base64

    import cv2
    import numpy as np

    draft = read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    mats = material_index(draft)
    layers = []
    for track in draft.get("tracks", []):
        for seg in track.get("segments", []):
            tr = seg["target_timerange"]
            if not (tr["start"] <= timeline_us < tr["start"] + tr["duration"]):
                continue
            kind, mat = mats.get(seg["material_id"], ("?", {}))
            clip = seg.get("clip")
            if not clip:
                continue   # сегмент без clip (эффект-трек, напр. «мерцание») — не рендер-слой, пропускаем
            L = {"id": seg["id"], "kind": kind, "name": seg_name(kind, mat),
                 "tx": clip["transform"]["x"], "ty": clip["transform"]["y"],
                 "sx": clip["scale"]["x"], "sy": clip["scale"]["y"],
                 "rot": clip.get("rotation", 0.0), "alpha": clip.get("alpha", 1.0)}
            img = _material_image(cv2, np, mats, kind, mat, seg, timeline_us)
            if img is not None:
                mh, mw = img.shape[:2]
                if mw > max_w:
                    img = cv2.resize(img, (max_w, max(1, int(mh * max_w / mw))))
                    mh, mw = img.shape[:2]
                if img.shape[2] == 4 and bool((img[:, :, 3] == 255).all()):   # непрозрачный -> JPEG (в ~20 раз мельче PNG)
                    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_BGRA2BGR), [cv2.IMWRITE_JPEG_QUALITY, 86])
                    mime = "jpeg"
                else:                                                          # есть альфа -> PNG
                    ok, buf = cv2.imencode(".png", img)
                    mime = "png"
                L["tex"] = f"data:image/{mime};base64," + base64.b64encode(buf.tobytes()).decode()
                L["mw"], L["mh"] = mw, mh
            else:                                     # текст/эффект — плейсхолдер
                L["tex"] = None
                L["mw"] = mat.get("width") or cw // 3
                L["mh"] = mat.get("height") or ch // 8
                if kind == "texts":
                    L["text"], L["color"] = _text_style(mat)
            layers.append(L)
    return {"canvas": {"w": cw, "h": ch}, "layers": layers}


def track_video(path, t0_us, t1_us, roi=None, show=True):
    """CSRT-трек рамки в [t0,t1] (время источника). Возвращает
    ([(t, cx, cy, w, h)], meta): стоп по потере трекера или расхождению внешнего вида."""
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"не смог открыть видео: {path}")
    cap.set(cv2.CAP_PROP_POS_MSEC, t0_us / 1000)
    ok, frame = cap.read()
    if not ok:
        raise ValueError("не смог прочитать первый кадр")
    if roi is None:
        roi = cv2.selectROI("Выдели объект и нажми ENTER", frame, showCrosshair=True)
        cv2.destroyAllWindows()
        if roi[2] == 0 or roi[3] == 0:
            raise ValueError("рамка не выбрана")
    roi = tuple(int(round(v)) for v in roi)
    tracker = cv2.TrackerCSRT_create()
    tracker.init(frame, roi)
    ref = _patch(cv2, frame, roi)
    t = cap.get(cv2.CAP_PROP_POS_MSEC) * 1000 - 1e6 / max(cap.get(cv2.CAP_PROP_FPS), 1)
    points = [(max(t, t0_us), roi[0] + roi[2] / 2, roi[1] + roi[3] / 2, roi[2], roi[3])]
    bad, reason = 0, "конец диапазона"
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) * 1000
        if t > t1_us + US / 60:
            break
        ok, box = tracker.update(frame)
        if not ok:
            reason = "трекер потерял объект"
            break
        cur = _patch(cv2, frame, box)
        ncc = float(cv2.matchTemplate(cur, ref, cv2.TM_CCOEFF_NORMED)[0][0]) if cur is not None else 0.0
        if ncc < NCC_MIN:  # похоже на дрейф/перекрытие — пропускаем кадр, копим счётчик
            bad += 1
            if bad >= DRIFT_FRAMES:
                reason = "объект потерян/перекрыт"
                break
            continue
        bad = 0
        points.append((t, box[0] + box[2] / 2, box[1] + box[3] / 2, box[2], box[3]))
        if show:
            x, y, w, h = (int(v) for v in box)
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
            cv2.imshow("tracking (q = прервать)", cv2.resize(frame, None, fx=0.3, fy=0.3))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                reason = "прервано"
                break
    cap.release()
    if show:
        cv2.destroyAllWindows()
    covered = (points[-1][0] - t0_us) / max(t1_us - t0_us, 1)
    return points, {"reason": reason, "covered": max(0.0, min(1.0, covered)), "frames": len(points)}


# ---------- full cycle ----------

def _project_open(proj_dir):
    """Открыт ли проект в CapCut. .locked НЕНАДЁЖЕН (его могли unlink'нуть, пока CapCut держит
    дескриптор — тогда exists() врёт «закрыт»), поэтому ИЛИ файл на месте, ИЛИ редактор активен."""
    return os.path.exists(os.path.join(proj_dir, ".locked")) or capcut_ui.is_editing()


def close_to_home(proj_dir, log=print):
    lock = os.path.join(proj_dir, ".locked")
    if not _project_open(proj_dir):
        return
    log("закрываю проект на главную...")
    capcut_ui.go_home()
    for _ in range(20):
        # ждём РЕАЛЬНОГО закрытия: и файла нет, и редактор ушёл на главную
        if not os.path.exists(lock) and not capcut_ui.is_editing():
            break
        time.sleep(1)
    time.sleep(2)  # дать CapCut дописать сейв


def holds_project(proj_dir):
    """ПОЛОЖИТЕЛЬНЫЙ признак «CapCut держит ЭТОТ проект»: у процесса есть открытые дескрипторы
    внутри папки проекта (`.locked`, обложки сабдрафтов). Честнее AX-догадок сразу в обе стороны:

    · работает, когда сам `.locked` уже удалён из ФС — дескриптор остаётся за процессом;
    · не врёт «открыт», когда окно в переходе и меню недоступно;
    · не врёт «закрыт», когда проект закрыт, а плиток главной AX почему-то не отдаёт (на этом
      `is_home()` тормозил запись на уже закрытом проекте).

    Замер 2026-08-09: проект открыт → 4 дескриптора, закрыт → 0.
    Проверить не смогли → отвечаем «держит»: осторожная сторона, зовущий останется на AX-пути."""
    try:
        pids = subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True, text=True).stdout.split()
        if not pids:
            return False
        out = subprocess.run(["lsof", "-p", pids[0], "-Fn"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return os.path.join(os.path.abspath(proj_dir), "") in out


def require_closed(proj_dir, log=print):
    """Убедиться, что писать в драфт БЕЗОПАСНО, и закрыть проект, если он открыт.

    Зовут перед любой записью слоёв в draft_info.json. Отличие от close_to_home: та пропускает
    работу, когда _project_open() говорит «закрыт», а он основан на ОТСУТСТВИИ признаков —
    у свежесозданного проекта нет ни .locked, ни включённого пункта меню, и открытый проект
    выглядит закрытым. Поэтому здесь требуем ПОЛОЖИТЕЛЬНОГО доказательства (is_home) и молча
    не продолжаем: тихая запись в удерживаемый CapCut'ом файл теряется при его сохранении.
    """
    if not capcut_ui.is_running():
        return   # CapCut не запущен — файл ничей, писать безопасно
    close_to_home(proj_dir, log)
    for _ in range(12):
        # ⚠️ ЗДЕСЬ БЫЛО «ИЛИ», И ЭТО СТОИЛО КАМЕРЫ (2026-08-12). Условие звучало как
        # `is_home() or not holds_project()`: ОДНОГО совравшего признака хватало, чтобы пустить
        # запись в удерживаемый файл. Так и вышло — `.locked` снесли на старте CapCut, а
        # `holds_project` опирается на дескрипторы ВНУТРИ папки проекта, и без замка он ответил
        # «не держит» при открытом редакторе. Слой камеры лёг в драфт под открытым проектом,
        # на таймлайне не появился, а ближайший сейв CapCut его стёр.
        #
        # Теперь пишем, только когда МОЛЧАТ ВСЕ признаки «открыт», а не когда согласился один:
        # `holds_project` — дескрипторы процесса, `is_editing` — доступен ли пункт меню
        # «Вернуться на главную» (истинен РОВНО когда проект открыт). Они врут по-разному и
        # независимо, поэтому вместе закрывают дыру: в тот раз is_editing() был True.
        # `is_home()` из условия убран намеренно: он врёт в обе стороны и добавить мог только риск.
        if not holds_project(proj_dir) and not capcut_ui.is_editing():
            return
        capcut_ui.go_home()   # первый клик мог уйти в никуда (окно в переходе)
        time.sleep(1)
    raise ValueError("CapCut держит проект открытым — закрой его («Вернуться на главную») и повтори. "
                     "Записывать слой в открытый проект нельзя: CapCut перезапишет файл и работа пропадёт")


def run_track(proj_dir, vid_id, target_ids, roi, start_us=None, engine="ml",
              props=None, level=3, reopen=True, live=False, reframe=False, planar=False,
              quad=None, log=print):
    """Full one-shot: close->track->inject->reopen. roi in CANVAS px (x,y,w,h).
    quad: 4 угла поверхности в px КАНВАСА (UL,UR,DL,DR) — выбор поверхности «по углам экрана».
    Прямоугольная рамка остаётся частным случаем: из квада всегда выводится её bbox.
    target_ids: id слоя ИЛИ список id (мультивыбор) — трек один раз, следуют все.
    props: какие кривые писать — подмножество {'pos','scale','rot'} (None = все).
    reframe: СЛЕЖКА ЗА ОБЪЕКТОМ — двигаем сам видеоклип, чтобы объект остался в кадре (follower не нужен).
    start_us: timeline-время выбранного кадра. engine 'ml'=CoTracker3, 'csrt'=OpenCV."""
    if isinstance(target_ids, str):
        target_ids = [target_ids]
    props = set(props) if props else {"pos", "scale", "rot"}
    name = os.path.basename(proj_dir)
    was_open = _project_open(proj_dir)
    _t0 = time.time()
    write_progress(3, "Запуск трекинга…")   # бар появляется мгновенно, до загрузки модели
    # У планарного пути живой режим ПОЯВИЛСЯ (углы едут командой, а не через файлы драфта).
    # Кил-свитч возвращает старое поведение: закрыть проект, записать углы в файлы, переоткрыть.
    # Мультивыбор тоже идёт старым путём — живой канал ve_curve.txt несёт один сегмент.
    if planar and (os.path.exists(VE_NO_LIVECMD_PATH) or len(target_ids) > 1):
        live = False
    log(f"\n════════ ТРЕКИНГ (жёсткость {level}, {engine.upper()}{', LIVE' if live else ''}) ════════")
    if not live:  # live: проект НЕ закрываем — дилиб пишет в открытый
        # require_closed, а НЕ close_to_home: та пропускает работу, когда _project_open() говорит
        # «закрыт», а он основан на ОТСУТСТВИИ признаков и врёт (свежий проект, unlink'нутый
        # .locked). Тогда inject_* пишет в файл, который CapCut держит, и автосейв стирает трек
        # молча. Здесь нужно ПОЛОЖИТЕЛЬНОЕ доказательство закрытости.
        require_closed(proj_dir, log)
        capcut_ui.show_console()  # только в не-live: терминал с прогресс-баром (в live прогресс в оверлее)

    draft = read_draft(proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    vid = by_id.get(vid_id)
    targets = [by_id[tid] for tid in target_ids if tid in by_id]
    if not vid or (not reframe and not targets):
        raise ValueError("сегмент не найден в драфте (переоткрой проект в тул)")
    if planar:   # ДО трека: гнать по углам можно не всякий слой, а ошибка вылезла бы уже мёртвым результатом
        bad = [(t["id"], k) for t, (ok, k) in ((t, cornerpin_capable(draft, t)) for t in targets) if not ok]
        if bad:
            raise ValueError(
                f"планарный трек не умеет такие слои: {', '.join(k for _, k in bad)}. "
                "Углы (corner_pin) движок хранит только у ВИДЕО и картинок — у текста и стикеров "
                "он выбрасывает это поле при загрузке, ключи ложатся, а слой стоит на месте. "
                "Положи вставку видео/картинкой либо гони обычный трекинг без планарного режима.")
    _, vmat = material_index(draft)[vid["material_id"]]
    src = vmat.get("path")
    if not src or not os.path.exists(src):
        raise ValueError("исходник видео не найден")
    # Стоковые клипы CapCut лежат в кеше онлайн-материалов и часто скачаны ЧАСТИЧНО
    # (файл есть, но moov-атома нет — OpenCV его не откроет). Без этой проверки трекинг
    # падал стектрейсом «не смог открыть видео», из которого причина не читалась.
    if "onlineMaterial" in src or "/Containers/com.lemon" in src:
        import cv2 as _cv2
        _cap = _cv2.VideoCapture(src)
        _ok = _cap.isOpened() and _cap.read()[0]
        _cap.release()
        if not _ok:
            raise ValueError(
                "исходник — стоковый клип CapCut, скачан не полностью (нет moov-атома). "
                "Трекинг работает только с полноценным файлом: проиграй клип целиком в CapCut, "
                "чтобы он докачался, либо замени его на локальное видео.")
    # окно трека: reframe -> весь видеоклип; иначе объединение пересечений видео с каждым слоем
    if reframe:
        vtr = vid["target_timerange"]
        ranges = [(vtr["start"], vtr["start"] + vtr["duration"])]
    else:
        ranges = [(a, b) for a, b in (overlap_window(vid, t) for t in targets) if b > a]
    if not ranges:
        raise ValueError("слои не пересекаются с видео по времени")
    ov0, ov1 = min(a for a, _ in ranges), max(b for _, b in ranges)
    query = max(ov0, min(int(start_us), ov1 - 1)) if start_us is not None else ov0

    # ROI: координаты канваса (композита) -> пиксели исходного видео
    import cv2
    import numpy as np

    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    Minv = cv2.invertAffineTransform(
        np.array(_layer_matrix(vid["clip"], vmat["width"], vmat["height"], cw, ch), np.float32))
    # Квад задан -> он и есть поверхность; порядок углов UL,UR,DL,DR сохраняем (bbox его теряет).
    # Не задан -> четыре угла рамки, то есть прежнее поведение слово в слово.
    rx, ry, rw, rh = roi
    quad_pts = ([tuple(p) for p in quad] if quad else
                [(rx, ry), (rx + rw, ry), (rx, ry + rh), (rx + rw, ry + rh)])
    cs = [Minv @ np.array([x, y, 1.0]) for x, y in quad_pts]
    quad_src = [(float(p[0]), float(p[1])) for p in cs]
    xc, yc = [p[0] for p in cs], [p[1] for p in cs]
    roi_src = (min(xc), min(yc), max(xc) - min(xc), max(yc) - min(yc))

    write_progress(8, "Чтение кадров, загрузка модели…")   # закрываем ~10с-пробел перед трекингом
    if engine == "ml" and ml_track.available():
        log("ML-трекинг (CoTracker3)...")
        mlpts, meta = ml_track.track_roi(src, kf_time(vid, ov0), kf_time(vid, ov1),
                                         kf_time(vid, query), roi_src, level=level,
                                         quad=(quad_src if quad else None), log=log)
        # ПЛАНАРНЫЙ ТРЕК — отдельная ветвь: гоняем УГЛЫ (corner_pin), а не трансформ слоя.
        # Возврат ранний, потому что дальше весь конвейер заточен под (xs, ys, ss, rs).
        if planar:
            homos, htimes = meta.get("homos"), meta.get("times")
            if not homos:
                raise ValueError("планарный трек доступен только с ML-трекером")
            cpin = {t["id"]: compute_cornerpin_ml(draft, vid, t, homos, htimes,
                                                  quad_src if quad else None)
                    for t in targets}
            corners = {k: v for k, v in cpin.items() if v}
            if not corners:
                raise ValueError("не удалось построить плоскость: мало кадров или ROI вне слоя")
            plane = _plane_report(meta, log)
            write_progress(97, "Запись углов…")
            tid = next(iter(corners))
            written = 0
            synced = live and live_cornerpin(by_id[tid], corners[tid], log=log)
            if not synced:
                # Кил-свитч, мультивыбор, нет дилиба — старый путь. Писать в файлы драфта можно
                # только при ЗАКРЫТОМ проекте: открытый CapCut затрёт запись своим сохранением.
                if live:
                    log("живой планарный путь не вышел — пишу углы в драфт и переоткрываю")
                    require_closed(proj_dir, log)   # положительная проверка, см. ветку выше
                written = inject_cornerpin(proj_dir, corners)
            nkf = sum(len(p) for c in corners.values() for p in c.values())
            log(f"планарный трек: слоёв {len(corners)}, ключей углов {nkf}, файлов {written}")
            if not synced and reopen and (was_open or capcut_ui.is_running()):
                try:
                    capcut_ui.reopen(name)
                except Exception as e:   # noqa: BLE001 — ключи уже записаны, переоткрытие вторично
                    log(f"переоткрытие: {e}")
            write_progress(100, "Готово")
            return {"ok": True, "layers": len(corners), "keyframes": nkf, "live": bool(synced),
                    "covered": meta.get("covered", 1.0), "planar": True, **plane}
        if reframe:   # слежка: кривая центрирования на сам видеоклип
            curves = {vid_id: compute_curves_reframe(draft, vid, mlpts, roi_src, want_scale=("scale" in props))}
        else:
            curves = {t["id"]: compute_curves_ml(draft, vid, t, mlpts, roi_src) for t in targets}
        npts = len(mlpts)
    else:  # CSRT: только вперёд от выбранного кадра
        if reframe:
            raise ValueError("слежка за объектом работает только с ML-трекером — включи галку ML")
        log("трекинг (CSRT)...")
        points, meta = track_video(src, kf_time(vid, query), kf_time(vid, ov1), roi=roi_src, show=False)
        if len(points) < 2:
            raise ValueError("трекинг не дал точек")
        curves = {t["id"]: (*compute_curves(draft, vid, t, points), None) for t in targets}
        npts = len(points)

    for tid, (xs, ys, ss, rs) in list(curves.items()):  # оставить только выбранные свойства
        curves[tid] = (xs if "pos" in props else [], ys if "pos" in props else [],
                       ss if "scale" in props else None, rs if "rot" in props else None)

    nkf = sum(len(c[0]) or len(c[2] or []) or len(c[3] or []) for c in curves.values())
    nlay = sum(1 for c in curves.values() if any(c))
    write_progress(97, "Запись ключей…")
    applied = True   # реально ли записали кривую (False = трек вернул статику)
    if live:
        # защита: если трек вернул статику (ROI не на движущемся объекте) — не заливаем сотни одинаковых ключей
        pvals = []
        for xs, ys, ss, rs in curves.values():
            pvals += [v for _, v in (xs or [])] + [v for _, v in (ys or [])]
        spread = (max(pvals) - min(pvals)) if pvals else 0.0
        if spread < 0.01:
            log(f"⚠️  ТРЕК ВЕРНУЛ СТАТИКУ (разброс позиции {spread:.4f}) — ROI попал не на движущийся объект. "
                f"Ключи НЕ залиты. Обведи ROI ПРЯМО на объекте, что двигается, и трекни заново.")
            written = 0
            applied = False
        else:
            log(f"LIVE-запись (разброс позиции {spread:.3f})...")
            try:
                os.remove(VE_RENDER_STATUS_PATH)
            except OSError:
                pass
            since = _log_size()
            write_live_curve(curves, log=log)
            written = 0
            log("→ применяется автоматически на follower в CapCut")
            # Ключи дилиб заливает в документ асинхронно (тик ~300мс), поэтому ЖДЁМ его отчёт:
            # команда значения не несёт и снимет то, что лежит в треке НА МОМЕНТ выстрела.
            tid, cur = next(((k, v) for k, v in curves.items() if any(v)), (None, None))
            prop, pairs = _live_prop(cur) if cur else (None, None)
            synced = bool(prop) and _log_wait("AUTO created", since) \
                and live_sync(by_id[tid], prop, pairs[len(pairs) // 2][0], log=log)
            # Живой синк не вышел (дилиба нет, проект закрыт, кил-свитч) — старый путь:
            # переоткрыть проект, на загрузке CapCut синкает документ в рендер сам.
            # <=0 покрывает и «дилиб не ответил» (-1), и «пуш не прошёл» (0).
            if not synced and _render_pushed() <= 0:
                log("обновляю превью (быстрый перезаход)…")
                _reopen_project(proj_dir, name, log=log)
    else:
        log("запись ключей...")
        written = inject_curves(proj_dir, curves)

    if not live and reopen and (was_open or capcut_ui.is_running()):
        log("открываю проект...")
        write_progress(99, "Открываю проект…")
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001 — трек уже записан; не роняем результат из-за открытия
            log(f"не смог авто-открыть ({e}) — открой проект вручную")
    log(f"слоёв {nlay}, ключей {nkf} в {written} файла(ов); покрытие {meta['covered']:.0%}, {meta['reason']}")
    write_progress(100, "Готово")
    log(f"════════ ГОТОВО за {time.time() - _t0:.0f}с ════════\n")
    # live+applied → follower в документе, но рендер не обновлён: UI покажет подсказку про «сборный клип»
    return {"points": npts, "keyframes": nkf, "layers": nlay, "files": written,
            "live": bool(live), "applied": applied, **meta}


_PREVIEW = {}     # ключ проекта+слоя -> состояние решённой камеры для живого предпросмотра


def preview3d_prepare(proj_dir, vid_id, target_id, quad, start_us=None, log=print):
    """ПОДГОТОВКА ЖИВОГО ПРЕДПРОСМОТРА: решить камеру ОДИН раз и запомнить.

    Крутилки объекта (смещение, высота, размер) без этого «ничего не означают»: пользователь
    вращает вслепую. После солва пересчёт четырёх углов стоит микросекунды, поэтому дальше
    предпросмотр честно живой — а показывает его САМ CapCut, настоящим слоем со своим шрифтом
    и стилями, через то же поле углов, которым работает ручной драг."""
    import cv2
    import numpy as np

    import track3d

    draft = read_draft(proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    vid, tgt = by_id.get(vid_id), by_id.get(target_id)
    if not vid or not tgt:
        raise ValueError("сегмент не найден в драфте (переоткрой проект в тул)")
    ok, kind = cornerpin_capable(draft, tgt)
    if not ok and kind == "texts":
        # ЗАВОРАЧИВАЕМ И ЗДЕСЬ. Пользователь жмёт «предпросмотр» раньше трека, и требовать от него
        # знания, что у текста нет слота углов, — то же самое, что не сделать фичу.
        mapping = wrap_texts_for_corners(proj_dir, [target_id], log=log)
        if mapping:
            if capcut_ui.is_running():
                capcut_ui.reopen(os.path.basename(proj_dir))
                time.sleep(2)
            draft = read_draft(proj_dir)
            by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
            target_id = mapping.get(target_id, target_id)
            vid, tgt = by_id.get(vid_id), by_id.get(target_id)
            if not vid or not tgt:
                raise ValueError("после заворота сегменты не нашлись (переоткрой проект в тул)")
            log(f"текст завёрнут в сборный клип → {target_id[:8]}")
            ok, kind = cornerpin_capable(draft, tgt)
    if not ok:
        raise ValueError(f"у слоя материал {kind} — углы движок хранит только у видео и картинок")
    _, vmat = material_index(draft)[vid["material_id"]]
    src = vmat.get("path")
    if not src or not os.path.exists(src):
        raise ValueError("исходник видео не найден")
    ov0, ov1 = overlap_window(vid, tgt)
    if ov1 <= ov0:
        raise ValueError("слой не пересекается с видео по времени")
    query = max(ov0, min(int(start_us), ov1 - 1)) if start_us is not None else ov0

    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    Minv = cv2.invertAffineTransform(
        np.array(_layer_matrix(vid["clip"], vmat["width"], vmat["height"], cw, ch), np.float32))
    quad_src = [tuple(float(v) for v in (Minv @ np.array([x, y, 1.0]))[:2]) for x, y in quad]

    work = os.path.expanduser(f"~/Movies/CapCut/User Data/ve_3d/{os.path.basename(proj_dir)}")
    # Решённая сцена остаётся в ПРОЦЕССЕ-СОЛВЕРЕ под этим ключом: тащить сюда pycolmap-объекты
    # незачем, а крутилки предпросмотра всё равно спрашивают их через `object_at`.
    key = f"{proj_dir}|{target_id}"
    r = track3d_call({"op": "prepare", "key": key, "path": src,
                      "quad": [list(q) for q in quad_src], "work": work,
                      "t0_us": kf_time(vid, ov0), "t1_us": kf_time(vid, ov1),
                      "ref_us": kf_time(vid, query), "progress_path": VE_PROGRESS_PATH}, log=log)
    _PREVIEW[key] = {"ref_src_us": r["ref_src_us"], "proj_dir": proj_dir, "vid_id": vid_id,
                     "target_id": target_id, "quad_src": quad_src}
    write_progress(100, "Готово")
    return {"ok": True, "key": key, "ref_us": query, **r["meta"]}


def preview_apply(proj_dir=None, vid_id=None, target_id=None, quad=None, obj=None,
                  playhead_us=None, key=None, log=print):
    """Крутилку подвинули — показать результат СРАЗУ живым полем углов. Ключей не пишем.

    ДВА РЕЖИМА, и дешёвый — основной:
      · БЕЗ РЕШЕНИЯ КАМЕРЫ (мгновенно): всё, что лежит В ПЛОСКОСТИ — смещение и размер — это
        чистый варп по обведённой сетке. Перспективу поверхности пользователь уже задал руками
        четырьмя углами; добывать её сорокасекундным солвом незачем.
      · С РЕШЕНИЕМ (после «Подготовить»): добавляются ВЫСОТА (объект вне плоскости — без 3D её
        взять неоткуда) и любой другой кадр, кроме опорного.
    В ответе `mode`, чтобы панель честно говорила, что именно показано."""
    import cv2
    import numpy as np

    import track3d

    p = _PREVIEW.get(key or "")
    draft = read_draft(p["proj_dir"] if p else proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    vid = by_id.get(p["vid_id"] if p else vid_id)
    tgt = by_id.get(p["target_id"] if p else target_id)
    if not vid or not tgt:
        raise ValueError("сегмент не найден в драфте (переоткрой проект в тул)")
    ok, kind = cornerpin_capable(draft, tgt)
    if not ok:
        raise ValueError(f"у слоя материал {kind} — углы движок хранит только у видео и картинок; "
                         "текст заворачивается в сборный клип автоматически при подготовке")
    tmat = material_index(draft)[tgt["material_id"]][1]
    o = dict(obj or {})
    if tmat.get("width") and tmat.get("height") and "aspect" not in o:
        o["aspect"] = tmat["width"] / tmat["height"]
    if p:
        t_src = kf_time(vid, int(playhead_us)) if playhead_us is not None else p["ref_src_us"]
        r = track3d_call({"op": "object_at", "key": key, "obj": o, "t_src": t_src}, log=log)
        H, quad_obj = np.array(r["H"], float), np.array(r["quad_obj"], float)
        mode = "3d"
    else:
        if not quad or len(quad) != 4:
            raise ValueError("не выбрана плоскость: обведи её четырьмя ручками в плеере")
        _, vmat = material_index(draft)[vid["material_id"]]
        cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
        Minv = cv2.invertAffineTransform(
            np.array(_layer_matrix(vid["clip"], vmat["width"], vmat["height"], cw, ch), np.float32))
        qs = np.array([[float(v) for v in (Minv @ np.array([x, y, 1.0]))[:2]] for x, y in quad],
                      np.float32)
        unit = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], np.float32)      # UL UR DL DR
        M = cv2.getPerspectiveTransform(unit, qs)
        # Плоский предпросмотр не решает камеру, поэтому оси репера заменяют стороны квада
        # на кадре: ракурс их укорачивает, но автовысота нужна тут «на глаз», а не в метрах.
        o = track3d.resolve_obj([(np.linalg.norm(qs[1] - qs[0]) + np.linalg.norm(qs[3] - qs[2])) / 2,
                                 (np.linalg.norm(qs[2] - qs[0]) + np.linalg.norm(qs[3] - qs[1])) / 2],
                                o)
        uv = np.array([[0.5 + o["x"] + sx * o["w"], 0.5 + o["y"] + sy * o["h"]]
                       for sy in (-0.5, 0.5) for sx in (-0.5, 0.5)], np.float32)
        quad_obj = cv2.perspectiveTransform(uv.reshape(-1, 1, 2), M).reshape(-1, 2)
        H = np.eye(3)              # опорный кадр: слой просто ложится в этот квад
        t_src = kf_time(vid, int(playhead_us)) if playhead_us is not None else \
            int((vid.get("source_timerange") or {}).get("start", 0))
        mode = "warp"
    # compute_cornerpin_ml — конвейерная функция и на ОДНОМ кадре отдаёт пусто (ей нужно ≥2 точки,
    # иначе сглаживать нечего). Подаём ту же гомографию дважды: считает ровно те же углы, что
    # получит настоящий трек, — предпросмотр и результат не разъедутся по определению.
    corners = compute_cornerpin_ml(draft, vid, tgt, [H, H], [t_src, t_src + 1000], quad_obj)
    vals = []
    for prop in CORNER_PROPS:
        pairs = corners.get(prop) or []
        vals += [float(pairs[0][1][0]), float(pairs[0][1][1])] if pairs else [0.0, 0.0]
    with open(VE_CORNERPIN_PATH, "w") as f:
        f.write(tgt["id"] + " " + " ".join(f"{v:.6f}" for v in vals) + "\n")
    log(f"предпросмотр [{mode}]: углы на {t_src / 1e6:.2f} с → {['%.2f' % v for v in vals[:4]]}…")
    return {"ok": True, "mode": mode, "corners": vals,
            "note": ("высота требует решения камеры — нажми «Подготовить»"
                     if mode == "warp" and abs(o["z"]) > 1e-9 else "")}


VE_WRAP_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_wrap.txt")
VE_WRAP_OUT_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_wrap_out.txt")
# ЖИВОЙ ЗАВОРОТ — ПО ЗАПРОСУ, а не по умолчанию (решение Назара 2026-08-09): он убивает
# пойманный указатель секвенции на весь процесс, канал молчит после первого применения и
# переоткрытие проекта его не воскрешает. Надёжность важнее скорости, пока ядро не вернёт
# канал. Ветка не удалена — проверена (5 из 5 на завороте, сквозной прогон 124 ключа).
VE_LIVEWRAP_PATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_livewrap.txt")


def scale_live(seg_id, sx, sy, timeout=6.0):
    """Масштаб сегмента родной командой `scaleSegment` в открытом проекте. Значение АБСОЛЮТНОЕ.

    ⚠️ Работает только на сегменте ВЕРХНЕГО уровня: у вложенного в компаунд своя сессия lyra,
    и та же команда там молча ничего не делает (замер: на прокси 0.5 применился, на вложенном
    тексте 4.9 — нет). Отсюда и порядок в живом заворотe: ужимаем текст, ПОКА он снаружи."""
    since = _log_size()
    js = ('{"commit_immediately":true,"params":{"segment_id":"%s","x":%.6f,"y":%.6f}}'
          % (seg_id, sx, sy))
    with open(VE_KFREQ_PATH, "w") as f:
        f.write("scaleSegment %s -1\n%s\n" % (seg_id, js))
    try:
        return _log_wait("MKREQ отправлена api=scaleSegment", since, timeout)
    finally:
        try:      # гейт снимаем всегда: оставленный стреляет при открытии любого проекта
            os.remove(VE_KFREQ_PATH)
        except OSError:
            pass


def wrap_live(seg, name="NZLD компаунд", timeout=10.0, log=print):
    """ЖИВОЙ ЗАВОРОТ в сборный клип: родная команда движка в ОТКРЫТОМ проекте, без цикла
    «закрыть → записать → открыть». Гейт `ve_wrap.txt`, ключевое поле payload'а — `type: 1`
    (с нулём та же команда молча удаляет сегмент), см. [[sbornye-klipy]].

    Возвращает id созданного ПРОКСИ либо None. Сам id приходит из `ve_wrap_out.txt`: движок
    после заворота запрашивает новый сегмент, дилиб ловит его в getseg-карте и пишет строку
    «<завёрнутый> <прокси>». Читать драфт бесполезно — на диске он в этот момент протухший."""
    tr = seg.get("target_timerange") or {}
    st, dur = int(tr.get("start", 0)), int(tr.get("duration", 0))
    if dur <= 0:
        raise ValueError("у сегмента нет длительности — заворачивать нечего")
    for p in (VE_WRAP_OUT_PATH,):     # старый ответ — чужой; ждём именно свой
        try:
            os.remove(p)
        except OSError:
            pass
    since = _log_size()
    with open(VE_WRAP_PATH, "w") as f:
        f.write("%s %d %d %s\n" % (seg["id"], st, dur, name))
    try:      # именно sent=1: строка печатается и когда команда не собралась
        ok = _log_wait("WRAP %s (%d..%d) sent=1" % (seg["id"], st, st + dur), since, timeout)
    finally:
        try:      # оставленный гейт стреляет при открытии ЛЮБОГО проекта — снимаем всегда
            os.remove(VE_WRAP_PATH)
        except OSError:
            pass
    proxy = None
    if ok:
        deadline = time.time() + 3.0
        while time.time() < deadline and not proxy:
            try:
                parts = open(VE_WRAP_OUT_PATH, encoding="utf-8").read().split()
                if len(parts) == 2 and parts[0] == seg["id"]:
                    proxy = parts[1]
            except OSError:
                time.sleep(0.2)
    log("живой заворот %s: %s" % (seg["id"][:8], ("прокси " + proxy[:8]) if proxy
                                  else ("команда ушла, id прокси не пришёл" if ok
                                        else "движок не ответил")))
    return proxy


def text_ink_box(seg_id, cw, ch, timeout=8.0):
    """Габарит ЧЕРНИЛ надписи (ширина, высота) в пикселях канваса — прямо из движка, гейт `BBOX2`
    (`lvve::TextUtils::get_bounding_rect`). None = ответа нет: проект закрыт, дилиба нет
    или сегмент не текст.

    ⚠️ Высота — это LINE BOX, а не высота видимых заглавных ([[shina-komand]]: 233.7 против ~87 px).
    Берём именно её, потому что она единственная измеримая: коробка сядет на строку, а не на глифы.

    Замер 2026-08-08: ответ НЕ включает масштаб клипа. Послали `scaleSegment` 2.0 (что он
    применился, проверено по файлу после закрытия — `clip.scale` стал 2.0), габарит остался
    325.8 px. То есть это собственный размер надписи при масштабе 1 — ровно то, что нужно
    для k = W_канваса / w_ink: делить на текущий масштаб НЕ надо."""
    since = os.path.getsize(VE_TEXTBOX_OUT_PATH) if os.path.exists(VE_TEXTBOX_OUT_PATH) else 0
    with open(VE_TEXTBOX_PATH, "w") as f:
        f.write("BBOX2 %s %d,%d\n{}\n" % (seg_id, int(cw), int(ch)))
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:                                  # файл ДОПИСЫВАЕТСЯ — берём последний ответ по id
                with open(VE_TEXTBOX_OUT_PATH, errors="ignore") as f:
                    f.seek(since)
                    rows = [l.split() for l in f if l.startswith(seg_id)]
                if rows:
                    return float(rows[-1][1]), float(rows[-1][2])
            except OSError:
                pass
            time.sleep(0.25)
    finally:
        try:      # оставленный гейт стреляет заново при открытии ЛЮБОГО проекта — убираем всегда
            os.remove(VE_TEXTBOX_PATH)
        except OSError:
            pass
    return None


def _livewrap_safe(draft, seg_ids):
    """Безопасен ли ЖИВОЙ заворот этих слоёв. (можно, причина отказа).

    Живая команда кладёт компаунд на дорожку 0 — это зашито в payload гейта (`track_index: 0`).
    Если на нулевой дорожке в это же время что-то стоит, движок его РАЗДВИНЕТ: на стенде
    исходное видео уехало со старта 0 на 2 с, и трекер дальше работал по сдвинутому клипу.
    Пока настоящий `track_index` не пробросили в `ve_wrap.txt`, живой путь берём только там,
    где двигать нечего; иначе честно уходим на файловый."""
    zero = draft["tracks"][0].get("segments", []) if draft.get("tracks") else []
    for sid in seg_ids:
        seg = next((s for t in draft["tracks"] for s in t.get("segments", [])
                    if s["id"] == sid), None)
        if not seg:
            continue
        a = int(seg["target_timerange"]["start"])
        b = a + int(seg["target_timerange"]["duration"])
        for s in zero:
            if s["id"] == sid:
                continue
            c = int(s["target_timerange"]["start"])
            if c < b and a < c + int(s["target_timerange"]["duration"]):
                return False, ("на дорожке 0 стоит %s — живой заворот сдвинул бы его, "
                               "потому что кладёт компаунд туда же" % s["id"][:8])
    return True, ""


def patch_live_wrap(draft, sid, info):
    """Отразить ЖИВОЙ заворот в драфте, который держим В ПАМЯТИ.

    Драфт на диске в этот момент протухший — компаунда там нет, — а весь хвост конвейера читает
    именно его: `cornerpin_capable` (материал прокси должен быть `videos`), `_layer_matrix`
    (габарит материала = коробка слоя), `ink_fractions` (канвас компаунда и масштаб текста внутри),
    запись углов. Собираем ровно то, что движок только что сделал: компаунд с КАДРОВЫМ канвасом,
    внутри текст со своим масштабом, снаружи прокси размером с канвас.
    """
    trk, seg = None, None
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            if s.get("id") == sid:
                trk, seg = t, s
    if not seg:
        return None
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    k, proxy_id = info["k"], info["proxy"]
    inner = copy.deepcopy(seg)                     # текст уехал внутрь — с масштабом, ужатым на k
    sc = float((inner.get("clip") or {}).get("scale", {}).get("x", 1.0))
    inner.setdefault("clip", {})["scale"] = {"x": sc * k, "y": sc * k}
    combo_id, mat_id = "COMBO-" + proxy_id, "PROXY-" + proxy_id
    draft["materials"].setdefault("drafts", []).append({
        "id": combo_id, "type": "combination",
        "draft": {"id": "INNER-" + proxy_id,
                  "canvas_config": {"width": cw, "height": ch},
                  "materials": {"texts": [{"id": inner["material_id"]}]},
                  "tracks": [{"id": "T-" + proxy_id, "type": "video", "segments": [inner]}]}})
    draft["materials"].setdefault("videos", []).append(
        {"id": mat_id, "type": "video", "path": "", "media_path": "",
         "width": int(cw), "height": int(ch), "extra_type_option": 2})
    seg["id"] = proxy_id
    seg["material_id"] = mat_id
    seg["extra_material_refs"] = [combo_id]
    # прокси движок ставит по центру с масштабом 1; мы вернули ему ровно то, что ужали внутри
    seg["clip"] = {"scale": {"x": 1.0 / k, "y": 1.0 / k}, "transform": {"x": 0.0, "y": 0.0},
                   "rotation": 0.0, "alpha": 1.0,
                   "flip": {"vertical": False, "horizontal": False}}
    seg["common_keyframes"], seg["keyframe_refs"] = [], []
    return seg


def wrap_texts_for_corners(proj_dir, seg_ids, stretch=1.0, log=print):
    """АВТО-ЗАВОРОТ текста в сборный клип, чтобы на нём заработали углы.

    Слот `corner_pin` есть у `materials.videos` и `materials.stickers`, а у `materials.texts` его
    НЕТ — запись туда движок отвергает (замер: поле исчезает после перезахода). Завёрнутый текст
    живёт внутри компаунда, а наверху оказывается ВИДЕО-прокси, у которого слот есть; надпись при
    этом остаётся редактируемой.

    По умолчанию заворачиваем ЖИВОЙ командой движка — проект не закрывается ни разу. Файловый
    По умолчанию путь ФАЙЛОВЫЙ: закрыть проект → записать обе копии → открыть обратно.
    Живой заворот родной командой включается файлом `ve_livewrap.txt` — он быстрее (0.5 с
    против цикла), но пока сажает канал команд на весь процесс.

    Возвращает {старый id: {'proxy', 'ink', 'k', 'live'}}. Пустой словарь = заворачивать нечего."""
    import compound

    draft = read_draft(proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    mi = material_index(draft)
    need = [sid for sid in seg_ids
            if sid in by_id and mi.get(by_id[sid].get("material_id"), ("?",))[0] == "texts"]
    if not need:
        return {}
    log(f"текстовых слоёв под заворот: {len(need)} (углы у текста движок не хранит)")
    # ЧЕРНИЛА ЖИВУТ ТОЛЬКО В ЖИВОЙ МОДЕЛИ, и от состояния getseg-карты зависит, ответит ли
    # движок. Поэтому меряем ДВАЖДЫ: до заворота (тогда переростка ещё можно ужать) и, если
    # там не вышло, ПОСЛЕ — сам заворот резолвит сегменты и карту наполняет.
    # ⚠️ Ужимать после заворота нечем: `scaleSegment` до вложенного сегмента не доходит,
    # хендлер ищет id в ТЕКУЩЕМ рабочем драфте, а текст уехал в под-драфт. Замер: команда
    # по старому id ушла (`sent=1`), масштаб внутри остался 1.0.
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    inks, stretches = {}, {}
    for sid in need:
        ink = text_ink_box(sid, cw, ch)
        inks[sid] = ink
        stretches[sid] = min(1.0, cw / ink[0]) if ink and ink[0] >= 1.0 else 1.0
        log("  %s: чернила %s ⇒ k=%.3f%s" %
            (sid[:8], ("%.1f × %.1f px" % ink) if ink else "до заворота не измерены",
             stretches[sid], " (переросток, ужимаем)" if stretches[sid] < 1.0 else ""))

    # ⚠️ ЖИВОЙ ЗАВОРОТ КЛАДЁТ КОМПАУНД НА ДОРОЖКУ 0 и раздвигает то, что там стоит: payload
    # гейта несёт `track_index: 0` жёстко. Замер на стенде: текст был на дорожке 3, после
    # заворота компаунд сел на 0, а исходное видео уехало со старта 0 на 2 с — то есть трекер
    # дальше считал по СДВИНУТОМУ клипу. С `track_index` = дорожка самого текста ничего не
    # двигается (проверено тем же выстрелом через общий гейт). Пока поле не пробросили в
    # `ve_wrap.txt`, живой путь берём только когда двигать нечего.
    live_ok, why = _livewrap_safe(draft, need)
    if os.path.exists(VE_LIVEWRAP_PATH) and live_ok:
        out = {}
        for sid in need:
            seg, k = by_id[sid], stretches[sid]
            # Ужимаем, ПОКА текст обычный слой снаружи: внутрь компаунда правка уезжает как
            # есть, а достучаться туда потом уже нечем.
            s0 = float((seg.get("clip") or {}).get("scale", {}).get("x", 1.0))
            if k != 1.0 and not scale_live(sid, s0 * k, s0 * k):
                raise ValueError("не удалось ужать надпись перед заворотом — движок не ответил")
            proxy = wrap_live(seg, name="Текст 3D", log=log)
            if not proxy:
                raise ValueError(
                    "живой заворот не удался: движок не отдал id прокси. Проверь, что проект "
                    "открыт, либо убери ve_livewrap.txt и заворачивай файловым путём")
            # ⚠️ id прокси дилиб вычисляет как «новое в getseg-карте», и на пустом выстреле это
            # умеет соврать (замер: команда по несуществующему id всё равно вернула чей-то id).
            # Настоящий прокси только что создан ⇒ в прочитанном драфте его быть НЕ МОЖЕТ.
            if proxy in by_id:
                raise ValueError("движок вернул id уже существующего сегмента (%s) — заворот "
                                 "не состоялся, а ответ ложный" % proxy[:8])
            # Компенсация на прокси — и ОНА ЖЕ пинок движку. Заворот перестраивает таймлайн,
            # и пойманный дилибом указатель секвенции протухает: после него молчат все гейты
            # (в логе `SEQKICK: … сдаюсь`), а трек падает уже на записи углов. Указатель
            # обновляется только из commit-хука, поэтому шлём команду с `commit_immediately`
            # ВСЕГДА — при k=1 она холостая (тот же масштаб), но коммит доводит.
            if not scale_live(proxy, 1.0 / k, 1.0 / k):
                raise ValueError("компенсация на прокси не прошла — картинка осталась мельче "
                                 "исходной, а движок не коммитнул")
            if not inks[sid]:
                # ВТОРАЯ ПОПЫТКА, ради которой порядок и переворачивали: заворот сам резолвит
                # сегменты, и карта дилиба после него отвечает даже там, где до этого молчала.
                # Текст внутри компаунда сохраняет свой ЖИВОЙ id — по нему и меряем.
                inks[sid] = text_ink_box(sid, cw, ch)
                if inks[sid]:
                    log("  %s: чернила после заворота %.1f × %.1f px" % (sid[:8], *inks[sid]))
                    if cw / inks[sid][0] < 1.0:
                        log("  ⚠️ надпись шире кадра, а ужать её уже нечем — внутрь компаунда "
                            "команды не доходят; надпись обрежется по краю")
            if not inks[sid]:
                # ⚠️ Без чернил `ink_fractions` вернёт (1,1), и углы МОЛЧА поедут по коробке.
                # Лучше громкий отказ: заворот уже случился, но трек по кривым данным хуже.
                raise ValueError(
                    "габарит надписи не измерен ни до заворота, ни после: движок отвечает "
                    "только на открытом проекте. Открой проект и запусти трек оттуда")
            out[sid] = {"proxy": proxy, "ink": inks[sid], "k": k, "live": True}
        return out

    log("файловый путь (с закрытием проекта): %s"
        % (why if os.path.exists(VE_LIVEWRAP_PATH) else "живой заворот включается ve_livewrap.txt"))
    # У файлового пути второй попытки нет: он закрывает проект, а на закрытом мерить некого.
    if any(not inks[sid] for sid in need) and abs(stretch - 1.0) < 1e-9:
        raise ValueError("габарит надписи не измерен, а файловый путь сейчас закроет проект — "
                         "мерить станет негде. Открой проект и запусти трек оттуда")
    require_closed(proj_dir, log)
    # ⚠️ ГОНКА, пойманная замером: `require_closed` доволен, когда видит главную (плитки на
    # экране), а `.locked` CapCut снимает чуть позже, дописав сейв. `wrap_in_project` проверяет
    # именно ФАЙЛ и в этот зазор отказывался «проект открыт». Ждём файл, а не судим по интерфейсу.
    lock = os.path.join(proj_dir, ".locked")
    for _ in range(40):
        if not os.path.exists(lock):
            break
        time.sleep(0.5)
    infos = compound.wrap_in_project(proj_dir, need, name_prefix="Текст 3D ",
                                     stretches=stretches, log=log)
    return {sid: {"proxy": inf["proxy_seg"], "ink": inks[sid], "k": stretches[sid], "live": False}
            for sid, inf in zip(need, infos)}


def run_track3d(proj_dir, vid_id, target_ids, quad, start_us=None, live=True, reopen=True,
                obj=None, text_stretch=1.0, log=print):
    """3D-ТРЕКИНГ: решение камеры → плоскость из облака → углы follower'а.

    Отличие от планарного пути только в ГОЛОВЕ: гомографии на кадр приходят не от подгонки
    точек на самой поверхности (там их может не быть вовсе), а от решённой камеры и плоскости,
    вынутой из облака RANSAC'ом. Хвост тот же самый — `compute_cornerpin_ml`, `live_cornerpin`,
    `inject_cornerpin`: они принимают homos+times и ничего не знают про источник.

    quad — четырёхугольник в px КАНВАСА (UL, UR, DL, DR). Он ЗАДАЁТ РЕПЕР плоскости, а не только
    область натяжки. Соотношение сторон не нужно — расстояние даёт облако.

    obj — где стоит объект в этом репере: {x, y, z, w, h} в долях квада. None = сам квад
    (сегодняшнее поведение). z ≠ 0 поднимает объект НАД плоскостью: текст над коробкой едет
    вместе с ней, но со своим параллаксом."""
    import cv2
    import numpy as np

    if not track3d_available():
        raise ValueError("3D-трекинг требует pycolmap: .venv/bin/pip install pycolmap")
    if isinstance(target_ids, str):
        target_ids = [target_ids]
    name = os.path.basename(proj_dir)
    _t0 = time.time()
    write_progress(3, "3D-трекинг: запуск…")
    log("\n════════ 3D-ТРЕКИНГ (решение камеры) ════════")
    draft = read_draft(proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    vid, targets = by_id.get(vid_id), [by_id[t] for t in target_ids if t in by_id]
    if not vid or not targets:
        raise ValueError("сегмент не найден в драфте (переоткрой проект в тул)")
    bad = [(t["id"], k) for t, (ok, k) in ((t, cornerpin_capable(draft, t)) for t in targets) if not ok]
    inks, mapping = {}, {}        # id прокси -> габарит чернил: углы ставятся ЧЕРНИЛАМ, не коробке
    if bad:
        # ТЕКСТ ЗАВОРАЧИВАЕМ САМИ. Требовать этого от пользователя — значит не получить пользователя:
        # он не обязан знать, что у texts нет слота corner_pin. С 2026-08-09 заворот ЖИВОЙ:
        # родная команда движка в открытом проекте, проект не закрывается ни разу.
        mapping = wrap_texts_for_corners(proj_dir, [t for t, k in bad if k == "texts"],
                                         stretch=text_stretch, log=log)
        if mapping:
            if all(m["live"] for m in mapping.values()):
                # ЖИВОЙ ЗАВОРОТ: проект НЕ закрывался, значит на диске компаунда ещё нет —
                # отражаем его в драфте, который держим в памяти, и идём дальше без переоткрытия.
                for sid, m in mapping.items():
                    patch_live_wrap(draft, sid, m)
            else:
                if capcut_ui.is_running():
                    capcut_ui.reopen(name)
                # ЖДЁМ ПРОКСИ, а не секунды. Фиксированная пауза здесь плавала: иногда
                # перечитанный драфт был ещё дозаворотным, слой оставался текстом, и трек
                # падал «у выбранного слоя материал texts» — на ровном месте, через раз.
                want = {m["proxy"] for m in mapping.values()}
                for _ in range(40):
                    draft = read_draft(proj_dir)
                    if want <= {s["id"] for t in draft["tracks"] for s in t.get("segments", [])}:
                        break
                    time.sleep(0.5)
            by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
            inks.update({m["proxy"]: m["ink"] for m in mapping.values()})
            target_ids = [mapping[t]["proxy"] if t in mapping else t for t in target_ids]
            targets = [by_id[t] for t in target_ids if t in by_id]
            vid = by_id.get(vid_id)
            log(f"текст завёрнут в сборный клип, слои: {', '.join(t[:8] for t in target_ids)}")
        bad = [(t["id"], k) for t, (ok, k) in ((t, cornerpin_capable(draft, t)) for t in targets)
               if not ok]
    if not vid or not targets:
        raise ValueError("после заворота сегменты не нашлись (переоткрой проект в тул)")
    if bad:
        raise ValueError(f"3D-трекинг гонит слой по углам, а движок хранит их только у видео и "
                         f"картинок; у выбранного слоя материал {bad[0][1]}")
    # УЖЕ завёрнутый текст: карты заворота нет, значит и габарита чернил нет — а без него углы
    # молча возвращаются к коробке, то есть к промаху в разы. Компаунд с текстом внутри живёт
    # дольше одного трека (пережил перезаход, пользователь трекает повторно), так что случай
    # обычный. Спрашиваем движок про ВНУТРЕННИЙ сегмент: на открытом проекте он резолвится.
    cwq, chq = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    for t in targets:
        if t["id"] in inks:
            continue
        isid = compound_text_seg_id(draft, t)
        if isid:
            box = text_ink_box(isid, cwq, chq)
            if box:
                inks[t["id"]] = box
                log(f"уже завёрнутый текст {t['id'][:8]}: чернила {box[0]:.1f} × {box[1]:.1f} px")
    _, vmat = material_index(draft)[vid["material_id"]]
    src = vmat.get("path")
    if not src or not os.path.exists(src):
        raise ValueError("исходник видео не найден")

    ranges = [(a, b) for a, b in (overlap_window(vid, t) for t in targets) if b > a]
    if not ranges:
        raise ValueError("слои не пересекаются с видео по времени")
    ov0, ov1 = min(a for a, _ in ranges), max(b for _, b in ranges)
    query = max(ov0, min(int(start_us), ov1 - 1)) if start_us is not None else ov0

    # Квад — обязательный вход: им задаётся ПЛОСКОСТЬ. Без него дальше шла распаковка None
    # и пользователь получал «NoneType object is not iterable» вместо объяснения.
    if not quad or len(quad) != 4:
        raise ValueError("не выбрана плоскость: нажми «Выбрать поверхность» и растяни четыре "
                         "ручки в плеере по краям плоскости, которую трекаем")
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    Minv = cv2.invertAffineTransform(
        np.array(_layer_matrix(vid["clip"], vmat["width"], vmat["height"], cw, ch), np.float32))
    quad_src = [tuple(float(v) for v in (Minv @ np.array([x, y, 1.0]))[:2]) for x, y in quad]

    # Пропорции КОРОБКИ слоя: из них считается высота объекта, если её оставили на автомате
    # (h=0). Коробка — это то, что corner-pin натягивает на квад, поэтому чужие пропорции
    # объекта плющат картинку ровно на их отношение.
    tmat = material_index(draft)[targets[0]["material_id"]][1]
    if tmat.get("width") and tmat.get("height"):
        obj = {"aspect": tmat["width"] / tmat["height"], **(obj or {})}
        if len(targets) > 1:
            log(f"пропорции взяты по первому слою ({tmat['width']}×{tmat['height']}): "
                "у остальных коробка своя, автовысота им может не подойти")

    work = os.path.expanduser(f"~/Movies/CapCut/User Data/ve_3d/{name}")
    homos, htimes, quad_obj, meta = solve_quad_3d(
        src, quad_src, work, t0_us=kf_time(vid, ov0), t1_us=kf_time(vid, ov1),
        ref_us=kf_time(vid, query), obj=obj, log=log)

    # Натягиваем слой на КВАД ОБЪЕКТА, а не на обведённый: при obj=None это одно и то же,
    # а при смещении/высоте объект едет своей плоскостью.
    cpin = {t["id"]: compute_cornerpin_ml(draft, vid, t, homos, htimes, quad_obj,
                                          ink=inks.get(t["id"])) for t in targets}
    corners = {k: v for k, v in cpin.items() if v}
    if not corners:
        raise ValueError("углы не построились: слой не пересекается с решённым диапазоном")
    write_progress(92, "Запись углов…")
    tid = next(iter(corners))
    written, synced = 0, live and live_cornerpin(by_id[tid], corners[tid], log=log)
    if not synced:
        # ⚠️ После ЖИВОГО заворота падать на файловую запись нельзя: компаунда на диске нет,
        # `inject_cornerpin` не найдёт прокси и тихо запишет ноль. Лучше громкий отказ.
        if inks and any(m["live"] for m in (mapping or {}).values()):
            raise ValueError("текст завёрнут живьём, но углы живым путём не легли: на диске "
                             "компаунда ещё нет, писать их в файл некуда. Повтори трек — "
                             "или убери ve_livewrap.txt — файловый путь надёжнее")
        if live:
            log("живой путь не вышел — пишу углы в драфт и переоткрываю")
        require_closed(proj_dir, log)
        written = inject_cornerpin(proj_dir, corners)
        if reopen and capcut_ui.is_running():
            try:
                capcut_ui.reopen(name)
            except Exception as e:  # noqa: BLE001 — углы записаны, переоткрытие вторично
                log(f"переоткрытие: {e}")
    nkf = sum(len(p) for c in corners.values() for p in c.values())
    log(f"3D-трек: слоёв {len(corners)}, ключей углов {nkf}, файлов {written}")
    write_progress(100, "Готово")
    log(f"════════ ГОТОВО за {time.time() - _t0:.0f}с ════════\n")
    return {"ok": True, "layers": len(corners), "keyframes": nkf, "live": bool(synced),
            "planar": True, "mode": "3d",
            **{k: v for k, v in meta.items() if k not in ("homos", "times")}}


def run_camera3d(proj_dir, layer_ids, depths, preset="parallax_pan", intensity=0.6,
                 want_scale=True, live=False, reopen=True, campath=None, make_layer=False,
                 dof=False, aperture=0.3, focus=1.96, scene=None, paths=None, starts=None, log=print):
    """3D-КАМЕРА (2.5D-параллакс): запечь виртуальную камеру в ключи выбранных слоёв.
    scene — токен сцены: None/"new" → новая камера (свежий токен); иначе перезапекаем существующую (реюз токена).
    Каждая сцена = свой трек «Камера 3D · <tok>» + уник. PNG + блок в риге; другие сцены НЕ трогаются (мерж)."""
    import camera3d
    if isinstance(layer_ids, str):
        layer_ids = [layer_ids]
    depths = list(depths) + [0.5] * max(0, len(layer_ids) - len(depths))
    name = os.path.basename(proj_dir)
    scene_tok = scene if (scene and scene != "new") else uuid.uuid4().hex[:8].upper()   # идентичность сцены
    was_open = _project_open(proj_dir)
    _t0 = time.time()
    write_progress(5, "3D-камера: чтение проекта…")
    log(f"\n════════ 3D-КАМЕРА ({preset}, сила {intensity:.2f}{', LIVE' if live else ''}) ════════")
    pre_id2path = {}   # ДО закрытия: id→путь (id из UI ещё валиден); после close id компаундов сменятся
    if not live:
        try:
            _pre = read_draft(proj_dir)
            for t in _pre["tracks"]:
                for s in t.get("segments", []):
                    p = seg_object_path(_pre, s)
                    if p:
                        pre_id2path[s["id"]] = p
        except (OSError, ValueError, KeyError):
            pass
        close_to_home(proj_dir, log)
    draft = read_draft(proj_dir)
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    by_start = {}   # позиция на таймлайне — резолв беспутных (текст) объектов
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            by_start.setdefault(s["target_timerange"]["start"], s)
    by_path = {}   # резолв по ПУТИ (стабилен через сохранение); путь = из UI ИЛИ из pre-close карты по id
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            p = seg_object_path(draft, s)
            if p:
                by_path.setdefault(p, s)
    paths = list(paths or []) + [""] * max(0, len(layer_ids) - len(paths or []))
    starts = list(starts or []) + [None] * max(0, len(layer_ids) - len(starts or []))
    segs = []
    for i, d, pth, st in zip(layer_ids, depths, paths, starts):
        pth = pth or pre_id2path.get(i)                          # путь из UI ИЛИ из pre-close карты по id
        s = (by_path.get(pth) if pth else None) or by_id.get(i) \
            or (by_start.get(st) if st is not None else None)     # путь → id → позиция (текст без пути)
        if s:
            segs.append((s, d))
    if not segs:
        raise ValueError("слои не найдены в драфте (переоткрой проект в тул)")
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    handles = {}   # беспутные (текст) объекты → PNG-ручка; при DoF заворачиваем и драйвим компаунд по ней
    if dof:
        for s, _ in segs:
            if not seg_object_path(draft, s):
                handles[s["id"]] = _make_handle_png(cw, ch)
    # общее окно камеры = объединение таймрейнджей выбранных слоёв (камера синхронна для всех)
    w0 = min(s["target_timerange"]["start"] for s, _ in segs)
    w1 = max(s["target_timerange"]["start"] + s["target_timerange"]["duration"] for s, _ in segs)
    span = max(1, w1 - w0)
    write_progress(45, "Запекаю движение камеры…")
    N = camera3d.SAMPLES
    if campath:   # СВОБОДНЫЙ путь камеры из превью (ключи в timeline-µs) — берёт верх над пресетом
        log(f"свободный путь камеры: {len(campath)} ключей")
        ct = sorted(int(k["t"]) for k in campath)
        blayers = []
        for seg, depth in segs:
            tr = seg["clip"]["transform"]
            st, dur = seg["target_timerange"]["start"], seg["target_timerange"]["duration"]
            # ключи слоя РОВНО на временах ключей камеры (внутри окна слоя) + границы окна — столько же, сколько у камеры,
            # а не 60 плотных. Для пана проекция линейна во времени → точно; для сильного зума между ключами линия
            # чуть срежет кривизну (для этого есть плотные пресеты). w0/w1 гарантируют покрытие всего окна слоя.
            times_tl = sorted({st, st + dur} | {t for t in ct if st <= t <= st + dur})
            samples = [(kf_time(seg, t), t) for t in times_tl]
            blayers.append({"id": seg["id"], "bx": tr["x"] * cw / 2.0, "by": camera3d.SIGN_Y * tr["y"] * ch / 2.0,
                            "base_scale": seg["clip"]["scale"]["x"], "base_rot": seg["clip"].get("rotation", 0.0),
                            "z": camera3d.depth_to_z(depth), "samples": samples})
        curves = camera3d.bake_path(cw, ch, blayers, campath, want_scale=want_scale)
    else:
        curves = {}
        for seg, depth in segs:
            tr = seg["clip"]["transform"]
            L = {"id": seg["id"], "bx": tr["x"] * cw / 2.0, "by": camera3d.SIGN_Y * tr["y"] * ch / 2.0,
                 "base_scale": seg["clip"]["scale"]["x"], "base_rot": seg["clip"].get("rotation", 0.0),
                 "z": camera3d.depth_to_z(depth)}
            st, dur = seg["target_timerange"]["start"], seg["target_timerange"]["duration"]
            times = [(kf_time(seg, int(st + dur * k / N)), (st + dur * k / N - w0) / span)
                     for k in range(N + 1)]
            curves.update(camera3d.bake(cw, ch, [L], times, preset=preset,
                                        intensity=intensity, want_scale=want_scale))
    nkf = sum(len(c[0]) for c in curves.values())
    write_progress(97, "Запись ключей…")
    cam_layer = None
    dof_info = None
    if live:
        # Порядок как в run_track: дилиб заливает ключи в ТРЕК документа, а перечитает их движок
        # только по команде. Замер 2026-08-09: без неё движок не синкает НИЧЕГО (0 своих
        # TEClip.setKeyframe), после ОДНОЙ — все клипы сцены разом.
        # ⚠️ Замер 2026-08-09: на ЭТОМ пути команда пока не доходит — дилиб после auto_apply сам
        # лезет в старый костыль render_sync, и тот убивает канал (в логе `WRAP принят …
        # curEditSeq=0x4d55545800000000` — мусор вместо указателя), после чего молчат ВСЕ гейты,
        # а CapCut умер на следующем «домой». Панель этой ручкой (/api/camera3d) не пользуется —
        # у неё сущность камеры (camera.py), где всё это работает. Чинить надо в дилибе.
        since = _log_size()
        write_live_curve(curves, log=log)
        tid, cur = next(((k, v) for k, v in curves.items() if any(v)), (None, None))
        prop, pairs = _live_prop(cur) if cur else (None, None)
        if prop and _log_wait("AUTO created", since):
            live_sync(by_id[tid], prop, pairs[len(pairs) // 2][0], log=log)
        try:
            os.remove(VE_CURVE_PATH)   # оставленный файл выстрелит на открытии ЛЮБОГО проекта
        except OSError:
            pass
    else:
        if not dof:
            inject_curves(proj_dir, curves)   # DoF: объекты уедут в компаунды — документ-ключи на них смысла нет
        if (make_layer or dof) and campath:   # слой-камера + риг (для DoF обязателен: параллакс/блюр живут вживую)
            try:
                cam_layer = add_camera_layer(proj_dir, cw, ch, w0, w1, campath, scene_tok, log=log)
                if cam_layer:   # rig для LIVE-DRIVE: камеру дилиб ловит по ПУТИ PNG (уник. на сцену), объекты — по ПУТИ ИСХОДНИКА
                    mi = material_index(draft)                       # (getFilePath стабилен: не зависит от position/scale/persist)
                    rig = []
                    for s, dp in segs:
                        _, mat = mi.get(s["material_id"], (None, {}))
                        path = (mat.get("path") or mat.get("media_path") or "").strip() or handles.get(s["id"], "")
                        rig.append({"id": s["id"], "z": camera3d.depth_to_z(dp),
                                    "bx": s["clip"]["transform"]["x"] * cw / 2.0,
                                    "by": camera3d.SIGN_Y * s["clip"]["transform"]["y"] * ch / 2.0,
                                    "base_scale": s["clip"]["scale"]["x"],
                                    "base_rot": s["clip"].get("rotation", 0.0), "path": path,
                                    "tstart": s["target_timerange"]["start"],
                                    "sstart": s.get("source_timerange", {}).get("start", 0) or 0,
                                    "speed": s.get("speed", 1.0) or 1.0})   # источник-время = (timeline-tstart)*speed
                    rig_upsert_scene(cw, ch, name, cam_layer, w0, cam_png_path(cw, ch, scene_tok), rig, log=log)   # МЕРЖ блока сцены
            except Exception as e:  # noqa: BLE001 — слой-камера опционален; ключи слоёв уже записаны
                log(f"слой-камеру не создал ({e}) — сам параллакс записан")
        if dof and cam_layer:   # ГЛУБИНА РЕЗКОСТИ: завернуть выбранные объекты в компаунды с блюром + вживить DoF
            write_progress(98, "Глубина резкости: заворот + блюр…")
            try:
                import dof as dofmod
                dof_info = dofmod.wrap_and_enable(proj_dir, [s["id"] for s, _ in segs],
                                                  aperture=aperture, focus=focus, handles=handles, log=log)
            except Exception as e:  # noqa: BLE001 — камера уже записана; DoF отдельно
                log(f"DoF-заворот не удался ({e}) — камера записана, глубина резкости не применена")
    if not live and reopen and (was_open or capcut_ui.is_running()):
        write_progress(99, "Открываю проект…")
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001 — ключи уже записаны
            log(f"не смог авто-открыть ({e}) — открой проект вручную")
    if dof_info and dof_info.get("bake_eids"):   # CapCut сменит id блюр-эффектов при открытии → фоновый реген карты
        import dof as dofmod
        dofmod.refresh_dof_map_async(proj_dir, dof_info["bake_eids"], log=log)
    if cam_layer:   # запомнить сетап ЭТОЙ сцены (по токену) — для восстановления и добавления объектов
        scene_save(proj_dir, scene_tok, cam_layer,
                   [{"path": seg_object_path(draft, s) or handles.get(s["id"], ""), "depth": dp} for s, dp in segs],
                   campath, {"on": bool(dof), "focus": focus, "aperture": aperture})
    write_progress(100, "Готово")
    log(f"3D-камера '{preset}': слоёв {len(curves)}, ключей {nkf}, {time.time() - _t0:.0f}с")
    return {"layers": len(curves), "keyframes": nkf, "preset": preset,
            "live": bool(live), "applied": True, "covered": 1.0, "cam_layer": cam_layer,
            "dof": dof_info, "scene": scene_tok}


def run_add_object(proj_dir, additions, focus=1.96, aperture=0.3, scene=None, reopen=True, log=print):
    """ИНКРЕМЕНТАЛЬНО добавить объекты в КОНКРЕТНУЮ сцену (scene=токен): завернуть новые в компаунды с блюром,
    дописать их в БЛОК ЭТОЙ сцены в риге, регенить карты — БЕЗ перезапекания камеры и без трогания других сцен.
    additions=[{id,depth}]. scene=None → первая/единственная сцена (назад-совместимость)."""
    import camera3d
    import compound
    import dof as dofmod
    name = os.path.basename(proj_dir)
    was_open = _project_open(proj_dir)
    if not os.path.exists(VE_RIG_PATH):
        raise RuntimeError("нет рига — сначала запеки камеру")
    scene_tok = scene
    if not scene_tok:                          # не указана — берём первую сцену рига
        _, _, _blocks = _rig_parse()
        scene_tok = _tok_from_png(_blocks[0][0]) if _blocks else ""
    # ДО закрытия снимаем карту id→путь: id из UI ещё валиден в текущей сессии; после close CapCut пересоздаст id
    # компаундов. Так работает и СТАРЫЙ UI (шлёт только id), и новый (шлёт path). Резолв потом — по стабильному пути.
    pre_id2path = {}
    try:
        _pre = read_draft(proj_dir)
        for t in _pre["tracks"]:
            for s in t.get("segments", []):
                p = seg_object_path(_pre, s)
                if p:
                    pre_id2path[s["id"]] = p
    except (OSError, ValueError, KeyError):
        pass
    write_progress(10, "Добавляю объект: закрываю проект…")
    close_to_home(proj_dir, log)
    draft = read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    by_id = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    by_start = {}   # позиция на таймлайне — резолв беспутных (текст) объектов, чей id мог пересоздаться
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            by_start.setdefault(s["target_timerange"]["start"], s)
    mi = material_index(draft)
    draft_ids = {m["id"] for m in draft["materials"].get("drafts", []) or []}
    # РЕЗОЛВ ПО ПУТИ (стабилен через сохранение); путь = из UI ИЛИ из pre-close карты по устаревшему id.
    by_path = {}
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            p = seg_object_path(draft, s)
            if p:
                by_path.setdefault(p, s)
    new_layers, wrap_ids, scene_layers, skipped, handles = [], [], [], [], {}
    for a in additions:
        pth = a.get("path") or pre_id2path.get(a.get("id"))   # путь из UI ИЛИ из pre-close карты по (устаревшему) id
        st = a.get("start")
        s = (by_path.get(pth) if pth else None) or by_id.get(a.get("id")) \
            or (by_start.get(st) if st is not None else None)   # беспутный текст → по позиции
        if not s:
            skipped.append("(не найден)"); continue
        is_comp = bool(set(s.get("extra_material_refs", [])) & draft_ids)   # уже компаунд?
        if is_comp:
            # УЖЕ компаунд (с блюром внутри) — НЕ заворачиваем повторно (компаунд-в-компаунде хрупок: если
            # его combination осиротела, внутренний proxy повисает → «Media Not Found»). Просто регистрируем:
            # блюр драйвит DoF-контроллер через карты, параллакс — по внутреннему пути. Ключи на этом же proxy.
            path = seg_object_path(draft, s)
            if not path:
                skipped.append("компаунд без резолв-пути"); continue
        else:
            _, mat = mi.get(s["material_id"], (None, {}))
            path = (mat.get("path") or mat.get("media_path") or "").strip()
            if not path:   # текст/группа без исходника → PNG-ручка, ключи ставим на компаунде (не на объекте)
                path = _make_handle_png(cw, ch); handles[s["id"]] = path
            wrap_ids.append(s["id"])   # заворачиваем ТОЛЬКО не-компаунды (одноуровнево)
        depth = float(a.get("depth", 0.5))
        new_layers.append({"id": s["id"], "z": camera3d.depth_to_z(depth),
                           "bx": s["clip"]["transform"]["x"] * cw / 2.0,      # трансформ верхнего (proxy) сегмента
                           "by": camera3d.SIGN_Y * s["clip"]["transform"]["y"] * ch / 2.0,
                           "base_scale": s["clip"]["scale"]["x"], "base_rot": s["clip"].get("rotation", 0.0),
                           "path": path, "tstart": s["target_timerange"]["start"],
                           "sstart": s.get("source_timerange", {}).get("start", 0) or 0,
                           "speed": s.get("speed", 1.0) or 1.0})
        scene_layers.append({"path": path, "depth": depth})
    if not new_layers:
        raise ValueError("не найдены объекты для добавления: %s" % ", ".join(dict.fromkeys(skipped))
                         if skipped else "нет объектов для добавления")
    if wrap_ids:
        write_progress(45, "Заворачиваю + блюр…")
        # handles: беспутным (текст) — PNG-ручка в proxy; nest=False — одноуровневый компаунд
        compound.wrap_in_project(proj_dir, wrap_ids, blur_value=0.0, nest=False, handles=handles, log=log)
    rig_append_to_scene(cam_png_path(cw, ch, scene_tok), new_layers, log=log)   # в БЛОК ЭТОЙ сцены
    write_progress(75, "Обновляю карты…")
    pairs = dofmod.write_dof_map(proj_dir); dofmod.write_seg_map(proj_dir)
    scene_add_layers(proj_dir, scene_tok, scene_layers)
    write_progress(99, "Открываю проект…")
    if reopen and (was_open or capcut_ui.is_running()):
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001 — карты/риг уже записаны
            log(f"не смог авто-открыть ({e}) — открой проект вручную")
    dofmod.refresh_dof_map_async(proj_dir, [l.split("\t")[0] for l in pairs], log=log)
    write_progress(100, "Готово")
    log(f"добавлено объектов: {len(new_layers)} (завёрнуто {len(wrap_ids)}, компаундов как есть {len(new_layers) - len(wrap_ids)})")
    return {"added": len(new_layers)}


def _plan_split(draft, cw, ch, depth_by_path, log=print):
    """ЧИСТЫЙ разбор дублей на независимые сцены (без файлового I/O — тестируемо на dict).
    Возвращает (mat_rewrites{material_id: new_path}, scene_blocks[(cam_id, cam_w0, cam_png, [layer_dict])], created_pngs).
    Каждой камере-копии — свой cam_png; каждому объекту-копии — своя PNG-ручка на proxy (уникальный путь = дилиб различает)."""
    import camera3d
    US = 1_000_000
    draft_ids = {m["id"] for m in draft["materials"].get("drafts", []) or []}
    vids = {m["id"]: m for m in draft["materials"].get("videos", []) or []}

    def mpath(s):
        m = vids.get(s.get("material_id"), {})
        return m.get("path", "") or m.get("media_path", "")

    # 1) камера-сегменты -> сцены (окно = target_timerange). Токен на сцену.
    cams = []
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            if "cam3d" in os.path.basename(mpath(s)):
                tr = s["target_timerange"]
                cams.append((tr["start"], tr["start"] + tr["duration"], s))
    cams.sort(key=lambda x: x[0])
    if len(cams) < 2:
        raise ValueError("нашёл < 2 камер — дублировать нечего (это уже одна сцена)")

    mat_rewrites, scene_blocks, pngs = {}, [], []

    def pop_depth(pool, rp):   # z для объекта по реальному пути; пул на сцену (для одинаковых путей — по порядку)
        vals = pool.get(rp)
        if vals:
            return vals.pop(0)
        return (depth_by_path.get(rp) or [0.5])[0]

    def window_of(tstart):
        for i, (a, b, _) in enumerate(cams):
            if a <= tstart < b:
                return i
        return -1

    # 2) объекты-компаунды по сценам
    per_scene = {}
    for t in draft["tracks"]:
        for s in t.get("segments", []):
            if "cam3d" in os.path.basename(mpath(s)) or "dof3d" in os.path.basename(mpath(s)):
                continue                                              # камера/DoF — не объект
            if not (set(s.get("extra_material_refs", [])) & draft_ids):
                continue                                              # не компаунд — параллакс живёт в компаундах
            sc = window_of(s["target_timerange"]["start"])
            if sc < 0:
                continue
            per_scene.setdefault(sc, []).append(s)

    for i, (a, b, cseg) in enumerate(cams):
        tok = uuid.uuid4().hex[:8].upper()
        cam_png = cam_png_path(cw, ch, tok)
        mat_rewrites[cseg["material_id"]] = cam_png                  # камере-копии — свой PNG
        pngs.append((cam_png, "cam"))
        pool = {p: list(zs) for p, zs in depth_by_path.items()}      # свежий пул глубин НА СЦЕНУ (копии идентичны)
        layers = []
        for s in per_scene.get(i, []):
            rp = seg_object_path(draft, s) or ""                    # реальный исходник (для глубины)
            handle = os.path.expanduser("~/Movies/CapCut/User Data/plugin_assets/obj_%s.png" % uuid.uuid4().hex[:10])
            mat_rewrites[s["material_id"]] = handle                  # объекту-копии — своя ручка на proxy
            pngs.append((handle, "handle"))
            clip = s.get("clip") or {"transform": {"x": 0, "y": 0}, "scale": {"x": 1.0}}
            layers.append({"id": s["id"], "z": pop_depth(pool, rp),
                           "bx": clip["transform"]["x"] * cw / 2.0,
                           "by": camera3d.SIGN_Y * clip["transform"]["y"] * ch / 2.0,
                           "base_scale": clip.get("scale", {}).get("x", 1.0),
                           "base_rot": clip.get("rotation", 0.0),
                           "path": handle, "tstart": s["target_timerange"]["start"],
                           "sstart": s.get("source_timerange", {}).get("start", 0) or 0,
                           "speed": s.get("speed", 1.0) or 1.0})
        scene_blocks.append((cseg["id"], a, cam_png, layers))
        log("сцена #%d [%.2f–%.2fс]: камера %s, объектов %d" % (i, a / US, b / US, tok, len(layers)))
    return mat_rewrites, scene_blocks, pngs


def split_scenes(proj_dir, reopen=True, log=print):
    """Разбить ДУБЛИ одной сцены (скопированные на таймлайне) на НЕЗАВИСИМЫЕ сцены. CapCut при дублировании
    тащит те же пути (камера-PNG + исходники объектов) → дилиб маршрутит по пути и видит их как ОДНУ сцену.
    Здесь каждой копии выдаём уникальный путь: свой cam3d_<tok>.png камере и obj_<uuid>.png-ручку на proxy
    КАЖДОГО объекта. Материалы CapCut уже клонировал per-копию — правим пути по material_id во всех авто-копиях."""
    import compound  # noqa: F401 — единый неймспейс зависимостей
    import dof as dofmod
    name = os.path.basename(proj_dir)
    was_open = _project_open(proj_dir)
    write_progress(10, "Разделяю сцены: закрываю проект…")
    close_to_home(proj_dir, log)
    root = read_draft(proj_dir)
    cw, ch = root["canvas_config"]["width"], root["canvas_config"]["height"]
    # глубины из ТЕКУЩЕГО рига (по реальному пути; для одинаковых путей — список z)
    depth_by_path = {}
    _h, _dof, blocks = _rig_parse()
    for _png, blk in blocks:
        for ln in blk:
            if ln.startswith("LAYER "):
                p = ln.split(None, 10)
                depth_by_path.setdefault(p[-1].strip(), []).append(float(p[2]))
    write_progress(40, "Раздаю уникальные пути копиям…")
    mat_rewrites, scene_blocks, pngs = _plan_split(root, cw, ch, depth_by_path, log=log)
    # применить переписи путей во ВСЕХ авторитетных копиях (material_id стабилен между root и Timelines)
    files = [os.path.join(proj_dir, "draft_info.json")] + \
        glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json"))
    bak = 0
    for fp in files:
        d = read_draft_path(fp)
        touched = False
        for mm in d["materials"].get("videos", []) or []:
            np_ = mat_rewrites.get(mm.get("id"))
            if np_:
                mm["path"] = np_
                mm["media_path"] = np_
                touched = True
        if touched:
            if not os.path.exists(fp + ".presplit.bak"):
                open(fp + ".presplit.bak", "wb").write(open(fp, "rb").read()); bak += 1
            with open(fp, "wb") as f:
                f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    # создать все PNG (камеры + ручки)
    import cv2
    import numpy as np
    for p, _kind in pngs:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.exists(p):
            cv2.imwrite(p, np.zeros((int(ch), int(cw), 4), np.uint8))
    # пересобрать риг: свежий header + сохранённый глобальный DOF + блок на сцену
    _rig_write([f"CANVAS {int(cw)} {int(ch)}", f"PROJECT {name}"], _dof,
               [(cam_png, [f"CAMERA {cam_id} {int(w0)} {cam_png}"] + [_rig_layer_line(L) for L in layers])
                for (cam_id, w0, cam_png, layers) in scene_blocks])
    write_progress(75, "Обновляю карты…")
    pairs = dofmod.write_dof_map(proj_dir)
    dofmod.write_seg_map(proj_dir)
    write_progress(99, "Открываю проект…")
    if reopen and (was_open or capcut_ui.is_running()):
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001 — риг/карты уже записаны
            log(f"не смог авто-открыть ({e}) — открой проект вручную")
    dofmod.refresh_dof_map_async(proj_dir, [l.split("\t")[0] for l in pairs], log=log)
    write_progress(100, "Готово")
    log("РАЗДЕЛЕНО на %d независимых сцен (бэкапов %d)" % (len(scene_blocks), bak))
    return {"scenes": len(scene_blocks), "objects": sum(len(l) for _, _, _, l in scene_blocks)}


def _points_to_source(draft, seg, mat, points_canvas):
    """Точки из координат канваса (композита) -> пиксели исходного видео сегмента."""
    import cv2
    import numpy as np

    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    Minv = cv2.invertAffineTransform(
        np.array(_layer_matrix(seg["clip"], mat["width"], mat["height"], cw, ch), np.float32))
    return [[float(p[0]), float(p[1])] for p in
            (Minv @ np.array([x, y, 1.0]) for x, y in points_canvas)]


def run_roto_preview(proj_dir, seg_id, timeline_us, points_canvas, labels):
    """Живой предпросмотр маски: точки на кадре timeline_us -> PNG подсветки в координатах канваса."""
    import cv2
    import numpy as np

    import roto
    draft = read_draft(proj_dir)
    seg = next((s for t in draft["tracks"] for s in t.get("segments", []) if s["id"] == seg_id), None)
    if not seg:
        raise ValueError("сегмент не найден")
    _, mat = material_index(draft)[seg["material_id"]]
    path = mat.get("path")
    if not path or not os.path.exists(path):
        raise ValueError("исходник видео не найден")
    pts_src = _points_to_source(draft, seg, mat, points_canvas)
    mask, pw, ph = roto.preview_mask(path, kf_time(seg, timeline_us), pts_src, labels)

    # маска (pw,ph в px извлечённого кадра) -> подсветка в координатах канваса
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    M = np.array(_layer_matrix(seg["clip"], mat["width"], mat["height"], cw, ch), np.float32)
    M[:, :2] *= mat["width"] / pw  # proc-кадр -> исходник -> канвас
    warped = cv2.warpAffine((mask.astype(np.uint8) * 255), M, (cw, ch))
    bgra = np.zeros((ch, cw, 4), np.uint8)
    sel = warped > 127
    bgra[sel] = (255, 0, 255, 120)  # magenta полупрозрачно
    ok, buf = cv2.imencode(".png", bgra)
    return buf.tobytes(), cw, ch


def _swap_material(draft, seg_id, cutout_path, w, h, dur_us):
    """Заменить материал сегмента на вырезанный .mov (новый video-материал + repoint)."""
    import copy
    import uuid as _uuid

    segs = {s["id"]: s for t in draft["tracks"] for s in t.get("segments", [])}
    seg = segs.get(seg_id)
    vids = draft["materials"].get("videos", [])
    old = next((m for m in vids if seg and m["id"] == seg["material_id"]), None)
    if old is None:
        return False
    new = copy.deepcopy(old)
    new["id"] = str(_uuid.uuid4()).upper()
    new["path"] = cutout_path
    new["material_name"] = "roto_" + str(old.get("material_name") or "clip")
    new["width"], new["height"], new["duration"] = int(w), int(h), int(dur_us)
    vids.append(new)
    seg["material_id"] = new["id"]
    seg["source_timerange"] = {"start": 0, "duration": int(dur_us)}
    return True


def run_roto(proj_dir, seg_id, points_canvas, labels, start_us=None, log=print):
    """Roto Brush (SAM 2) по точкам -> вырез по всему клипу -> АВТОЗАМЕНА материала сегмента
    на вырезанный .mov (слой в проекте становится без фона). Точки в координатах канваса."""
    import roto
    if not roto.available():
        raise ValueError("нет SAM 2 или ffmpeg для вырезки")
    draft = read_draft(proj_dir)
    seg = next((s for t in draft["tracks"] for s in t.get("segments", []) if s["id"] == seg_id), None)
    if not seg:
        raise ValueError("сегмент не найден")
    _, mat = material_index(draft)[seg["material_id"]]
    path = mat.get("path")
    if not path or not os.path.exists(path):
        raise ValueError("исходник видео не найден")
    pts_src = _points_to_source(draft, seg, mat, points_canvas)
    src = seg.get("source_timerange") or {"start": 0, "duration": mat.get("duration", 0)}
    t0, t1 = src["start"], src["start"] + src["duration"]
    query = kf_time(seg, start_us) if start_us is not None else t0
    base = (mat.get("material_name") or "clip").rsplit(".", 1)[0]
    out = os.path.join(proj_dir, f"roto_{base}.mov")
    out_fps = min(round(draft.get("fps", 30)), 30)

    # 1) вырезать (проект ещё открыт — быстрее для пользователя)
    res = roto.remove_background(path, t0, t1, pts_src, labels, out, query_us=query, out_fps=out_fps, log=log)
    dur_us = round(res["frames"] / res["fps"] * 1e6)

    # 2) закрыть проект, заменить материал в обоих файлах, открыть обратно
    name = os.path.basename(proj_dir)
    was_open = _project_open(proj_dir)
    close_to_home(proj_dir, log)
    written = 0
    for p in [os.path.join(proj_dir, "draft_info.json")] + \
            glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = read_draft_path(p)
        if _swap_material(d, seg_id, out, res["size"][0], res["size"][1], dur_us):
            shutil.copy2(p, p + ".preroto.bak")
            with open(p, "wb") as f:
                f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            written += 1
    log(f"вырезано {res['frames']} кадров, слой заменён в {written} файла(ов)")
    if was_open or capcut_ui.is_running():
        log("открываю проект...")
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001 — трек уже записан; не роняем результат из-за открытия
            log(f"не смог авто-открыть ({e}) — открой проект вручную")
    return {**res, "replaced": written}


# ---------- CLI (fallback) ----------

def _pick(prompt, items, render):
    for i, it in enumerate(items):
        print(f"  [{i}] {render(it)}")
    while True:
        s = input(f"{prompt} [0-{len(items) - 1}]: ").strip()
        if s.isdigit() and int(s) < len(items):
            return items[int(s)]


def main():
    projs = list_projects()
    proj = _pick("Проект", projs[:15], lambda p: p["name"] + (" [открыт]" if p["open"] else ""))
    proj_dir = proj["dir"]
    close_to_home(proj_dir)

    draft = read_draft(proj_dir)
    mats = material_index(draft)
    segs = [s for t in draft["tracks"] for s in t.get("segments", [])]

    def label(s):
        kind, mat = mats.get(s["material_id"], ("?", {}))
        tr = s["target_timerange"]
        return f'{kind:8s} {tr["start"]/US:5.2f}-{(tr["start"]+tr["duration"])/US:5.2f}s  {seg_name(kind, mat)[:40]}'

    videos = [s for s in segs if mats.get(s["material_id"], ("?", {}))[1].get("path")]
    print("\nВ каком видео трекаем:")
    vid = _pick("Видео", videos, label)
    print("\nКакой слой следует за объектом:")
    target = _pick("Слой", [s for s in segs if s["id"] != vid["id"]], label)

    _, vmat = mats[vid["material_id"]]
    tl0, tl1 = overlap_window(vid, target)
    if tl1 <= tl0:
        sys.exit("клипы не пересекаются по времени")
    points, meta = track_video(vmat["path"], kf_time(vid, tl0), kf_time(vid, tl1))  # selectROI
    if len(points) < 2:
        sys.exit("трекинг не дал точек")
    xs, ys, ss = compute_curves(draft, vid, target, points)
    written = inject_curves(proj_dir, {target["id"]: (xs, ys, ss, None)})
    print(f"ключей {len(xs)} в {written} файла(ов); покрытие {meta['covered']:.0%}, стоп: {meta['reason']}")
    print("готово" if capcut_ui.reopen(proj["name"], os.path.join(proj_dir, ".locked"))
          else f"открой «{proj['name']}» вручную")


def _selftest():
    import numpy as np

    t = list(range(11))
    assert decimate(t, [[2 * i for i in t]], [0.001]) == [0, 10], "прямая -> только концы"
    peak = [0, 1, 2, 3, 4, 3, 2, 1, 0]
    keep = decimate(list(range(9)), [peak], [0.5])
    assert keep[0] == 0 and keep[-1] == 8 and 4 in keep, f"излом не сохранён: {keep}"
    noisy = [(-1.0) ** i for i in range(50)]
    assert np.var(smooth(noisy, 2.0)) < np.var(noisy), "сглаживание не уменьшило дребезг"

    # Конвертер в РОДНОЙ формат: доли кадра, y ВНИЗ. Сверено с их файлом численно
    # (cache.json box / image_size == data.json до 1e-6) — здесь держим ту же арифметику.
    b = native_boxes([(1_000_000, 360.0, 640.0, 1.0, 0.0)], (0, 0, 72, 128), (720, 1280))[0]
    assert b["pts"] == 1_000_000, b
    assert abs(b["left"] - 0.45) < 1e-9 and abs(b["right"] - 0.55) < 1e-9, b
    assert abs(b["top"] - 0.45) < 1e-9 and abs(b["bottom"] - 0.55) < 1e-9, b
    assert b["top"] < b["bottom"], "y должен идти ВНИЗ, как у CapCut"
    b2 = native_boxes([(0, 360.0, 640.0, 2.0, 0.0)], (0, 0, 72, 128), (720, 1280))[0]
    assert abs((b2["right"] - b2["left"]) - 2 * (b["right"] - b["left"])) < 1e-9, "масштаб не удвоил бокс"
    # зажим масштаба: сырой трекер иногда отдаёт «раздувание», от которого объект улетал за кадр
    bx = native_boxes([(0, 360.0, 640.0, 99.0, 0.0)], (0, 0, 72, 128), (720, 1280))[0]
    assert abs((bx["right"] - bx["left"]) - SCALE_CLAMP[1] * 0.1) < 1e-9, f"масштаб не зажат: {bx}"

    # Ветка «рендер не принял ключи -> переоткрыть»: важно, что 0 отличается от отсутствия файла.
    # Спутать их = либо лишние переоткрытия на каждом треке, либо молча несвежее превью.
    import tempfile
    global VE_RENDER_STATUS_PATH
    keep_path = VE_RENDER_STATUS_PATH
    try:
        with tempfile.TemporaryDirectory() as d:
            VE_RENDER_STATUS_PATH = os.path.join(d, "st.txt")
            assert _render_pushed(timeout=0.3) == -1, "нет файла -> -1 (не трогаем проект)"
            open(VE_RENDER_STATUS_PATH, "w").write("0\n")
            assert _render_pushed() == 0, "0 -> пуш не прошёл, нужно переоткрытие"
            open(VE_RENDER_STATUS_PATH, "w").write("4\n")
            assert _render_pushed() == 4, "4 -> рендер принял, переоткрытие не нужно"
    finally:
        VE_RENDER_STATUS_PATH = keep_path
    print("selftest OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        main()
