#!/usr/bin/env python3
# dof.py — ДИНАМИЧЕСКАЯ глубина резкости: в компаунд объекта кладём блюр-эффект,
# ключи блюра печём по траектории камеры: d(t)=z-cz(t), blur(t)=aperture*|d-focus|.
# Наезд камеры -> cz растёт -> расстояния до фокуса меняются -> фокус перекатывается.
#
# ponytail: авто-заворот в компаунд и РЕАЛТАЙМ-драйв из дилиба (эффект-ключи KeyframeEffect) —
# следующие рунги. Пока инъекция в существующие компаунды + бейк кривой по ключам зума камеры;
# на воспроизведении блюр и параллакс синхронны (оба кейфреймлены).
import glob, json, uuid, os, sys


def authoritative_draft(proj_dir):
    """Путь к копии драфта, которую РЕАЛЬНО грузит движок.

    CapCut держит проект в двух местах: корневой draft_info.json и Timelines/<id>/draft_info.json,
    где <id> = main_timeline_id из Timelines/project.json. Загружает он ВТОРУЮ. Пока обе копии
    идентичны, разницы нет — но стоит их развести, и чтение корневой даёт id, которых в работающей
    модели не существует. Именно так глубина резкости молча не работала: ve_dof_map.txt строился
    из корневой копии и не совпадал с движком НИ ОДНИМ id («DOF resolve: слоёв=0 из 3 пар»).
    Разведение копий починено в compound.make_ids, но читать всё равно обязаны авторитетную:
    id эффектов может переписать и сам CapCut.
    """
    tl = os.path.join(proj_dir, "Timelines")
    if os.path.isdir(tl):
        cands = []
        try:
            main = json.load(open(os.path.join(tl, "project.json"), encoding="utf-8")).get("main_timeline_id")
            if main:
                cands.append(os.path.join(tl, main, "draft_info.json"))
        except (OSError, ValueError):
            pass   # нет project.json / битый — падём на перебор ниже
        cands += sorted(glob.glob(os.path.join(tl, "*", "draft_info.json")))
        for c in cands:
            if os.path.exists(c):
                return c
    return os.path.join(proj_dir, "draft_info.json")

# «Размытие» — стабильный ресурс на этой машине (из ручного эффекта юзера)
EFFECT_ID = "7399464929830423813"
EFFECT_PATH = os.path.expanduser(
    "~/Library/Containers/com.lemon.lvoverseas/Data/Movies/CapCut/User Data/Cache/effect/"
    "7399464929830423813/2db7bf49d9349e308ef0f46c39b14abf")
DOF_MARK = "ve_dof"   # метка наших сегментов -> идемпотентность (сносим старое перед вставкой)
RIG_ZREF = 2.0        # нейтральная глубина, если рига нет (как в дилибе)


def uid():
    return str(uuid.uuid4()).upper()


def blur_for_dist(d, focus, aperture, clamp=1.0):
    """Блюр от расстояния объекта до камеры d: чем дальше от фокусного F, тем сильнее. 0..clamp."""
    return max(0.0, min(clamp, aperture * abs(d - focus)))


def blur_for_depth(z, focus, aperture, clamp=1.0):
    """Статический случай (камера без зума, cz=0 -> d=z)."""
    return blur_for_dist(z, focus, aperture, clamp)


def cz_of(sx, z_min):
    """Позиция наезда камеры по зуму — та же формула, что в дилибе (drive_layers)."""
    return z_min * (1.0 - 1.0 / max(0.05, sx))


def blur_curve(z, cam_sx, z_min, focus, aperture, clip_dur):
    """Динамика: блюр-ключи объекта z по траектории зума камеры cam_sx=[(t_us,sx)].
    d(t)=z-cz(t); blur(t)=aperture*|d-focus|. Времена = ключи камеры (в пределах клипа)."""
    out = []
    for t, sx in cam_sx:
        if t > clip_dur:
            continue
        out.append((t, blur_for_dist(z - cz_of(sx, z_min), focus, aperture)))
    return out or None


