#!/usr/bin/env python3
"""Сущность «3D-камера»: камера ВЛАДЕЕТ привязанными ДОРОЖКАМИ.

Модель (решения автора, 2026-07-20):
  • Камера = прозрачный слой-носитель позы на своём треке «Камера 3D · <tok>». Только видеослой
    умеет ключуемую позу: у родной «Корректировки» (`lvve::SegmentPictureAdjust`) геометрии нет
    вообще — ни clip, ни transform, а ключи своего вида KeyframeAdjust (замерено по nm).
  • Привязка ПОТРЕКОВАЯ и ЯВНАЯ, хранится списком в сайдкаре → монтаж не меняет состав молча
    (позиционное членство требовало бы сверки после каждого перетаскивания).
  • При СОЗДАНИИ камера авто-привязывает все дорожки под собой, глубина по порядку (выше = ближе).
    Дальше только руками: позже добавленные дорожки сами не влезают.
  • Ключи живут на камере; у объекта они ПОЯВЛЯЮТСЯ при привязке и УБИРАЮТСЯ при отвязке.
    Своя анимация слоя при привязке УМИРАЕТ (решение автора) → снимок/восстановление не нужны.
  • Глубина и привязка меняются ЖИВЬЁМ: переписываем ve_rig.txt, дилиб перечитывает его по mtime.
    Переоткрытие проекта нужно ТОЛЬКО для создания/удаления самого слоя-камеры (запись в драфт).

ponytail: состав рига = разворот дорожек в сегменты. Дилиб про дорожки не знает и не должен —
он и так драйвит плоский список LAYER, так что потрековая модель не стоила ему ни строчки.
"""
import copy
import glob
import json
import os
import shutil
import threading
import time
import uuid

import camera3d
import capcut_track as ct

NEAR_TOP = True   # верхняя дорожка = ближняя к камере (как в AE читается слой «спереди»)


# ── дорожки ───────────────────────────────────────────────────────────────────
def _is_service(name):
    """Служебные треки плагина (камера, контроллер DoF) — они не объекты сцены."""
    return ct._is_cam_track(name) or (name or "").startswith("DoF")


def _media_name(draft, seg):
    """Имя исходника сегмента для подписи дорожки (у дорожек CapCut своих имён обычно нет)."""
    p = ct.seg_object_path(draft, seg)
    if p:
        return os.path.basename(p)
    _, mat = ct.material_index(draft).get(seg.get("material_id"), (None, {}))
    return (mat.get("material_name") or "").strip()


def tracks_of(draft):
    """Видеодорожки проекта СНИЗУ ВВЕРХ: [{id, label, media, nsegs, service, tok, start, end}].
    Порядок в draft['tracks'] = порядок слоёв снизу вверх (основная дорожка первая)."""
    out = []
    vids = [t for t in draft.get("tracks", []) if t.get("type") == "video"]
    nvid = len(vids)
    for i, t in enumerate(draft.get("tracks", [])):
        if t.get("type") != "video":
            continue
        segs = t.get("segments", []) or []
        # Имя дорожки CapCut может обнулить — камеру опознаём по материалу сегмента (см.
        # cam_name_of_track). Для НЕ-камеры вернётся '' и всё работает как раньше.
        name = ct.cam_name_of_track(draft, t) or (t.get("name") or "")
        # row = НОМЕР СТРОКИ В ТАЙМЛАЙНЕ СВЕРХУ, считая ВСЕ видеодорожки (включая служебные:
        # камеру и контроллер DoF). Именно так нумерует их вьюмодель шапки (trackIndex).
        # Без этого поля кнопке пришлось бы вычитать индекс из длины ОТФИЛЬТРОВАННОГО списка —
        # и она промахивалась на служебные дорожки, привязывая чужую (поймано на живом проекте).
        out.append({"id": t["id"], "name": name, "nsegs": len(segs), "index": i,
                    "row": nvid - 1 - vids.index(t),
                    "service": _is_service(name), "tok": ct._scene_tok_of_track(name) if ct._is_cam_track(name) else "",
                    "label": name or f"Дорожка {i + 1}",
                    "media": _media_name(draft, segs[0]) if segs else "",
                    "start": min([s["target_timerange"]["start"] for s in segs], default=0),
                    "end": max([s["target_timerange"]["start"] + s["target_timerange"]["duration"]
                                for s in segs], default=0)})
    return out


def track_of_seg(draft, seg_id):
    """Дорожка, которой принадлежит сегмент. Нужна «нитке»: жест начинается в панели,
    а заканчивается кликом по КЛИПУ в родном таймлайне — оттуда к нам приходит только id
    сегмента (проверенный канал selSegId), а привязка у нас потрековая."""
    for t in draft.get("tracks", []):
        if any(s.get("id") == seg_id for s in t.get("segments", []) or []):
            return t["id"]
    return None


def auto_depths(objs):
    """Глубина по порядку дорожек: верхняя = 0.0 (перед), нижняя = 1.0 (даль).
    Одна дорожка → 0.5 (середина): без разброса перспективы всё равно нет."""
    n = len(objs)
    if n == 1:
        return {objs[0]["id"]: 0.5}
    return {o["id"]: (1.0 - k / (n - 1.0)) if NEAR_TOP else (k / (n - 1.0))
            for k, o in enumerate(objs)}   # objs снизу вверх: k=0 (низ) → 1.0 даль


# ── сайдкар: привязки сцены ───────────────────────────────────────────────────
def _scenes(proj_dir):
    return ct._scene_load_side(proj_dir).get("scenes", {}) or {}


def _save_scene(proj_dir, tok, patch):
    with ct.SCENE_LOCK:   # чтение-правка-запись целиком: сюда пишут и панель, и спираль, и трекинг
        data = ct._scene_load_side(proj_dir)
        sc = data.setdefault("scenes", {}).setdefault(tok, {})
        sc.update(patch)
        ct._scene_write_side(proj_dir, data)
        return sc


def _binds(proj_dir, tok):
    """{track_id: depth} привязанных дорожек сцены."""
    return {k: float(v) for k, v in (_scenes(proj_dir).get(tok, {}).get("tracks", {}) or {}).items()}


# ── риг: разворот привязанных ДОРОЖЕК в плоский список сегментов ──────────────
def _layer(draft, seg, z, cw, ch):
    # Путь берём РЕКУРСИВНО (seg_object_path), а не с материала сегмента. У завёрнутого в сборный
    # клип объекта верхний сегмент — прокси, и путь у него ПУСТОЙ (это и есть маркер компаунда).
    # Дилиб сопоставляет слои с рендер-клипами ПО ПУТИ, поэтому пустой путь = ни одного совпадения:
    # «DRIVE wrote 0/3 layers», параллакс и глубина резкости мертвы. Ловится это только если
    # пересоздать камеру ПОСЛЕ заворачивания — тогда риг строится уже по прокси-сегментам.
    mi = ct.material_index(draft)
    _, mat = mi.get(seg["material_id"], (None, {}))
    path = (ct.seg_object_path(draft, seg)
            or mat.get("path") or mat.get("media_path") or "").strip()
    tr = seg["clip"]["transform"]
    # id — ВНУТРЕННЕГО сегмента (см. seg_object_id): у прокси сборного клипа дилибу нечего драйвить
    return {"id": ct.seg_object_id(draft, seg), "z": z, "bx": tr["x"] * cw / 2.0,
            "by": camera3d.SIGN_Y * tr["y"] * ch / 2.0,
            "base_scale": seg["clip"]["scale"]["x"], "base_rot": seg["clip"].get("rotation", 0.0),
            "path": path, "tstart": seg["target_timerange"]["start"],
            "sstart": seg.get("source_timerange", {}).get("start", 0) or 0,
            "speed": seg.get("speed", 1.0) or 1.0}


def _rig_scope(proj_dir, log=print):
    """ve_rig.txt ОДИН на машину, а проектов много. Блоки чужого проекта надо выкинуть:
    иначе дилиб держит чужие камеры, жрёт лимит слоёв (48) и может увести драйв на совпавший
    путь файла. В CapCut одновременно открыт один проект — риг честно скоупим по PROJECT."""
    name = os.path.basename(proj_dir)
    header, dof, blocks = ct._rig_parse()
    cur = next((l.split(None, 1)[1].strip() for l in header if l.startswith("PROJECT ")), None)
    if cur is not None and cur != name and (blocks or dof):
        ct._rig_write([f"PROJECT {name}"], None, [])
        log(f"риг: сброшены блоки чужого проекта «{cur}» → «{name}»")