def _kf(t_us, v, kid=None):
    return {"id": kid or uid(), "curveType": "Line", "time_offset": int(t_us),
            "left_control": {"x": 0.0, "y": 0.0}, "right_control": {"x": 0.0, "y": 0.0},
            "values": [float(v)], "string_value": "", "graphID": ""}


def _blur_material(value, mid=None):
    """mid — id из общего бандла заворота (compound.make_ids). Без него материал получал бы
    РАЗНЫЙ id в root и Timelines: копии драфта расходились, см. комментарий в make_ids."""
    return {"id": mid or uid(), "effect_id": EFFECT_ID, "resource_id": EFFECT_ID, "name": "Размытие",
            "type": "video_effect", "sub_type": 0, "bind_segment_id": "", "transparent_params": "",
            "path": EFFECT_PATH, "value": 1.0, "category_id": "1111", "category_name": "Эффекты видео",
            "platform": "all", "apply_target_type": 2, "source_platform": 1, "version": "",
            "item_effect_type": 0,
            "adjust_params": [{"name": "effects_adjust_blur", "value": float(value), "default_value": 0.5}],
            "time_range": None, "formula_id": "", "apply_time_range": None, "render_index": 0,
            "track_render_index": 0, "common_keyframes": [], "request_id": "", "algorithm_artifact_path": "",
            "disable_effect_faces": [], "covering_relation_change": 0, "enable_mask": True, "effect_mask": [],
            "enable_video_mask_stroke": True, "enable_video_mask_shadow": True,
            "aigc_current_artifact_path": "", "aigc_current_artifact_cnt": 0}


def _blur_segment(mat_id, duration, keyframes, ids=None):
    """ids — бандл из compound.make_ids(). Именно этот id уходит в ve_dof_map.txt и по нему дилиб
    находит эффект в живой модели, поэтому он ОБЯЗАН совпадать в обеих копиях драфта."""
    sub = ids.get("sub") if ids else None
    seg = {"id": (ids or {}).get("blur_seg") or uid(), "source_timerange": None,
           "target_timerange": {"start": 0, "duration": int(duration)},
           "render_timerange": {"start": 0, "duration": 0}, "desc": DOF_MARK, "state": 0, "speed": 1.0,
           "is_loop": False, "is_tone_modify": False, "reverse": False, "intensifies_audio": False,
           "cartoon": False, "volume": 1.0, "last_nonzero_volume": 1.0, "clip": None, "uniform_scale": None,
           "material_id": mat_id, "extra_material_refs": [], "render_index": 11000, "keyframe_refs": [],
           "enable_lut": False, "enable_adjust": False, "enable_hsl": False, "visible": True, "group_id": "",
           "enable_color_curves": True, "enable_hsl_curves": True, "track_render_index": 1, "hdr_settings": None,
           "enable_color_wheels": True, "track_attribute": 0, "is_placeholder": False, "template_id": "",
           "enable_smart_color_adjust": False, "template_scene": "default", "common_keyframes": [],
           "caption_info": None,
           "responsive_layout": {"enable": False, "target_follow": "", "size_layout": 0,
                                 "horizontal_pos_layout": 0, "vertical_pos_layout": 0},
           "enable_color_match_adjust": False, "enable_color_correct_adjust": False, "enable_adjust_mask": False,
           "raw_segment_id": "", "lyric_keyframes": None, "enable_video_mask": True,
           "digital_human_template_group_id": "", "color_correct_alg_result": "", "source": "segmentsourcenormal",
           "enable_mask_stroke": False, "enable_mask_shadow": False, "enable_color_adjust_pro": False}
    if keyframes:
        seg["common_keyframes"] = [{"id": sub("blur_kf") if sub else uid(),
                                    "material_id": "", "property_type": "effects_adjust_blur",
                                    "keyframe_list": [_kf(t, v, sub("kf", i) if sub else None)
                                                      for i, (t, v) in enumerate(keyframes)]}]
    return seg


def add_blur_to_compound(inline_draft, blur, duration, keyframes=None):
    """Кладёт (или заменяет наш) блюр-эффект в inline-draft компаунда.
    blur — константа 0..1; keyframes=[(t_us,val),...] переопределяет её анимацией."""
    mats = inline_draft.setdefault("materials", {})
    ves = mats.setdefault("video_effects", [])
    tracks = inline_draft.setdefault("tracks", [])
    # DoF владеет блюром: сносим наши (desc) И любой блюр-эффект (по effect_id), чужие эффекты не трогаем
    blur_ids = {m["id"] for m in ves if m.get("effect_id") == EFFECT_ID}
    old_mat_ids = set()
    for tr in tracks:
        if tr.get("type") != "effect":
            continue
        keep = []
        for s in tr.get("segments", []):
            if s.get("desc") == DOF_MARK or s.get("material_id") in blur_ids:
                old_mat_ids.add(s.get("material_id"))
            else:
                keep.append(s)
        tr["segments"] = keep
    if old_mat_ids:
        mats["video_effects"] = [m for m in ves if m.get("id") not in old_mat_ids]
        ves = mats["video_effects"]
    tracks[:] = [t for t in tracks if not (t.get("type") == "effect" and not t.get("segments"))]
    # вставить свежий
    mat = _blur_material(keyframes[0][1] if keyframes else blur)
    ves.append(mat)
    seg = _blur_segment(mat["id"], duration, keyframes)
    tracks.append({"id": uid(), "type": "effect", "flag": 0, "attribute": 0, "name": "",
                   "is_default_name": True, "segments": [seg]})
    return mat["id"]


def parse_rig(path):
    """ve_rig.txt -> {abs_src_path: z}. LAYER <id> <z> <bx by bs br ts ss sp> <path>."""
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        if not line.startswith("LAYER "):
            continue
        toks = line.split(None, 10)  # 0=LAYER,1=id,2..10=9 чисел? -> путь в toks[10]
        if len(toks) < 11:
            continue
        try:
            z = float(toks[2])
        except ValueError:
            continue
        out[toks[10].strip()] = z
    return out


def rig_camera_path(path):
    """Путь png слоя-камеры из ve_rig.txt (строка CAMERA <id> <n> <path>)."""
    if not os.path.exists(path):
        return None
    for line in open(path, encoding="utf-8"):
        if line.startswith("CAMERA "):
            toks = line.split(None, 3)
            if len(toks) >= 4:
                return toks[3].strip()
    return None


def read_camera_sx(draft, cam_path):
    """Ключи масштаба слоя-камеры из главного таймлайна -> [(t_us, sx)] по времени.
    Пусто -> камера без зума (cz=0, блюр статичный)."""
    mats = draft.get("materials", {})
    id2path = {}
    for k in ("videos", "photos"):
        for m in mats.get(k, []) or []:
            if isinstance(m, dict) and m.get("id"):
                id2path[m["id"]] = m.get("path", "")
    for tr in draft.get("tracks", []):
        if tr.get("type") != "video":
            continue
        for s in tr.get("segments", []):
            if id2path.get(s.get("material_id")) != cam_path:
                continue
            for g in s.get("common_keyframes") or []:
                if g.get("property_type") == "KFTypeScaleX":
                    kfs = [(int(k["time_offset"]), float(k["values"][0]))
                           for k in g.get("keyframe_list", []) if k.get("values")]
                    return sorted(kfs)
    return []


def compound_inner(inline_draft, _depth=0):
    """Путь исходника и длительность внутреннего видео-объекта компаунда, РЕКУРСИВНО сквозь вложенные компаунды
    (компаунд-в-компаунде): спускаемся до реального исходника, а не до внутреннего proxy (у него path='')."""
    mats = inline_draft.get("materials", {})
    id2path = {}
    for k in ("videos", "photos"):
        for m in mats.get(k, []) or []:
            if isinstance(m, dict) and m.get("id"):
                id2path[m["id"]] = m.get("path", "")
    draft_by_id = {m["id"]: m for m in mats.get("drafts", []) or [] if isinstance(m, dict)}
    for tr in inline_draft.get("tracks", []):
        if tr.get("type") != "video":
            continue
        for s in tr.get("segments", []):
            if _depth < 6:                              # вложенный компаунд? -> рекурсия до реального исходника
                for r in s.get("extra_material_refs", []):
                    m = draft_by_id.get(r)
                    if m and isinstance(m.get("draft"), dict):
                        p, dur = compound_inner(m["draft"], _depth + 1)
                        if p:
                            return p, dur
            p = id2path.get(s.get("material_id"))
            if p:
                dur = (s.get("target_timerange") or {}).get("duration", 0)
                return p, dur
    return None, 0