def _cam_ease_from_doc(cam_seg):
    """Ручки безье кривой камеры из ДОКУМЕНТА → {px|py|sx: [(t, cubic, vti, vi, vto, vo), ...]}.
    control-точки CapCut уже в рендерном формате (right_control = (vto, vo)), берём как есть.
    cubic=1, если curveType не 'Line'/'Hold' (у прямых ручки нулевые — движок игнорит)."""
    prop = {"KFTypePositionX": "px", "KFTypePositionY": "py", "KFTypeScaleX": "sx"}
    out = {}
    for c in (cam_seg.get("common_keyframes") or []):
        key = prop.get(c.get("property_type"))
        if not key:
            continue
        rows = []
        for k in c.get("keyframe_list") or []:
            lc, rc = k.get("left_control") or {}, k.get("right_control") or {}
            cubic = 0 if (k.get("curveType") or "").startswith(("Line", "Hold")) else 1
            rows.append((int(k.get("time_offset", 0)), cubic,
                         float(lc.get("x", 0)), float(lc.get("y", 0)),
                         float(rc.get("x", 0)), float(rc.get("y", 0))))
        if rows:
            out[key] = sorted(rows)
    return out or None


def _rig_camease_lines(cam_ease):
    """Строки CAMEASE для рига. Одна на свойство: CAMEASE px t,c,vti,vi,vto,vo;t,c,...;"""
    if not cam_ease:
        return []
    lines = []
    for key, rows in cam_ease.items():
        pts = ";".join(f"{t},{c},{vti:.1f},{vi:.6f},{vto:.1f},{vo:.6f}"
                       for t, c, vti, vi, vto, vo in rows)
        lines.append(f"CAMEASE {key} {pts}")
    return lines


def write_rig(proj_dir, tok, log=print):
    """Собрать блок сцены в ve_rig.txt из ПРИВЯЗОК: дорожка → все её сегменты.
    Отвязанные (были в риге, теперь нет) уходят строками CLEAR — дилиб снимает с них ключи."""
    _rig_scope(proj_dir, log)
    draft = ct.read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    sc = _scenes(proj_dir).get(tok, {})
    cam_id, binds = sc.get("camId"), _binds(proj_dir, tok)
    if not cam_id:
        # САМОВОССТАНОВЛЕНИЕ: слой-камера в проекте есть, а ссылка на него в сайдкаре потерялась
        # (сайдкар лежит рядом с проектом и легко отстаёт: копирование проекта, откат, ручная
        # правка). Без camId риг собирался ПУСТЫМ и параллакс молча не работал — снаружи это
        # выглядело как «привязал дорожку, а эффекта нет». Ищем камеру по эффективному имени
        # (из материала, если имя дорожки CapCut сбросил).
        cam_id = next((s["id"] for t in draft["tracks"]
                       if ct._scene_tok_of_track(ct.cam_name_of_track(draft, t)) == tok
                       for s in (t.get("segments") or [])), None)
        if not cam_id:
            log(f"камера {tok}: слой-камера не найден в проекте — риг не собран")
            return 0
        _save_scene(proj_dir, tok, {"camId": cam_id})
        log(f"камера {tok}: camId восстановлен по слою ({cam_id[:8]})")
    by_track = {t["id"]: t for t in draft["tracks"]}
    tilts = sc.get("tilts") or {}   # наклон плоскости, как и глубина — свойство ДОРОЖКИ
    layers, live_ids = [], set()
    for tid, depth in binds.items():
        t = by_track.get(tid)
        if not t:
            continue   # дорожку удалили из проекта — привязка просто не разворачивается
        tx, ty = (tilts.get(tid) or [0.0, 0.0])[:2]
        for s in t.get("segments", []) or []:
            L = _layer(draft, s, camera3d.depth_to_z(depth), cw, ch)
            L["tiltx"], L["tilty"] = float(tx), float(ty)
            layers.append(L)
            live_ids.add(s["id"])
    cam_seg = next((s for t in draft["tracks"] for s in t.get("segments", []) if s["id"] == cam_id), None)
    w0 = cam_seg["target_timerange"]["start"] if cam_seg else 0
    # Кривые движения камеры БЕРЁМ ИЗ ДОКУМЕНТА, а не из рендер-синка. CapCut отдаёт ручки в
    # рендер лишь эпизодически (при активном редактировании ключа), поэтому дилиб их постоянно
    # терял и перспектива шла линейно. Документ — источник правды: тут кривая стабильна.
    # Формат control-точек в документе УЖЕ совпадает с рендерным (замерено 1:1: right_control =
    # (vto, vo)), поэтому конвертация не нужна.
    cam_ease = _cam_ease_from_doc(cam_seg) if cam_seg else None
    # Отвязанные = всё, что сцена вела раньше, но чего больше нет в составе, ПЛЮС ещё не снятые
    # с прошлого раза. Копим в сайдкаре, а не полагаемся на строки в риге: `rig_upsert_scene`
    # переписывает файл, а `_rig_parse` строки CLEAR не разбирает — при двух отвязках подряд
    # быстрее одного опроса дилиба (300мс) первая потерялась бы, и ключи остались бы навсегда.
    # Пере-выпуск безопасен: снятие ключей идемпотентно (пустой трек → clearEmptyKeyframes → no-op).
    pending = {i for i in (sc.get("driven") or []) if i not in live_ids}
    pending |= {i for i in (sc.get("pending_clear") or []) if i not in live_ids}
    since = ct._log_size()
    ct.rig_upsert_scene(cw, ch, os.path.basename(proj_dir), cam_id, w0,
                        ct.cam_png_path(cw, ch, tok), layers, log=log, pan=sc.get("pan"),
                        cam_ease=cam_ease, roll=sc.get("roll"))
    _rig_add_clears(sorted(pending))
    _save_scene(proj_dir, tok, {"driven": sorted(live_ids), "pending_clear": sorted(pending)})
    log(f"камера {tok}: дорожек {len(binds)}, сегментов {len(layers)}"
        + (f", снимаю ключи с {len(pending)}" if pending else ""))
    _sync_soon(proj_dir, tok, since, log=log)   # смена глубины/привязки — тоже до картинки, но в фоне
    return len(layers)


def _rig_add_clears(seg_ids):
    """Дописать CLEAR-строки в конец рига (дилиб снимет с этих сегментов наши кривые и забудет их)."""
    if not seg_ids:
        return
    with open(ct.VE_RIG_PATH, "a", encoding="utf-8") as f:
        for i in seg_ids:
            f.write(f"CLEAR {i}\n")


# ── операции ──────────────────────────────────────────────────────────────────
CURVE_LABELS = {"KFTypePositionX": ("px", "влево/вправо"),
                "KFTypePositionY": ("py", "вверх/вниз"),
                "KFTypeScaleX": ("sx", "приближение")}
CURVE_SAMPLES = 64      # точек полилинии на кривую — на ширине панели глазу хватает с запасом
GRID_BUDGET = 80        # RIG_MAXK в ve_hook.cpp: столько точек влезает в сетку перспективы
GRID_CAP = 26           # потолок дробления там же (persp_grid)


def _bez_y(p1, p2, u):
    """Значение нормированной безье-кривой в точке u. P0=(0,0), P3=(1,1), P1/P2 — УЖЕ нормированные
    ручки. Ньютон по x(s)=u, как в bez_at() дилиба, чтобы график и картинка не разошлись."""
    x1, y1 = p1
    x2, y2 = p2
    s = u
    for _ in range(12):
        m = 1 - s
        x = 3 * m * m * s * x1 + 3 * m * s * s * x2 + s ** 3 - u
        d = 3 * m * m * x1 + 6 * m * s * (x2 - x1) + 3 * s * s * (1 - x2)
        if -1e-9 < d < 1e-9:
            break
        s = min(1.0, max(0.0, s - x / d))
    m = 1 - s
    return 3 * m * m * s * y1 + 3 * m * s * s * y2 + s ** 3