def write_dof_map(proj_dir, out_path=None):
    """Извлекает пару (effect_id, путь_внутр_видео) по каждому компаунду -> ve_dof_map.txt.
    Дилиб не может спарить фото-компаунды через draft-API, а тут связь структурно ясна;
    effect_id стабилен и есть в getseg-карте, дилиб находит эффект по id."""
    d = json.load(open(authoritative_draft(proj_dir), encoding="utf-8"))
    out = out_path or os.path.expanduser("~/Movies/CapCut/User Data/ve_dof_map.txt")
    # combination_id -> путь внешнего proxy (ручка): у текст-компаунда внутри нет видео/фото, идентичность даёт ручка
    draft_ids = {m["id"] for m in d["materials"].get("drafts", []) or []}
    vid_path = {mm["id"]: mm.get("path", "") for mm in d["materials"].get("videos", []) or []}
    proxy_path = {}
    for t in d["tracks"]:
        for s in t.get("segments", []):
            combo = next((r for r in s.get("extra_material_refs", []) if r in draft_ids), None)
            if combo:
                proxy_path[combo] = vid_path.get(s.get("material_id"), "")
    lines = []
    for m in d.get("materials", {}).get("drafts", []) or []:
        cd = m.get("draft")
        if not isinstance(cd, dict):
            continue
        ph = proxy_path.get(m["id"], "")   # путь внешнего proxy
        # РУЧКА (obj_*.png) на proxy имеет ПРИОРИТЕТ над реальным внутр. путём: она уникальна на копию,
        # а реальный исходник у дублей ОБЩИЙ → без этого layer_by_path схлопнул бы копии в один слой.
        p = ph if "/plugin_assets/obj_" in ph else (compound_inner(cd)[0] or ph)
        ve_ids = {x["id"] for x in cd["materials"].get("video_effects", []) if isinstance(x, dict) and x.get("effect_id") == EFFECT_ID}
        eid = None
        for tr in cd["tracks"]:
            if tr.get("type") == "effect":
                for s in tr["segments"]:
                    if s.get("material_id") in ve_ids or s.get("desc") == DOF_MARK:
                        eid = s.get("id")
        if p and eid:
            lines.append("%s\t%s" % (eid, p))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return lines


def write_seg_map(proj_dir, out_path=None):
    """Извлекает пару (живой video-seg-id, путь) внутреннего объекта каждого компаунда -> ve_segmap.txt.
    Дилиб резолвит документный сегмент по этому id (seg_by_id) — точный матч, обходит стрей-дубли того же пути
    (напр. осиротевшее фото на главном таймлайне, которое seg_by_path ошибочно выбирал)."""
    d = json.load(open(authoritative_draft(proj_dir), encoding="utf-8"))
    out = out_path or os.path.expanduser("~/Movies/CapCut/User Data/ve_segmap.txt")
    lines = []
    for m in d.get("materials", {}).get("drafts", []) or []:
        cd = m.get("draft")
        if not isinstance(cd, dict):
            continue
        id2p = {mm["id"]: mm.get("path", "") for k in ("videos", "photos") for mm in cd["materials"].get(k, [])}
        for tr in cd["tracks"]:
            if tr.get("type") == "video":
                for s in tr["segments"]:
                    p = id2p.get(s.get("material_id"))
                    if p and s.get("id"):
                        lines.append("%s\t%s" % (s["id"], p))
    # текст-компаунд: внутри нет видео-сегмента → драйвим ВНЕШНИЙ proxy (ключи на компаунде). Мапим его seg-id → ручка.
    draft_ids = {m["id"] for m in d["materials"].get("drafts", []) or []}
    vid_path = {mm["id"]: mm.get("path", "") for mm in d["materials"].get("videos", []) or []}
    for t in d["tracks"]:
        for s in t.get("segments", []):
            if any(r in draft_ids for r in s.get("extra_material_refs", [])):
                pp = vid_path.get(s.get("material_id"), "")
                if "/plugin_assets/obj_" in pp and s.get("id"):   # только ручки (беспутные), не реальные компаунды
                    lines.append("%s\t%s" % (s["id"], pp))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return lines


def report(proj_dir, rig_path, focus, aperture):
    d = json.load(open(authoritative_draft(proj_dir), encoding="utf-8"))
    rig = parse_rig(rig_path)
    zs = sorted(rig.values())
    print("=== RIG глубины ===")
    for p, z in sorted(rig.items(), key=lambda kv: kv[1]):
        print("  z=%5.2f  blur=%.3f  %s" % (z, blur_for_depth(z, focus, aperture), os.path.basename(p)))
    print("focus=%.2f aperture=%.2f  (z-range %.2f..%.2f)" % (focus, aperture, zs[0], zs[-1]) if zs else "no rig")
    print("=== КОМПАУНДЫ в проекте ===")
    for i, m in enumerate(d.get("materials", {}).get("drafts", []) or []):
        if not (isinstance(m, dict) and isinstance(m.get("draft"), dict)):
            continue
        p, dur = compound_inner(m["draft"])
        z = rig.get(p)
        b = blur_for_depth(z, focus, aperture) if z is not None else None
        print("  COMPOUND[%d] dur=%.2fs inner=%s | z=%s blur=%s"
              % (i, dur / 1e6, os.path.basename(p) if p else "?", z, ("%.3f" % b) if b is not None else "НЕТ в риге"))


def apply_dof(proj_dir, rig_path, focus, aperture, out_path=None):
    """Пишет блюр во ВСЕ существующие компаунды по глубине их объекта. out_path=None -> на месте (+.predof.bak).
    Возвращает список (inner_basename, z, blur). Отказывается писать в открытый проект (.locked), если пишем на месте."""
    src = os.path.join(proj_dir, "draft_info.json")
    on_place = out_path is None
    if on_place and os.path.exists(os.path.join(proj_dir, ".locked")):
        raise RuntimeError("проект открыт (.locked) — закрой CapCut перед записью на месте")
    d = json.load(open(src, encoding="utf-8"))
    rig = parse_rig(rig_path)
    cam_sx = read_camera_sx(d, rig_camera_path(rig_path))   # траектория зума камеры
    z_min = min(rig.values()) if rig else RIG_ZREF
    done = []
    for m in d.get("materials", {}).get("drafts", []) or []:
        if not (isinstance(m, dict) and isinstance(m.get("draft"), dict)):
            continue
        p, dur = compound_inner(m["draft"])
        z = rig.get(p)
        if z is None or not dur:
            continue
        kf = blur_curve(z, cam_sx, z_min, focus, aperture, dur) if cam_sx else None
        b = blur_for_depth(z, focus, aperture)   # значение материала (t=0 / статик)
        add_blur_to_compound(m["draft"], b, dur, keyframes=kf)
        rng = "%.2f..%.2f" % (min(v for _, v in kf), max(v for _, v in kf)) if kf else "%.3f" % b
        done.append((os.path.basename(p), z, rng))
    dst = out_path or src
    if on_place:
        bak = src + ".predof.bak"
        if not os.path.exists(bak):
            open(bak, "wb").write(open(src, "rb").read())
    with open(dst, "wb") as f:
        f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return done