def _cam_curve(raw):
    """Кривые камеры для графика в панели: полилиния + ключи + точки сетки перспективы.

    Нормируем ручки ЗДЕСЬ, а не в QML: control-точки CapCut — АБСОЛЮТНЫЕ смещения от ключа
    (x в микросекундах, y в единицах свойства), и ровно на этом месте дилиб когда-то читал их
    как доли интервала, из-за чего перспектива шла по чужой кривой. Одно место нормировки —
    один шанс ошибиться.

    Всё возвращаем в долях 0..1: u — время по длине кривой, w — значение в своём диапазоне.
    """
    times = _kf_times(raw)
    if len(times) < 2:
        return None
    t0, t1 = times[0], times[-1]
    span = t1 - t0
    out = {"props": [], "grid": []}
    for c in raw:
        key_label = CURVE_LABELS.get(c.get("property_type"))
        if not key_label:
            continue
        key, label = key_label
        kl = sorted(c.get("keyframe_list") or [], key=lambda k: k.get("time_offset", 0))
        if len(kl) < 2:
            continue
        vs = [float((k.get("values") or [0])[0]) for k in kl]
        vmin, vmax = min(vs), max(vs)
        # Свойство не меняется (напр. камера не ходит вверх-вниз): диапазона нет, нормировать не на что.
        # Кладём такую линию ПОСЕРЕДИНЕ поля, а не на нулевую отметку — на оси она читается как сбой
        # отрисовки, а посередине честно говорит «эта ось стоит».
        const = (vmax - vmin) < 1e-12
        rng = 1.0 if const else (vmax - vmin)
        base = vmin - 0.5 * rng if const else vmin
        pts, cubic_any, p1, p2 = [], False, (0.0, 0.0), (1.0, 1.0)
        for i in range(len(kl) - 1):
            ta, tb = kl[i].get("time_offset", 0), kl[i + 1].get("time_offset", 0)
            dt, dv = tb - ta, vs[i + 1] - vs[i]
            rc, lc = kl[i].get("right_control") or {}, kl[i + 1].get("left_control") or {}
            cubic = not (rc.get("x", 0) == 0 and lc.get("x", 0) == 0) and dt > 0
            flat = abs(dv) < 1e-12
            if cubic:
                cubic_any = True
                p1 = (min(1.0, max(0.0, rc.get("x", 0) / dt)), 0.0 if flat else rc.get("y", 0) / dv)
                p2 = (min(1.0, max(0.0, 1 + lc.get("x", 0) / dt)), 1.0 if flat else 1 + lc.get("y", 0) / dv)
            # чётное число сэмплов: на пресете 0.88/0.14 кривая в середине круче линейной в ~5.8 раза,
            # и без точки ровно на середине полилиния срезала бы самый крутой участок
            n = max(4, CURVE_SAMPLES // max(1, len(kl) - 1)) & ~1
            for q in range(n + 1):
                u = q / n
                w = _bez_y(p1, p2, u) if cubic else u
                pts.append([(ta + (tb - ta) * u - t0) / span, (vs[i] + dv * w - base) / rng])
        out["props"].append({
            "key": key, "label": label, "cubic": cubic_any,
            "vmin": round(vmin, 4), "vmax": round(vmax, 4),
            "pts": [[round(a, 5), round(b, 5)] for a, b in pts],
            "const": const,
            "keys": [[(t - t0) / span, (v - base) / rng] for t, v in zip(
                [k.get("time_offset", 0) for k in kl], vs)]})
    if not out["props"]:
        return None
    # Времена, в которых дилиб реально считает углы перспективы — та же арифметика, что в
    # persp_grid(): бюджет делится на число интервалов. Показываем их точками поверх линии,
    # чтобы «применяется ли кривая» было видно, а не приходилось верить на слово.
    n = len(times)
    split = min(GRID_CAP, max(1, (GRID_BUDGET - 1) // max(1, n - 1)))
    grid = []
    for i, t in enumerate(times):
        grid.append((t - t0) / span)
        if i + 1 < n:
            grid += [((t + (times[i + 1] - t) * q / split) - t0) / span for q in range(1, split)]
    out["grid"] = [round(g, 5) for g in grid]
    return out


def _kf_times(raw):
    """Моменты ключей в сыром common_keyframes сегмента (все свойства сведены в один набор)."""
    return sorted({p.get("time_offset", 0) for c in raw for p in (c.get("keyframe_list") or [])})


def _kf_gist(raw):
    """Суть кривых без id: (свойство, момент, значения, безье-ручки). Идентификаторы игнорируем —
    они пересоздаются на каждой перезаписи и на смысл движения не влияют."""
    return sorted((c.get("property_type", ""), p.get("time_offset", 0),
                   tuple(round(float(v), 6) for v in (p.get("values") or [])),
                   p.get("curveType", ""),
                   round(float((p.get("left_control") or {}).get("x", 0)), 6),
                   round(float((p.get("left_control") or {}).get("y", 0)), 6),
                   round(float((p.get("right_control") or {}).get("x", 0)), 6),
                   round(float((p.get("right_control") or {}).get("y", 0)), 6))
                  for c in raw for p in (c.get("keyframe_list") or []))


def _snapshot_cam_keys(proj_dir, draft, track, sc):
    """Сложить сырые ключи клипа камеры в сайдкар.

    ЗАЧЕМ: движение камеры пользователь ставит РОДНЫМИ средствами — двигает клип камеры в плеере,
    CapCut пишет ключи в сегмент драфта. В сайдкаре при этом лежит только `camKfs` с момента
    создания (обычно два тождественных ключа). Значит после удаления клипа восстанавливать было
    бы нечего: камера вернулась бы статичной. Сырой блок кладём КАК ЕСТЬ — времена ключей
    отсчитываются от начала сегмента, а восстановленный сегмент получает тот же target_timerange,
    поэтому блок переносится дословно, без пересчёта поз."""
    seg = next((s for t in draft["tracks"] if t["id"] == track["id"]
                for s in (t.get("segments") or [])), None)
    if not seg:
        return
    raw = seg.get("common_keyframes") or []
    # Сравниваем ПО СМЫСЛУ (свойство, момент, значения), а не по сырому JSON: дилиб переписывает
    # документные ключи камеры на каждом драйве и раздаёт им НОВЫЕ uuid, так что побайтовое
    # сравнение всегда расходилось и снимок писал сайдкар каждые 2 секунды. Эта запись и
    # устроила гонку, в которой привязки были потеряны — держим её по-настоящему редкой.
    if _kf_gist(raw) == _kf_gist(sc.get("camRaw") or []):
        return False
    _save_scene(proj_dir, track["tok"], {"camRaw": raw})
    return True   # кривая/ключи камеры изменились — вызвавшему стоит пересобрать риг (обновить CAMEASE)


def state(proj_dir):
    """Состояние для панели: список дорожек + камеры + привязки активной сцены."""
    draft = ct.read_draft(proj_dir)
    tr = tracks_of(draft)
    scenes = _scenes(proj_dir)
    cams = []
    for t in tr:
        if not ct._is_cam_track(t["name"]):
            continue
        sc = scenes.get(t["tok"], {})
        # Кривая движения камеры изменилась (пользователь правит её родным редактором кривых) →
        # пересобрать риг, чтобы CAMEASE (канал кривой к дилибу) обновился и искажение поехало
        # по той же кривой. Без этого канал устаревал: он писался лишь при смене привязок.
        if _snapshot_cam_keys(proj_dir, draft, t, sc) and (sc.get("tracks") or {}):
            try:
                write_rig(proj_dir, t["tok"])
            except Exception:  # noqa: BLE001 — опрос не должен падать из-за рига
                pass
        cams.append({"tok": t["tok"], "track": t["id"], "label": t["name"],
                     "keepin": bool(sc.get("keepin")),   # «не выходить за рамку» — состояние галочки
                     "binds": {k: float(v) for k, v in (sc.get("tracks") or {}).items()},
                     "tilts": sc.get("tilts") or {},   # {trackId: [вокруг горизонтали, вокруг вертикали]}
                     "pan": sc.get("pan") or {"gain": 0.0},   # сила поворота камеры (градусов на край панорамы)
                     "roll": float(sc.get("roll") or 0.0),    # завал горизонта сцены (градусы)
                     "seg": sc.get("camId") or "",             # клип камеры — по нему инспектор узнаёт «выделили камеру»
                     "pivot": float((sc.get("pan") or {}).get("pivot", 0.5)),
                     # готовность считаем ТОЛЬКО при включённом DoF: она перечитывает весь драфт,
                     # а панель опрашивает состояние каждые 2 секунды
                     "dofReady": dof_ready(proj_dir) if (sc.get("dof") or {}).get("on") else None,
                     "dof": sc.get("dof") or {"on": False, "focus": 1.96, "aperture": 0.3},
                     "keys": sorted(sc.get("camKfs") or [], key=lambda k: k["t"]),
                     "curve": _cam_curve(sc.get("camRaw") or []),   # график кривой камеры для панели
                     "start": t["start"], "end": t["end"]})
    # Сцена есть в сайдкаре, а слоя-камеры в драфте нет ⇒ клип камеры удалили (Backspace,
    # откат, копирование проекта). Раньше это было МОЛЧАНИЕ: параллакс просто переставал
    # работать без единого сообщения. Теперь панель об этом говорит и предлагает восстановить.
    scenes = _scenes(proj_dir)   # перечитываем: снимок ключей мог только что дописать сайдкар
    lost = [{"tok": tok, "binds": len(sc.get("tracks") or {}),
             "keys": len(_kf_times(sc.get("camRaw") or []))}
            for tok, sc in scenes.items() if tok not in {c["tok"] for c in cams}]
    return {"canvas": [draft["canvas_config"]["width"], draft["canvas_config"]["height"]],
            "tracks": [t for t in tr if not t["service"]], "cameras": cams, "lost": lost,
            "name": os.path.basename(proj_dir)}


def restore(proj_dir, tok, reopen=True, log=print):
    """Вернуть удалённый слой-камеру сцены `tok`. Привязки и путь камеры живут в сайдкаре и
    удаление клипа их не трогает — значит восстановление это ровно повторное вживление слоя
    с теми же ключами. Требует закрытого проекта: пишем в драфт (как и create)."""
    sc = _scenes(proj_dir).get(tok)
    if not sc:
        raise ValueError(f"сцены {tok} нет в проекте")
    _stale_lock(proj_dir, log)
    # require_closed, а НЕ пара «_project_open + close_to_home»: та отпускает запись, когда
    # признаков открытости не видно, а они основаны на ОТСУТСТВИИ улик и врут. Тут пишется слой
    # в драфт — нужен положительный ответ is_home(), иначе слой уйдёт в удерживаемый файл и
    # пропадёт при сейве CapCut (проверено на «3D CAMERA» 2026-08-09).
    was_open = ct._project_open(proj_dir) or ct.capcut_ui.is_running()
    ct.require_closed(proj_dir, log)
    draft = ct.read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    campath = sorted(sc.get("camKfs") or [], key=lambda k: k["t"])
    if not campath:   # путь потерян вместе со сценой — ставим тождество на длину привязанных дорожек
        objs = [t for t in tracks_of(draft) if t["id"] in (sc.get("tracks") or {}) and t["nsegs"]]
        if not objs:
            raise ValueError("не от чего отсчитать длину сцены — создай камеру заново")
        campath = [{"t": min(o["start"] for o in objs), "cx": 0.0, "cy": 0.0, "cz": 0.0},
                   {"t": max(o["end"] for o in objs), "cx": 0.0, "cy": 0.0, "cz": 0.0}]
    w0, w1 = campath[0]["t"], campath[-1]["t"]
    cam_id = ct.add_camera_layer(proj_dir, cw, ch, w0, w1, campath, tok, log=log)
    if not cam_id:
        raise ValueError("слой-камеру создать не удалось")
    _save_scene(proj_dir, tok, {"camId": cam_id})
    nk = _restore_cam_keys(proj_dir, cam_id, sc.get("camRaw") or [], log=log)
    n = write_rig(proj_dir, tok, log=log)
    if reopen and (was_open or ct.capcut_ui.is_running()):
        try:
            ct.capcut_ui.reopen(os.path.basename(proj_dir), os.path.join(proj_dir, ".locked"))
        except Exception as e:   # noqa: BLE001 — слой уже в драфте, откроется вручную
            log(f"не смог авто-открыть ({e}) — открой проект сам")
    log(f"камера {tok} восстановлена: seg={cam_id[:8]}, сегментов {n}, ключей {nk}")
    return {"tok": tok, "camId": cam_id, "layers": n, "keys": nk}


def _restamp_kfs(raw):
    """Копия блока ключей со СВЕЖИМИ id у каждой группы и каждого ключа.

    Дословный deepcopy опасен: если в снимке затесались ДВА ключа с одним id (а такое в живом
    драфте нашлось — один id стоял и на нулевом, и на последнем ключе всех трёх дорожек камеры),
    то восстановление воскрешало дубль снова и снова. Движок ищет ключ по id и берёт ПЕРВОЕ
    совпадение, поэтому клик по последнему ключу разрешался в нулевой — а на нулевом «предыдущего»
    ключа нет, и родной applyKeyframePreset разыменовывает пустой указатель (см. hook20).
    Id — это просто дескриптор, ничем снаружи он не адресуется (проверено: ровно 6 вхождений на
    файл, все — сами объекты ключей), поэтому перештамповка безопасна. Так же поступает и сам
    движок: CommonKeyframe::deep_copy(true) перевыпускает UUID.
    """
    out = copy.deepcopy(raw)
    for c in out:
        c["id"] = str(uuid.uuid4()).upper()
        for k in (c.get("keyframe_list") or []):
            k["id"] = str(uuid.uuid4()).upper()
    return out


def _restore_cam_keys(proj_dir, cam_id, raw, log=print):
    """Вернуть сохранённое движение камеры на свежесозданный сегмент. Пишем во ВСЕ копии драфта:
    root и Timelines/* — CapCut читает ту, что соответствует активному таймлайну."""
    if not raw:
        return 0
    root = os.path.join(proj_dir, "draft_info.json")
    fresh = _restamp_kfs(raw)   # один набор свежих id на все копии драфта: они обязаны совпадать
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = ct.read_draft_path(path)
        seg = next((s for t in d["tracks"] for s in (t.get("segments") or []) if s["id"] == cam_id), None)
        if not seg:
            continue
        seg["common_keyframes"] = copy.deepcopy(fresh)
        with open(path, "wb") as fp:
            fp.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    n = len(_kf_times(raw))
    log(f"движение камеры возвращено: {n} ключей")
    return n


def _stale_lock(proj_dir, log=print):
    """`.locked` переживает нештатный выход CapCut и потом врёт «проект открыт».
    Если CapCut не запущен — лок протух, снимаем его (иначе создание камеры упрётся в закрытие).

    ⚠️ Спрашиваем НЕ «запущен ли CapCut», а держит ли он ЭТОТ проект (`holds_project` — по
    дескрипторам процесса). Пока CapCut стартует, `pgrep` его ещё не видит, и одной пробы
    «не запущен» хватало, чтобы снести ЖИВОЙ замок. Дальше все проверки «проект открыт?» врали
    «закрыт» (признак-то у них — отсутствие улик), запись уходила в файл, который CapCut держит
    в памяти, и его ближайший сейв её стирал. Ровно так 2026-08-09 пропала камера в проекте
    «3D CAMERA»: слой лежал в драфте, на таймлайне его не было, а потом CapCut его затёр."""
    lock = os.path.join(proj_dir, ".locked")
    # ⚠️ `is_editing()` добавлен 2026-08-12: одного `holds_project` мало. Он ищет дескрипторы
    # ВНУТРИ папки проекта, а главный из них — сам `.locked`; сносим замок — и признак слепнет,
    # после чего все проверки «открыт?» отвечают «закрыт» и запись уходит в удерживаемый файл.
    # Ровно так снова пропала камера. Пункт меню «Вернуться на главную» доступен РОВНО при
    # открытом проекте и от наличия замка не зависит.
    if not os.path.exists(lock) or ct.holds_project(proj_dir) or ct.capcut_ui.is_editing():
        return False
    os.unlink(lock)
    log("снял протухший .locked (проект никем не удерживается)")
    return True


def _seed_object_keys(proj_dir, draft, depths, w0, w1, log=print):
    """Засеять привязанным объектам ПУСТЫШЕЧНЫЕ ключи (два ключа с их же текущим трансформом).

    ЗАЧЕМ (найдено живым тестом, замерами не ловилось): дилиб пишет параллакс в РЕНДЕР через
    клип, который перехватывает в хуке `TEClipBase::setKeyframe`. CapCut дёргает эту функцию
    только когда СИНКАЕТ ключи слоя — а у объекта без ключей синкать нечего, значит клип не
    ловится и драйв отчитывается `wrote 0/3`: документ обновляется, картинка стоит.
    Пара ключей с базовыми значениями ничего не меняет визуально, но делает свойство
    «ключуемым» ⇒ при открытии CapCut прогоняет их через хук и дилиб получает свой клип."""
    by_track = {t["id"]: t for t in draft["tracks"]}
    curves = {}
    for tid in depths:
        for s in (by_track.get(tid, {}).get("segments") or []):
            tr, sc = s["clip"]["transform"], s["clip"]["scale"]
            t0, t1 = ct.kf_time(s, w0), ct.kf_time(s, w1)
            curves[s["id"]] = ([(t0, tr["x"]), (t1, tr["x"])],
                               [(t0, tr["y"]), (t1, tr["y"])],
                               [(t0, sc["x"]), (t1, sc["x"])], None)
    if curves:
        ct.inject_curves(proj_dir, curves)
        log(f"засеяно ключей-пустышек: {len(curves)} объектов (иначе рендер не отдаёт клип)")


def create(proj_dir, reopen=True, log=print):
    """Создать камеру: слой-носитель + АВТО-ПРИВЯЗКА всех дорожек под ней с глубиной по порядку.
    Требует закрытия проекта (пишем в драфт), потом переоткрывает — дальше всё живое."""
    t0 = time.time()
    tok = uuid.uuid4().hex[:8].upper()
    _stale_lock(proj_dir, log)
    was_open = ct._project_open(proj_dir) or ct.capcut_ui.is_running()
    ct.write_progress(5, "Камера: чтение проекта…")
    # CapCut держит проект в памяти и перезаписывает файл при сохранении: пока он открыт,
    # слой-камера в драфте не выживет. Проверяем БЕЗУСЛОВНО и по положительному признаку —
    # у свежего проекта нет ни .locked, ни включённого пункта меню, и _project_open() врёт
    # «закрыт» (так камера один раз «создалась и не появилась»).
    try:
        ct.require_closed(proj_dir, log)
    except ValueError:
        ct.write_progress(100, "")
        raise
    draft = ct.read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    # Дорожки, уже привязанные к ДРУГОЙ камере, не трогаем: у дорожки может быть только один
    # владелец, иначе две камеры писали бы в одни и те же ключи и дрались за них.
    taken = {tid for t, s in _scenes(proj_dir).items() for tid in (s.get("tracks") or {})}
    objs = [t for t in tracks_of(draft)
            if not t["service"] and t["nsegs"] and t["id"] not in taken]
    if not objs:
        if not taken:
            raise ValueError("в проекте нет объектов — добавь дорожку с медиа")
        # Дорожки заняты — но КЕМ? Сцена-призрак (её слоя в проекте нет: слой удалили руками или
        # он не пережил сохранение CapCut) выглядела так же, как живая камера, и «Создать» молча
        # упиралось в занятые дорожки без единой подсказки, что делать. Называем виновника.
        seg_ids = {s["id"] for t in draft["tracks"] for s in (t.get("segments") or [])}
        ghosts = [t for t, s in _scenes(proj_dir).items()
                  if (s.get("tracks") or {}) and s.get("camId") not in seg_ids]
        if ghosts:
            raise ValueError(f"дорожки заняты камерой {', '.join(ghosts)}, но её слоя в проекте нет — "
                             "нажми «Восстановить камеру» либо удали её, и создавай заново")
        raise ValueError(f"нет свободных дорожек: все привязаны к камере "
                         f"{', '.join(t for t, s in _scenes(proj_dir).items() if s.get('tracks'))}")
    w0 = min(o["start"] for o in objs)
    w1 = max(o["end"] for o in objs)
    ct.write_progress(45, "Вживляю камеру…")
    # путь камеры = два ключа-тождества на границах сцены: поза есть, движения нет.
    # Свойство становится ключуемым → дальше CapCut сам доставляет ключи, когда юзер двигает камеру.
    campath = [{"t": int(w0), "cx": 0.0, "cy": 0.0, "cz": 0.0},
               {"t": int(w1), "cx": 0.0, "cy": 0.0, "cz": 0.0}]
    cam_id = ct.add_camera_layer(proj_dir, cw, ch, w0, w1, campath, tok, log=log)
    if not cam_id:
        raise ValueError("слой-камеру создать не удалось")
    depths = auto_depths(objs)
    _save_scene(proj_dir, tok, {"camId": cam_id, "tracks": {k: v for k, v in depths.items()},
                                "camKfs": campath, "dof": {"on": False, "focus": 1.96, "aperture": 0.3}})
    _seed_object_keys(proj_dir, draft, depths, w0, w1, log=log)
    ct.write_progress(90, "Собираю сцену…")
    n = write_rig(proj_dir, tok, log=log)
    if reopen and (was_open or ct.capcut_ui.is_running()):
        ct.write_progress(99, "Открываю проект…")
        try:
            ct.capcut_ui.reopen(os.path.basename(proj_dir), os.path.join(proj_dir, ".locked"))
        except Exception as e:   # noqa: BLE001 — камера уже в драфте, откроется вручную
            log(f"не смог авто-открыть ({e}) — открой проект сам")
    ct.write_progress(100, "Готово")
    log(f"камера {tok}: привязано дорожек {len(depths)}, сегментов {n}, {time.time() - t0:.0f}с")
    return {"tok": tok, "camId": cam_id, "tracks": depths, "layers": n}


VE_CAMPATH = os.path.expanduser("~/Movies/CapCut/User Data/ve_campath.txt")


def _pose_to_layer(k, cw, ch):
    """Поза камеры (мировые единицы) -> трансформ слоя-носителя (px/py/sx/rz).
    Ровно обратные формулы add_camera_layer: дилиб ждёт ИМЕННО эти числа (их же он читает
    из рендер-кривой слоя-камеры), поэтому вся математика ниже по течению не меняется."""
    f, hw, hh, z = camera3d.FOCAL_FRAC * ch, cw / 2.0, ch / 2.0, camera3d.Z_REF
    cz = min(k.get("cz", 0.0), z * 0.95)   # cz->Z_REF даёт деление на ноль (бесконечный зум)
    return (-k.get("cx", 0.0) * f / (z * hw), -k.get("cy", 0.0) * f / (z * hh),
            z / (z - cz), k.get("roll", 0.0))


def _sync_render(proj_dir, tok, since, draft=None, log=print):
    """ДОСЫЛ ОДНОЙ РОДНОЙ КОМАНДЫ после драйва — тот же приём, что у трекинга (см. shina-komand).

    Дилиб кладёт параллакс И в документ, И в рендер, но рендер он пушит в клип, пойманный хуком.
    Указатель протухает (переоткрытие, пересборка клипа), а два сегмента с ОДНИМ исходником вообще
    делят один слот рига — тогда часть слоёв живёт со старой картинкой. `addCommonKeyframe`
    заставляет движок перечитать документ и синкнуть рендер САМОМУ, как он это делает себе.

    Замер 2026-08-09 (стенд 0729, 7 ведомых слоёв): за окно тишины движок не синкает НИЧЕГО
    (0 своих TEClip.setKeyframe), после ОДНОЙ команды — 7 клипов × 5 свойств. Значит выстрел
    нужен ОДИН на сцену, а не на сегмент.

    ⭐ Стреляем В ТОТ ЖЕ ТИК опроса, не дожидаясь драйва. Дилиб за ОДНУ итерацию своего цикла
    (300 мс) читает и `ve_campath`/`ve_rig`, и `ve_kfreq`, а обе работы ставит в очередь ГЛАВНОГО
    потока — та серийная, значит порядок «сначала драйв, потом команда» сохраняется сам.
    Ожидание драйва стоило ЦЕЛЫЙ лишний период опроса: 315 мс из 505 (замер 2026-08-09), потому
    что мой выстрел всегда приходил сразу ПОСЛЕ того, как цикл проверил гейт ключей."""
    if not ct._project_open(proj_dir):
        return False   # проект закрыт (создание/удаление камеры) — синкать нечего
    draft = draft if draft is not None else ct.read_draft(proj_dir)
    sc = _scenes(proj_dir).get(tok, {})
    by_track = {t["id"]: t for t in draft["tracks"]}
    # Облёт/наклон заменяют позицию УГЛАМИ (дилиб сносит треки позиции целиком). Стрелять надо по
    # свойству, которое реально записано, иначе команда создаст одинокий ключ на пустом треке.
    cp = float((sc.get("pan") or {}).get("gain") or 0) > 0 or bool(sc.get("tilts"))
    prop = ct.CORNER_PROPS[0] if cp else "KFTypePositionX"
    kfs = sorted(int(k["t"]) for k in (sc.get("camKfs") or []))
    for tid in _binds(proj_dir, tok):
        for s in (by_track.get(tid, {}).get("segments") or []):
            st, dur = s["target_timerange"]["start"], s["target_timerange"]["duration"]
            # целимся В СУЩЕСТВУЮЩИЙ ключ (время ключа камеры внутри окна сегмента): движок
            # снимает значение с трека, и снимок садится на наш ключ, лишнего не создавая
            t_tl = next((t for t in kfs if st <= t <= st + dur), None)
            if t_tl is not None:
                return _shoot(s, prop, ct.kf_time(s, t_tl), since, log=log)
    return False


def _drive_first(since):
    """Успел ли драйв ПЕРЕД командой. Лог только дописывается, поэтому позиция в нём = время."""
    try:
        with open(ct.VE_LOG_PATH, errors="ignore") as f:
            f.seek(since)
            w = f.read()
    except OSError:
        return True   # лога нет — проверять нечем, вести себя как раньше
    d, m = w.find("DRIVE scenes="), w.find("MKREQ отправлена api=addCommonKeyframe")
    return d >= 0 and (m < 0 or d < m)


def _shoot(seg, prop, t_src, since, log=print):
    """Выстрел + страховка от гонки на границе тика. Обычно дилиб успевает и драйв, и команду за
    одну итерацию, но если наш гейт ключей он прочитал, а гейт позы ещё нет, команда снимет
    СТАРОЕ значение — тогда картинка отстанет на один щелчок ползунка и на последнем щелчке
    (когда следующего не будет) останется мимо. Ловим по логу и досылаем ещё один выстрел."""
    at = ct._log_size()
    ok = ct.live_sync(seg, prop, t_src, log=log)
    if ok and not _drive_first(since):
        if ct._log_wait("DRIVE scenes=", at, timeout=1.5):
            log("живой синк: драйв пришёл после команды — досылаю")
            ok = ct.live_sync(seg, prop, t_src, log=log)
    return ok


_SYNC_LOCK = threading.Lock()
_SYNC_NEXT = [None]   # since последнего запроса, пришедшего пока выстрел в работе


def _sync_soon(proj_dir, tok, since, log=print):
    """Тот же синк, но НЕ в запросе: ползунок глубины шлёт `bind` на каждое движение
    (`onValueChanged` в панели), а выстрел стоит ~0.5 с — ждать его значило бы превратить
    ползунок в кисель (замер: 0.00 с против 0.56 с на тик).

    Очередь не копим: пока один выстрел в работе, запоминаем только ПОСЛЕДНИЙ запрос. Команда
    значения не несёт и снимает то, что лежит в треке на момент выстрела, поэтому промежуточные
    положения ползунка синкать бессмысленно — важен последний."""
    _SYNC_NEXT[0] = since
    if not _SYNC_LOCK.acquire(blocking=False):
        return   # выстрел уже идёт — он подхватит наш since в конце

    def work():
        try:
            while _SYNC_NEXT[0] is not None:
                s, _SYNC_NEXT[0] = _SYNC_NEXT[0], None
                _sync_render(proj_dir, tok, s, log=log)
        finally:
            _SYNC_LOCK.release()
        if _SYNC_NEXT[0] is not None:   # запрос успел прийти между проверкой и release
            _sync_soon(proj_dir, tok, _SYNC_NEXT[0], log=log)

    threading.Thread(target=work, daemon=True).start()


def live_path(proj_dir, tok, campath, log=print):
    """ЖИВОЙ путь камеры: пишем ve_campath.txt, дилиб подхватывает по mtime и сразу гонит
    параллакс — проект НЕ закрывается. Он же персистит ключи слоя-камеры в документ.
    Времена в файле — в SOURCE-времени слоя-камеры (= timeline - начало камеры)."""
    sc = _scenes(proj_dir).get(tok, {})
    if not sc.get("camId"):
        raise ValueError(f"камера {tok} не найдена")
    draft = ct.read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    cam_seg = next((s for t in draft["tracks"] for s in t.get("segments", [])
                    if s["id"] == sc["camId"]), None)
    w0 = cam_seg["target_timerange"]["start"] if cam_seg else 0
    png = ct.cam_png_path(cw, ch, tok)
    lines = [f"CAM {png}"]
    for k in sorted(campath, key=lambda k: k["t"]):
        px, py, sx, rz = _pose_to_layer(k, cw, ch)
        lines.append(f"K {int(k['t']) - int(w0)} {px:.6f} {py:.6f} {sx:.6f} {rz:.6f}")
    since = ct._log_size()
    with open(VE_CAMPATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _save_scene(proj_dir, tok, {"camKfs": campath})
    log(f"живой путь камеры: {len(campath)} ключей")
    synced = _sync_render(proj_dir, tok, since, draft=draft, log=log)
    return {"keys": len(campath), "campath": campath, "synced": bool(synced)}


def preset(proj_dir, tok, name, intensity=0.6, log=print):
    """Готовое движение камеры одним нажатием: строим путь пресетом и применяем ЖИВЬЁМ.
    Окно = длительность слоя-камеры. Это главный сценарий «нажал и красиво»."""
    sc = _scenes(proj_dir).get(tok, {})
    draft = ct.read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    cam = next((s for t in draft["tracks"] for s in t.get("segments", [])
                if s["id"] == sc.get("camId")), None)
    if not cam:
        raise ValueError(f"камера {tok} не найдена")
    w0 = cam["target_timerange"]["start"]
    w1 = w0 + cam["target_timerange"]["duration"]
    if name == "reset":
        return live_path(proj_dir, tok, [{"t": w0, "cx": 0.0, "cy": 0.0, "cz": 0.0, "roll": 0.0},
                                         {"t": w1, "cx": 0.0, "cy": 0.0, "cz": 0.0, "roll": 0.0}], log=log)
    f, hw = camera3d.FOCAL_FRAC * ch, cw / 2.0
    n = 2 if name in ("push_in", "pull_out", "parallax_pan", "parallax_tilt") else 9   # облёт/дрейф нелинейны
    path = []
    for i in range(n + 1):
        s = i / n
        cx, cy, cz = camera3d.camera_at(name, s, intensity, f, hw)
        path.append({"t": int(w0 + (w1 - w0) * s), "cx": cx, "cy": cy, "cz": cz, "roll": 0.0})
    log(f"пресет «{name}» сила {intensity:.2f}")
    return live_path(proj_dir, tok, path, log=log)


def set_key(proj_dir, tok, t_us, pose, log=print):
    """Поставить/обновить ключ камеры в момент t (ручной режим, как ромб в AE)."""
    sc = _scenes(proj_dir).get(tok, {})
    path = [dict(k) for k in (sc.get("camKfs") or []) if abs(int(k["t"]) - int(t_us)) > 16000]
    path.append({"t": int(t_us), **{k: float(pose.get(k, 0.0)) for k in ("cx", "cy", "cz", "roll")}})
    return live_path(proj_dir, tok, path, log=log)


def del_key(proj_dir, tok, t_us, log=print):
    sc = _scenes(proj_dir).get(tok, {})
    path = [k for k in (sc.get("camKfs") or []) if abs(int(k["t"]) - int(t_us)) > 16000]
    if not path:   # последний ключ убирать нельзя — камера должна иметь позу
        path = [{"t": int(t_us), "cx": 0.0, "cy": 0.0, "cz": 0.0, "roll": 0.0}]
    return live_path(proj_dir, tok, path, log=log)


def set_path(proj_dir, tok, campath, log=print):
    """Записать путь камеры (ключи позы) в слой-камеру. campath=[{t,cx,cy,cz}] в timeline-µs,
    единицы мировые (как в camera3d). Пишем root И Timelines/* — иначе CapCut перечитает старую копию.
    Проект должен быть ЗАКРЫТ (иначе CapCut перезапишет файл своей копией из памяти)."""
    cam_id = _scenes(proj_dir).get(tok, {}).get("camId")
    if not cam_id:
        raise ValueError(f"камера {tok} не найдена")
    d0 = ct.read_draft(proj_dir)
    cw, ch = d0["canvas_config"]["width"], d0["canvas_config"]["height"]
    f, hw, hh = camera3d.FOCAL_FRAC * ch, cw / 2.0, ch / 2.0
    z = camera3d.Z_REF
    for path in [os.path.join(proj_dir, "draft_info.json")] + \
            glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = ct.read_draft_path(path)
        seg = next((s for t in d["tracks"] for s in t.get("segments", []) if s["id"] == cam_id), None)
        if not seg:
            continue
        # обратные формулы add_camera_layer: поза камеры -> трансформ слоя-носителя
        ct.set_curve(seg, "KFTypePositionX", [(ct.kf_time(seg, k["t"]), -k["cx"] * f / (z * hw)) for k in campath])
        ct.set_curve(seg, "KFTypePositionY", [(ct.kf_time(seg, k["t"]), -k["cy"] * f / (z * hh)) for k in campath])
        ct.set_curve(seg, "KFTypeScaleX", [(ct.kf_time(seg, k["t"]), z / (z - k["cz"])) for k in campath])
        with open(path, "wb") as fp:
            fp.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    _save_scene(proj_dir, tok, {"camKfs": campath})
    log(f"путь камеры {tok}: {len(campath)} ключей")
    return {"keys": len(campath)}


def bind(proj_dir, tok, track_id=None, depth=None, seg_id=None, log=print):
    """Привязать дорожку (depth 0..1) или ОТВЯЗАТЬ (depth=None). Живьём: проект не трогаем,
    только переписываем риг — дилиб подхватит по mtime и на следующем драйве применит.
    seg_id — «нитка»: пришёл клик по клипу в родном таймлайне, дорожку резолвим сами."""
    if not track_id and seg_id:
        track_id = track_of_seg(ct.read_draft(proj_dir), seg_id)
        if not track_id:
            raise ValueError("клип не найден в проекте — переоткрой проект в туле")
    if not track_id:
        raise ValueError("не указана дорожка")
    data = ct._scene_load_side(proj_dir)
    sc = data.setdefault("scenes", {}).setdefault(tok, {})
    tracks = sc.setdefault("tracks", {})
    if depth is None:
        tracks.pop(track_id, None)
    else:
        # у дорожки один владелец: молча увести её у другой камеры значило бы, что обе пишут
        # в одни ключи. Лучше честно отказать — пользователь сам решит, кому она нужна.
        other = next((t for t, s in (data.get("scenes") or {}).items()
                      if t != tok and track_id in (s.get("tracks") or {})), None)
        if other:
            raise ValueError(f"дорожка уже привязана к другой камере ({other}) — отвяжи там сначала")
        tracks[track_id] = max(0.0, min(1.0, float(depth)))
    ct._scene_write_side(proj_dir, data)
    n = write_rig(proj_dir, tok, log=log)
    return {"tracks": {k: float(v) for k, v in tracks.items()}, "layers": n}


def dof_ready(proj_dir):
    """Готова ли сцена к глубине резкости и есть ли чем анимировать фокус.

    Дилиб модулирует блюр ТОЛЬКО если у объектов есть эффект «Размытие» внутри сборного клипа
    (карта объект↔эффект) и в проекте лежит слой-контроллер «DoF» (его масштаб X = сила,
    Y = фокус — именно на нём пользователь ставит ключи и кривые). Без этого галочка «Включить»
    писала ve_dof.txt и МОЛЧА ничего не делала."""
    import dof as dofmod
    draft = ct.read_draft(proj_dir)
    ctl = any((t.get("name") or "").startswith("DoF") for t in draft.get("tracks") or [])
    try:
        pairs = len(dofmod.write_dof_map(proj_dir, out_path=os.devnull))
    except Exception:   # noqa: BLE001 — читаем чужую структуру; «не смог разобрать» = не готово
        pairs = 0
    # СЛОЙ СНОВА ОБЯЗАТЕЛЕН (решение Назара 2026-08-09, возврат к слою-контроллеру).
    # Промежуточная версия убрала его из условия — и это молча ломало всю цепочку: готовность
    # выходила True без слоя, кнопка «Подготовить сцену» не показывалась, а больше слой создать
    # НЕКОМУ. Снаружи выглядело как «включил галочку, ничего не происходит».
    return {"ctl": ctl, "pairs": pairs, "ready": bool(pairs and ctl)}


def prep_dof(proj_dir, tok, focus=1.96, aperture=0.3, reopen=True, log=print):
    """Собрать всё, без чего глубина резкости не работает: завернуть привязанные объекты в
    сборные клипы с эффектом «Размытие», вживить слой-контроллер фокуса, сгенерить карты.

    ЭТО МЕНЯЕТ СТРУКТУРУ ТАЙМЛАЙНА (объекты становятся сборными клипами) — поэтому отдельная
    кнопка, а не побочный эффект галочки. Дилиб умеет крутить только уже существующий эффект
    размытия внутри компаунда: своего рендера у плагина нет.
    Фокус после этого анимируется РОДНЫМИ ключами на клипе «DoF контроллер» (масштаб Y)."""
    import dof as dofmod
    sc = _scenes(proj_dir).get(tok)
    if not sc:
        raise ValueError(f"сцены {tok} нет в проекте")
    _stale_lock(proj_dir, log)
    was_open = ct._project_open(proj_dir) or ct.capcut_ui.is_running()
    ct.write_progress(10, "Глубина резкости: закрываю проект…")
    # Та же дыра, что была в create(): _project_open() основан на ОТСУТСТВИИ признаков и врёт
    # «закрыт» на свежем проекте, после чего запись уходит в файл, удерживаемый CapCut'ом,
    # и теряется при его сохранении. Требуем положительного доказательства.
    try:
        ct.require_closed(proj_dir, log)
    except ValueError:
        ct.write_progress(100, "")
        raise
    draft = ct.read_draft(proj_dir)
    binds = _binds(proj_dir, tok)
    seg_ids = [s["id"] for t in draft["tracks"] if t["id"] in binds for s in (t.get("segments") or [])]
    if not seg_ids:
        ct.write_progress(100, "")
        raise ValueError("к камере не привязано ни одной дорожки — размывать нечего")
    ct.write_progress(40, "Глубина резкости: сборные клипы…")
    info = dofmod.wrap_and_enable(proj_dir, seg_ids, aperture=aperture, focus=focus, log=log)
    _save_scene(proj_dir, tok, {"dof": {"on": True, "focus": float(focus), "aperture": float(aperture)}})
    ct.write_progress(99, "Глубина резкости: открываю проект…")
    if reopen and (was_open or ct.capcut_ui.is_running()):
        try:
            ct.capcut_ui.reopen(os.path.basename(proj_dir), os.path.join(proj_dir, ".locked"))
        except Exception as e:   # noqa: BLE001 — компаунды уже записаны, реопен вторичен
            log(f"не смог авто-открыть ({e}) — открой проект сам")
    if info.get("bake_eids"):   # CapCut перегенерит id блюр-эффектов при открытии → фоновый реген карты
        dofmod.refresh_dof_map_async(proj_dir, info["bake_eids"], log=log)
    ct.write_progress(100, "Готово")
    log(f"глубина резкости готова: завёрнуто {info['wrapped']}, пар {info['pairs']}")
    return info


def set_dof(proj_dir, tok, on=None, focus=None, aperture=None, log=print):
    """Глубина резкости сцены. Тумблер живой: ve_dof.txt читается дилибом каждый драйв."""
    sc = _scenes(proj_dir).get(tok, {})
    d = dict(sc.get("dof") or {"on": False, "focus": 1.96, "aperture": 0.3})
    if on is not None:
        d["on"] = bool(on)
    if focus is not None:
        d["focus"] = float(focus)
    if aperture is not None:
        d["aperture"] = float(aperture)
    _save_scene(proj_dir, tok, {"dof": d})
    p = os.path.expanduser("~/Movies/CapCut/User Data/ve_dof.txt")
    if d["on"]:
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"{d['focus']} {d['aperture']}\n")
    elif os.path.exists(p):
        os.unlink(p)
    # Дилиб перечитывает ve_dof.txt только НА ДРАЙВЕ, а драйв запускает mtime ve_campath/ve_rig.
    # Сам по себе тумблер ничего не запускал: замер 2026-08-09 на стенде «3D CAMERA» (проект
    # открыт, DoF вкл) — после set_dof в логе НОЛЬ строк DRIVE и ноль реакций движка, то есть
    # сила и фокус не доходили до картинки, пока пользователь не подвинет камеру.
    # write_rig — та же воронка, что у ползунка глубины: перезапускает драйв И досылает команду
    # синка рендера (фоном, очередь не копится).
    if ct._project_open(proj_dir):
        write_rig(proj_dir, tok, log=log)
    log(f"DoF {'вкл' if d['on'] else 'выкл'} (фокус {d['focus']}, сила {d['aperture']})")
    return dict(d, **dof_ready(proj_dir)) if d["on"] else d


def delete(proj_dir, tok, reopen=True, log=print):
    """Удалить камеру: снять ключи со всех ведомых объектов, убрать трек камеры из драфта.
    Ключи объектов уходят вместе с камерой (решение автора)."""
    _stale_lock(proj_dir, log)
    was_open = ct._project_open(proj_dir) or ct.capcut_ui.is_running()
    driven = list(_scenes(proj_dir).get(tok, {}).get("driven") or [])
    # 1) снять привязки → write_rig выдаст CLEAR на всё, что вело; дилиб снимет ключи ЖИВЬЁМ
    data = ct._scene_load_side(proj_dir)
    if tok in (data.get("scenes") or {}):
        data["scenes"][tok]["tracks"] = {}
        ct._scene_write_side(proj_dir, data)
    write_rig(proj_dir, tok, log=log)
    if driven:
        time.sleep(1.2)   # дать дилибу один цикл опроса (300мс) на снятие ключей до закрытия
    # 2) убрать трек камеры из драфта — тут ЗАПИСЬ, значит нужен положительный ответ is_home():
    # close_to_home пропускает работу по отсутствию улик, и удаление уходило бы в файл, который
    # CapCut держит, — при его сейве трек камеры воскресал бы (зеркало потери на «3D CAMERA»)
    ct.require_closed(proj_dir, log)
    cam_name = f"Камера 3D · {tok}"
    root = os.path.join(proj_dir, "draft_info.json")
    # root И Timelines/*: компаунды/треки живут в обоих, иначе CapCut перечитает старую копию
    for path in [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json")):
        d = ct.read_draft_path(path)
        keep = [t for t in d["tracks"] if t.get("name") != cam_name]
        if len(keep) != len(d["tracks"]):
            d["tracks"] = keep
            shutil.copy2(path, path + ".predel.bak")
            with open(path, "wb") as fp:
                fp.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    # 3) вычистить сцену из сайдкара и блок из рига
    data = ct._scene_load_side(proj_dir)
    (data.get("scenes") or {}).pop(tok, None)
    ct._scene_write_side(proj_dir, data)
    cfg = ct.read_draft(proj_dir)["canvas_config"]
    png = ct.cam_png_path(cfg["width"], cfg["height"], tok)
    header, dof, blocks = ct._rig_parse()
    ct._rig_write(header, dof, [b for b in blocks if b[0] != png])
    if reopen and (was_open or ct.capcut_ui.is_running()):
        try:
            ct.capcut_ui.reopen(os.path.basename(proj_dir), os.path.join(proj_dir, ".locked"))
        except Exception as e:   # noqa: BLE001
            log(f"не смог авто-открыть ({e})")
    log(f"камера {tok} удалена (снято ключей с {len(driven)} объектов)")
    return {"deleted": tok, "cleared": len(driven)}


def demo():
    """Самопроверка чистой логики (без CapCut): авто-глубина и разворот привязок."""
    objs = [{"id": "low"}, {"id": "mid"}, {"id": "top"}]   # снизу вверх
    d = auto_depths(objs)
    assert d["top"] == 0.0 and d["low"] == 1.0, d          # верх = перед, низ = даль
    assert abs(d["mid"] - 0.5) < 1e-9, d
    assert auto_depths([{"id": "one"}]) == {"one": 0.5}
    z_near, z_far = camera3d.depth_to_z(0.0), camera3d.depth_to_z(1.0)
    assert z_near < z_far, (z_near, z_far)                 # ближе к камере = меньше Z
    assert _is_service("Камера 3D · A1B2") and not _is_service("IMG_7295.MOV")
    print("camera.demo: ок — верх=перед, низ=даль, служебные треки отфильтрованы")


if __name__ == "__main__":
    demo()


def set_pivot(proj_dir, tok, depth, log=print):
    """Глубина оси, вокруг которой камера облетает сцену (0..1, как у дорожек).
    Слои на этой глубине остаются на месте, ближние и дальние расходятся в разные стороны."""
    sc = _scenes(proj_dir).get(tok, {})
    pan = dict(sc.get("pan") or {})
    pan["pivot"] = max(0.0, min(1.0, float(depth)))
    _save_scene(proj_dir, tok, {"pan": pan})
    write_rig(proj_dir, tok, log=log)
    return {"pan": pan}


def set_pan(proj_dir, tok, gain, log=print):
    """ПОВОРОТ КАМЕРЫ (эмуляция 3D). `gain` — на сколько градусов камера разворачивается, когда
    панорама доведена до края кадра. 0 = как раньше, камера просто едет вбок.

    Панорама при этом становится ОБЛЁТОМ вокруг точки в глубине сцены: слои на её глубине стоят,
    ближние и дальние расходятся в разные стороны, и каждый ловит свою перспективу. Чистый
    разворот «на штативе» объёма бы не дал — все слои растянуты на кадр и видны под одним углом."""
    pan = dict(_scenes(proj_dir).get(tok, {}).get("pan") or {})
    pan["gain"] = float(gain)
    _save_scene(proj_dir, tok, {"pan": pan})
    write_rig(proj_dir, tok, log=log)
    return {"pan": pan}


def set_roll(proj_dir, tok, deg, log=print):
    """РОЛЛ СЦЕНЫ — завал горизонта, градусы. Живёт в риге рядом с панорамой и осью облёта.

    Почему не через поворот клипа камеры, хотя внешне это то же самое: драйв читает ролл ТОЛЬКО
    из кривой ключей `rz`, а статический поворот клипа игнорирует. Поставить ключ из панели не
    вышло — их `onClickSetKeyFrame` на нашем keyframeData молчит (замер: 237 вызовов, 0 ключей).
    Через риг канал прямой и такой же, как у pan/pivot/tilt. Кейфреймленный ролл при этом жив:
    дилиб складывает кривую `rz` с этим значением, так что одно другому не мешает."""
    d = float(deg)
    _save_scene(proj_dir, tok, {"roll": d})
    write_rig(proj_dir, tok, log=log)
    return {"roll": d}


def set_tilt(proj_dir, tok, track_id, tiltx, tilty, log=print):
    """Наклон плоскости дорожки в 3D (градусы вокруг горизонтали и вертикали).
    Живой, как и глубина: правим сайдкар → пересобираем риг → дилиб перечитывает по mtime."""
    sc = _scenes(proj_dir).get(tok, {})
    tilts = dict(sc.get("tilts") or {})
    tx, ty = float(tiltx), float(tilty)
    if tx or ty:
        tilts[track_id] = [tx, ty]
    else:
        tilts.pop(track_id, None)   # ноль = наклона нет, не держим мусор в сайдкаре
    _save_scene(proj_dir, tok, {"tilts": tilts})
    write_rig(proj_dir, tok, log=log)
    return {"tilts": tilts}


def set_keepin(proj_dir, tok, on):
    """Галочка «не выходить за рамку»: флаг живёт в шапке ve_rig.txt, дилиб читает по mtime.
    Пересобираем риг сцены — заодно применится сразу, без переоткрытия проекта."""
    _save_scene(proj_dir, tok, {"keepin": bool(on)})
    ct._rig_set_keepin(bool(on))
    return {"ok": True, "keepin": bool(on)}