def add_control_layer(proj_dir, focus=1.96, aperture=0.3):
    """Вживляет слой-контроллер DoF (прозрачный, uniform_scale off) + строку DOF в риг.
    ПРОЕКТ ДОЛЖЕН БЫТЬ ЗАКРЫТ. Юзер потом правит его масштаб X (диафрагма) / Y (фокус) в CapCut."""
    import capcut_track as ct
    if os.path.exists(os.path.join(proj_dir, ".locked")):
        raise RuntimeError("проект открыт (.locked) — закрой CapCut")
    d = json.load(open(authoritative_draft(proj_dir), encoding="utf-8"))
    cw = d["canvas_config"]["width"]; ch = d["canvas_config"]["height"]
    w0, w1 = None, None                       # диапазон = ОБЪЕДИНЕНИЕ окон ВСЕХ камер (DoF-слой глобален на все сцены)
    for t in d["tracks"]:
        if (t.get("name") or "").startswith("Камера 3D"):
            for s in t["segments"]:
                tr = s.get("target_timerange", {})
                st = tr.get("start", 0); en = st + tr.get("duration", 0)
                w0 = st if w0 is None else min(w0, st)
                w1 = en if w1 is None else max(w1, en)
    if w0 is None:
        w0, w1 = 0, d.get("duration", 5000000)
    dof_id = ct.add_dof_layer(proj_dir, cw, ch, w0, w1, focus=focus, aperture=aperture)
    if dof_id:
        png = os.path.expanduser("~/Movies/CapCut/User Data/plugin_assets/dof3d_%dx%d.png" % (int(cw), int(ch)))
        ct.set_dof_rig_line(dof_id, png)
    return dof_id


def wrap_and_enable(proj_dir, layer_ids, aperture=0.3, focus=1.96, handles=None, log=print):
    """DoF при запекании камеры: завернуть ВЫБРАННЫЕ объекты в сборные клипы с блюром,
    вживить слой-контроллер, сгенерить карты (объект↔эффект + живые seg-id), создать ve_dof.txt.
    Вызывается ВНУТРИ камерного бейка (проект уже ЗАКРЫТ, слой-камера и риг уже записаны).
    Блюр живьём перекатывает дилиб по глубине+наезду; seed=0 (резко) до первого драйва.
    handles: {seg_id: png} для беспутных (текст) — ручка в proxy, чтобы карта/дилиб резолвили компаунд.
    Возвращает {wrapped, pairs, segs, layer}."""
    import compound
    infos = compound.wrap_in_project(proj_dir, layer_ids, blur_value=0.0, handles=handles, log=log)
    # СЛОЙ-КОНТРОЛЛЕР ВЕРНУЛИ (решение Назара 2026-08-09). Промежуточная версия сажала фокус на
    # масштаб Y самой камеры, а диафрагму делала неанимируемой константой — «работало как часы»
    # было именно у слоя. Он же снимает ограничение «гейты глобальны на проект»: параметры живут
    # на дорожке, а не в одном файле на машину.
    # Вертикальный масштаб слоя = ГЛУБИНА (плоскость фокуса), горизонтальный = ДИАФРАГМА.
    did = add_control_layer(proj_dir, focus=focus, aperture=aperture)
    pairs = write_dof_map(proj_dir)                      # <effect_id>\t<путь> (бейк-time id; CapCut их перегенерит)
    segs = write_seg_map(proj_dir)                       # <живой video-seg-id>\t<путь>
    with open(os.path.expanduser("~/Movies/CapCut/User Data/ve_dof.txt"), "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (focus, aperture))
    log("DoF: завёрнуто %d, пар %d, живых-id %d, слой=%s"
        % (len(infos), len(pairs), len(segs), (did or "?")[:8]))
    bake_eids = [l.split("\t")[0] for l in pairs]        # id эффектов ДО открытия (CapCut их сменит)
    return {"wrapped": len(infos), "pairs": len(pairs), "segs": len(segs), "layer": did,
            "bake_eids": bake_eids}


def _compound_effect_ids(proj_dir):
    """id всех блюр-effect-сегментов в компаундах текущего draft_info.json (детект перегенерации CapCut)."""
    d = json.load(open(authoritative_draft(proj_dir), encoding="utf-8"))
    out = set()
    for m in d.get("materials", {}).get("drafts", []) or []:
        cd = m.get("draft")
        if not isinstance(cd, dict):
            continue
        ve_ids = {x["id"] for x in cd["materials"].get("video_effects", [])
                  if isinstance(x, dict) and x.get("effect_id") == EFFECT_ID}
        for tr in cd["tracks"]:
            if tr.get("type") == "effect":
                for s in tr["segments"]:
                    if s.get("material_id") in ve_ids or s.get("desc") == DOF_MARK:
                        out.add(s["id"])
    return out


def refresh_dof_map_async(proj_dir, bake_eids, log=print):
    """CapCut пересоздаёт id блюр-эффектов при ПЕРВОМ открытии синтезированного компаунда → ve_dof_map.txt
    (записанная при бейке) устаревает, и слой-контроллер не может модулировать блюр. Фоново ждём, пока
    CapCut запишет новые id в автосейв, и регеним карту — дилиб перечитывает её вживую (рестарт не нужен).
    Одноразово: после первого открытия id стабильны. Видео-segmap выживает, но регеним и его на всякий."""
    import threading
    import time as _t

    def poll():
        bake = set(bake_eids)
        for _ in range(48):                              # ~4 мин макс (5с × 48)
            _t.sleep(5)
            try:
                cur = _compound_effect_ids(proj_dir)
            except Exception:                            # noqa: BLE001 — файл могут переписывать, ретраим
                continue
            if cur and cur != bake:                      # CapCut перегенерил id → карта устарела → регеним
                try:
                    write_dof_map(proj_dir); write_seg_map(proj_dir)
                    log("DoF: карты обновлены под новые id CapCut (%d эффектов) — блюр-модуляция активна" % len(cur))
                except Exception as e:                   # noqa: BLE001
                    log("DoF: реген карт не удался (%s)" % e)
                return
    threading.Thread(target=poll, daemon=True).start()


def run_dof(proj_dir, focus=1.96, aperture=0.3, on=True, log=print):
    """Однокнопочный DoF: закрыть проект → вживить слой-контроллер + строку DOF в риг →
    сгенерить обе карты (объект↔эффект + живые seg-id) → создать ve_dof.txt → открыть.
    Выкл (on=False) — снести ve_dof.txt (дилиб видит вживую, реопен не нужен).
    ПРЕДПОСЫЛКИ (юзер делает в CapCut ДО кнопки): объекты завёрнуты в сборные клипы,
    в каждый вложен эффект «Размытие», 3D-камера настроена (есть ve_rig.txt со слоями).
    Идемпотентно — можно жать повторно после правок блюра/заворота."""
    import capcut_track as ct
    import capcut_ui
    dof_txt = os.path.expanduser("~/Movies/CapCut/User Data/ve_dof.txt")
    if not on:
        if os.path.exists(dof_txt):
            os.remove(dof_txt)
        log("DoF выключен (ve_dof.txt снят)")
        return {"on": False}
    if not os.path.exists(ct.VE_RIG_PATH):
        raise RuntimeError("нет ve_rig.txt — сначала настрой 3D-камеру (она задаёт глубины слоёв)")
    name = os.path.basename(proj_dir)
    was_open = ct._project_open(proj_dir) or capcut_ui.is_running()
    ct.write_progress(10, "DoF: закрываю проект…")
    # require_closed, а НЕ close_to_home: та начинается с «if not _project_open(): return», а этот
    # признак основан на ОТСУТСТВИИ улик и врёт — у свежего проекта нет .locked, у убитого через
    # pkill он unlink'нут. Тогда add_control_layer пишет слой в файл, который CapCut держит
    # открытым, и автосейв стирает его молча. Нужно ПОЛОЖИТЕЛЬНОЕ доказательство закрытости.
    # Образцы: run_track (трекинг) и commit_camera/prep_dof (камера).
    try:
        ct.require_closed(proj_dir, log)
    except ValueError:
        ct.write_progress(100, "")   # иначе плашка прогресса залипнет на 10%
        raise
    ct.write_progress(40, "DoF: слой-контроллер…")
    dof_id = add_control_layer(proj_dir)               # бейк слоя (проект закрыт) + строка DOF в риг
    ct.write_progress(70, "DoF: карты объект↔эффект…")
    pairs = write_dof_map(proj_dir)                    # <effect_id>\t<путь>
    segs = write_seg_map(proj_dir)                     # <живой video-seg-id>\t<путь>
    with open(dof_txt, "w", encoding="utf-8") as f:    # тумблер + базовые гейны фокуса/силы
        f.write("%s %s\n" % (focus, aperture))
    ct.write_progress(99, "DoF: открываю проект…")
    if was_open or capcut_ui.is_running():
        try:
            capcut_ui.reopen(name, os.path.join(proj_dir, ".locked"))
        except Exception as e:  # noqa: BLE001 — карты/слой уже записаны, реопен опционален
            log("не смог авто-открыть (%s) — открой проект вручную" % e)
    ct.write_progress(100, "Готово")
    log("DoF готов: слой=%s, пар %d, живых-id %d, focus=%s ap=%s"
        % ((dof_id or "?")[:8], len(pairs), len(segs), focus, aperture))
    return {"on": True, "layer": dof_id, "pairs": len(pairs), "segs": len(segs),
            "focus": focus, "aperture": aperture}


if __name__ == "__main__":
    # self-check формулы
    assert blur_for_depth(2.0, 2.0, 0.3) == 0.0                  # в фокусе -> резко
    assert abs(blur_for_depth(5.0, 2.0, 0.3) - 0.9) < 1e-9        # далеко -> сильный блюр
    assert blur_for_depth(100.0, 2.0, 0.3) == 1.0                 # кламп
    # Проект-фикстура. По умолчанию — проект автора; свой задать через NZLD_TEST_PROJECT.
    proj = os.path.expanduser("~/Movies/CapCut/User Data/Projects/com.lveditor.draft/"
                              + os.environ.get("NZLD_TEST_PROJECT", "0712 (1)"))
    rig = os.path.expanduser("~/Movies/CapCut/User Data/ve_rig.txt")
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    focus = float(pos[0]) if len(pos) > 0 else 1.96              # субъект (девушка) в фокусе
    aperture = float(pos[1]) if len(pos) > 1 else 0.3
    report(proj, rig, focus, aperture)
    # проверка записи на КОПИИ (живой проект открыт — не трогаем)
    if "--apply-copy" in sys.argv:
        tmp = "/private/tmp/claude-502/-Users-naza-prod-Documents-plugin/e0c78770-bc78-40c9-aa9d-492ab98ce3e0/scratchpad/draft_info.dof.json"
        done = apply_dof(proj, rig, focus, aperture, out_path=tmp)
        print("\n=== ЗАПИСЬ НА КОПИЮ ok -> %s ===" % tmp)
        for nm, z, b in done:
            print("  %s z=%.2f blur=%s" % (nm, z, b))
    # вживить слой-контроллер DoF (прозрачный, X=диафрагма Y=фокус) — ТОЛЬКО при закрытом проекте
    if "--layer" in sys.argv:
        did = add_control_layer(proj)
        print("\n=== СЛОЙ-КОНТРОЛЛЕР DoF вживлён: %s ===" % (did or "НЕ создан"))
        print("Открывай CapCut: слой «DoF контроллер» — тяни его масштаб X (диафрагма) и Y (фокус), можно кейфреймить.")
    # регенерация ve_dof_map.txt (пара объект↔эффект для дилиба) — запускать после настройки блюр-эффектов
    if "--map" in sys.argv:
        lines = write_dof_map(proj)
        print("\n=== ve_dof_map.txt (%d пар объект↔эффект) ===" % len(lines))
        for l in lines:
            print("  " + l.replace("\t", "  →  "))
        seglines = write_seg_map(proj)
        print("=== ve_segmap.txt (%d живых video-seg-id) ===" % len(seglines))
        for l in seglines:
            print("  " + l.replace("\t", "  →  "))
    # однокнопочный DoF целиком (слой + карты + тумблер + реопен). --off = выключить
    if "--run" in sys.argv or "--off" in sys.argv:
        res = run_dof(proj, focus, aperture, on=("--off" not in sys.argv))
        print("\n=== run_dof: %s ===" % res)
    # реальное применение на месте — ТОЛЬКО при закрытом проекте
    if "--apply" in sys.argv:
        done = apply_dof(proj, rig, focus, aperture)
        print("\n=== ПРИМЕНЕНО НА МЕСТЕ (бэкап .predof.bak) ===")
        for nm, z, b in done:
            print("  %s z=%.2f blur=%s" % (nm, z, b))
        print("Открывай CapCut — фон должен размыться по глубине.")
