#!/usr/bin/env python3
# compound.py — СИНТЕЗ сборного клипа (combination) прямо в JSON, байт-в-байт как делает CapCut сам.
# Зачем: DoF требует каждый объект в компаунде с блюром внутри, но сборные клипы нельзя выбрать
# после создания — значит заворачивать надо программно, в момент запекания камеры (пока выделение живо).
#
# Структура компаунда (снята с реального CapCut-компаунда, «0712 (1)»):
#   верхний proxy-сегмент (несёт трансформ объекта, material=proxy-видео path="", extra_type_option=2)
#     └ extra_material_refs[0] = combination-материал (drafts[]) c inline `draft`:
#         inline draft = полный саб-проект: объект (нейтральный clip, start=0) на video-треке
#                        + блюр-эффект на effect-треке (те же _blur_material/_blur_segment, что DoF).
# Персистентность: компаунд лежит ИДЕНТИЧНО в root draft_info.json И Timelines/*/draft_info.json —
# писать надо ОБА (как слой-бейки камеры/DoF), иначе CapCut перезагрузит из авторитетной Timelines-копии.
# ponytail: subdraft/<id>/ на диске CapCut держит устаревшим и игнорит (читает inline) — не синтезируем.
import copy
import glob
import json
import os
import uuid

import dof   # переиспуем _blur_material/_blur_segment (тот же EFFECT_ID, render_index 11000)

# ключи inline-draft, наследуемые от КОРНЕВОГО драфта проекта (версия/платформа/конфиг — не хардкодим)
_INHERIT = ("version", "new_version", "fps", "is_drop_frame_timecode", "color_space", "config",
            "platform", "last_modified_platform", "keyframes", "keyframe_graph_list", "relationships",
            "mixed_track_mode_on", "render_index_track_mode_on", "free_render_index_mode_on",
            "lyrics_effects", "path", "static_cover_image_path", "time_marks",
            "uneven_animation_template_info", "smart_ads_info", "function_assistant_info")
# aux-материалы объекта: их КИНДЫ (переезжают внутрь вместе с объектом; для proxy клонируем свежие)
_AUX_KINDS = ("speeds", "placeholder_infos", "canvases", "sound_channel_mappings",
              "material_colors", "vocal_separations", "loudnesses", "material_animations")


def uid():
    return str(uuid.uuid4()).upper()


def _mat_index(draft):
    """id материала -> (kind, dict). Плоский индекс по всем спискам materials."""
    idx = {}
    for kind, arr in draft.get("materials", {}).items():
        if isinstance(arr, list):
            for m in arr:
                if isinstance(m, dict) and m.get("id"):
                    idx[m["id"]] = (kind, m)
    return idx


def _find_seg(draft, seg_id):
    """(track, seg) верхнего сегмента по id, иначе (None, None)."""
    for t in draft.get("tracks", []):
        for s in t.get("segments", []):
            if s.get("id") == seg_id:
                return t, s
    return None, None


def is_compound(seg, draft_ids):
    """Сегмент уже завёрнут? (есть ссылка на combination-материал)."""
    return bool(set(seg.get("extra_material_refs", [])) & draft_ids)


def _inner_skeleton(root_draft, inner_id, name, dur, cw, ch):
    """Скелет inline-draft: наследует конфиг/платформу проекта от корня, свой id/имя/канвас."""
    sk = {k: copy.deepcopy(root_draft[k]) for k in _INHERIT if k in root_draft}
    sk.update({
        "id": inner_id, "name": name, "duration": int(dur), "draft_type": "video",
        "create_time": 0, "update_time": 0, "source": "default",
        "canvas_config": {"ratio": "original", "width": int(cw), "height": int(ch), "background": None},
        "group_container": None, "mutable_config": None, "cover": None, "retouch_cover": None,
        "extra_info": None, "color_space": root_draft.get("color_space", 0),
        "materials": {}, "tracks": [],
    })
    return sk


def _proxy_for_text(draft, name, mid, cw, ch, dur):
    """proxy-видео для ЗАВЁРНУТОГО ТЕКСТА. Клонировать текстовый материал нельзя: у него нет
    полей видео (габарит, длительность, crop), и движок такой «видео» не примет. Берём за образец
    ЖИВОЙ видео-материал того же проекта — так набор полей заведомо тот, что ждёт эта версия
    CapCut, — и правим размеры под канвас."""
    src = None
    for m in draft.get("materials", {}).get("videos", []) or []:
        if isinstance(m, dict) and m.get("type") in ("video", "photo"):
            src = m
            break
    if src is None:
        raise ValueError("в проекте нет ни одного видео-материала — не из чего сделать прокси "
                         "сборного клипа для текста")
    m = _proxy_video(src, name, mid)
    m.update({"width": int(cw), "height": int(ch), "duration": int(dur),
              "type": "video", "has_audio": False, "crop_ratio": "free", "crop_scale": 1.0})
    return m


def _proxy_video(obj_mat, name, mid=None):
    """proxy-видео компаунда = клон видео-материала объекта с обнулёнными путями + маркер компаунда.
    mid — id из общего бандла заворота: без него копии драфта разъезжались (см. make_ids)."""
    m = copy.deepcopy(obj_mat)
    m["id"] = mid or uid()
    m["type"] = "video"
    m["path"] = ""
    m["media_path"] = ""
    m["material_name"] = name
    m["material_id"] = ""
    m["extra_type_option"] = 2          # маркер сборного клипа
    m["has_audio"] = False
    m["source"] = 0
    if isinstance(m.get("video_algorithm"), dict):
        m["video_algorithm"]["path"] = ""    # кэш-рендер объекта к proxy не относится (композит живой)
    return m


def make_ids():
    """Свежие id для одного заворота (генерим ОДИН раз → применяем к root и Timelines одинаково).

    ВСЕ id обязаны совпадать в root и Timelines/*. Движок грузит копию из Timelines, а весь наш
    код читает корневую (ct.read_draft). Пока копии идентичны, это безопасно — и именно на этом
    инварианте держится вся запись «в оба файла» (см. capcut_track.py:580). Раньше часть id
    чеканилась ВНУТРИ пофайлового цикла (материал прокси, материал и сегмент блюра, копии aux),
    копии расходились, и ve_dof_map.txt ссылался на сегменты, которых в ЗАГРУЖЕННОЙ копии нет:
    глубина резкости молча не работала (в логе «DOF resolve: слоёв=0 из 3 пар»).

    sub(role, i) — стабильный id на любую роль и индекс, выведенный из общего корня заворота.
    Так одинаковость получается ПО ПОСТРОЕНИЮ, а не перечислением мест: сколько бы ролей ни
    добавили потом, они совпадут в обеих копиях без дополнительных правок.
    """
    base = uuid.uuid4().hex

    def sub(role, i=0):
        return str(uuid.uuid5(uuid.NAMESPACE_OID, "%s:%s:%d" % (base, role, i))).upper()

    ids = {k: sub(k) for k in ("inner", "combo", "combination", "proxy_seg", "vtrack", "etrack",
                               "proxy_mat", "blur_mat", "blur_seg")}
    ids["sub"] = sub
    return ids


def wrap_segment(draft, seg_id, ids, blur_keyframes=None, blur_value=0.0, name="Сборный клип", nest=False, handle_path=None, text_stretch=None):
    """Заворачивает верхний видео-сегмент seg_id в сборный клип с блюром внутри. Мутирует draft НА МЕСТЕ.
    ids — из make_ids() (общие для всех копий файла). Возвращает dict сведений либо None если не вышло.
    nest=False: если сегмент уже компаунд — идемпотентно вернёт {'already': True} (не двойним при бейке).
    nest=True: заворачиваем ДАЖЕ уже-компаунд (компаунд-в-компаунде) — вложенный combination едет внутрь целиком.
    handle_path: для БЕСПУТНОГО объекта (текст) — путь-ручка в proxy-материал, чтобы компаунд резолвился по нему.
    text_stretch: k у текста — «ужать переростка», см. ниже. Без него масштабы не трогаем."""
    draft_ids = {m["id"] for m in draft.get("materials", {}).get("drafts", []) or []}
    trk, seg = _find_seg(draft, seg_id)
    if not seg:
        return None
    if is_compound(seg, draft_ids) and not nest:
        return {"already": True, "seg": seg_id}
    idx = _mat_index(draft)
    obj_mid = seg.get("material_id")
    if obj_mid not in idx or idx[obj_mid][0] not in ("videos", "texts"):
        return None                                  # видео/фото и текст; остальное не заворачиваем
    obj_kind, obj_mat = idx[obj_mid]
    cw = draft["canvas_config"]["width"]; ch = draft["canvas_config"]["height"]
    dur = int(seg.get("target_timerange", {}).get("duration", 0)) or int(draft.get("duration", 5000000))

    # --- inline draft: объект (нейтральный clip, локальные тайминги) + блюр ---
    inner = _inner_skeleton(draft, ids["inner"], name, dur, cw, ch)
    inner_mats = {}
    moved = {obj_mid}                                 # id материалов, уносимых внутрь (удалить из корня)
    obj_seg = copy.deepcopy(seg)                      # объект переезжает внутрь
    # УЖАТЬ ПЕРЕРОСТКА. Коробка компаунда на углы больше НЕ влияет — cornerpin сажает на цель
    # сами чернила (`quad_for_ink`), а куда уехала коробка, неважно. Осталась одна причина
    # трогать масштаб: надпись ШИРЕ внутреннего канваса режется по его краю. Поэтому
    # k = min(1, W/w_чернил) — только ужимаем, и ровно настолько же возвращаем на прокси.
    # ⚠️ k из драфта не вычисляется: габарита надписи там нет (замер: fixed_width/height = -1,
    # только метрики шрифта). Поэтому k — вход, а не догадка; 1.0 = масштабы не трогаем.
    k = float(text_stretch or 1.0)
    obj_seg["clip"] = {"scale": {"x": k, "y": k}, "rotation": 0.0,
                       "transform": {"x": 0.0, "y": 0.0},
                       "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0}
    obj_seg["target_timerange"] = {"start": 0, "duration": dur}
    obj_seg["uniform_scale"] = {"on": True, "value": 1.0}
    obj_seg["common_keyframes"] = []                  # трансформ-ключи остаются на proxy (двигается весь компаунд)
    obj_seg["keyframe_refs"] = []
    # перенести материал объекта + его aux внутрь (те же id — inline-namespace изолирован).
    # ВКЛЮЧАЯ drafts (вложенный combination): при заворачивании компаунда он едет внутрь целиком (компаунд-в-компаунде).
    for mid in [obj_mid] + list(seg.get("extra_material_refs", [])):
        if mid in idx:
            kind, m = idx[mid]
            inner_mats.setdefault(kind, []).append(copy.deepcopy(m))
            moved.add(mid)
    inner["materials"] = inner_mats
    # блюр внутрь (те же конструкторы, что DoF-бейк)
    blur_mat = dof._blur_material(blur_keyframes[0][1] if blur_keyframes else blur_value,
                                  ids.get("blur_mat"))
    inner_mats.setdefault("video_effects", []).append(blur_mat)
    blur_seg = dof._blur_segment(blur_mat["id"], dur, blur_keyframes, ids)
    inner["tracks"] = [
        {"id": ids["vtrack"], "type": "video", "segments": [obj_seg],
         "flag": 0, "attribute": 0, "name": "", "is_default_name": True},
        {"id": ids["etrack"], "type": "effect", "segments": [blur_seg],
         "flag": 0, "attribute": 0, "name": "", "is_default_name": True},
    ]

    # --- combination-материал (drafts[]) ---
    ph = _draftpath_placeholder(draft)
    combo = {"id": ids["combo"], "type": "combination", "name": "", "category_id": "", "category_name": "",
             "formula_id": "", "combination_id": ids["combination"], "combination_type": "none",
             "aimusic_mv_template_info": None, "precompile_combination": False,
             "draft_file_path": "%s/subdraft/%s/draft_content.json" % (ph, ids["inner"]),
             "draft_cover_path": "%s/subdraft/%s/draft_cover.jpg" % (ph, ids["inner"]),
             "draft_config_path": "%s/subdraft/%s/sub_draft_config.json" % (ph, ids["inner"]),
             "draft": inner}
    draft["materials"].setdefault("drafts", []).append(combo)

    # --- proxy-видео + свежие aux для верхнего сегмента ---
    if obj_kind == "texts":
        proxy = _proxy_for_text(draft, name, ids.get("proxy_mat"), cw, ch, dur)
    else:
        proxy = _proxy_video(obj_mat, name, ids.get("proxy_mat"))
    if handle_path:                              # беспутный объект (текст): ручка → компаунд резолвится по ней
        proxy["path"] = handle_path; proxy["media_path"] = handle_path
    draft["materials"].setdefault("videos", []).append(proxy)
    proxy_aux = []
    for ai, mid in enumerate(seg.get("extra_material_refs", [])):
        if mid in idx:
            kind, m = idx[mid]
            if kind == "drafts":
                continue
            # индекс, а не uid(): число копий одинаково в обеих копиях драфта, значит и id совпадут
            c = copy.deepcopy(m); c["id"] = ids["sub"]("aux", ai)
            draft["materials"].setdefault(kind, []).append(c)
            proxy_aux.append(c["id"])

    # --- верхний сегмент становится proxy: трансформ объекта остаётся, материал=proxy, ref на combo ---
    seg["id"] = ids["proxy_seg"]
    # КОМПЕНСАЦИЯ СНАРУЖИ: внутри ужали на k, снаружи ровно на столько же вернули — видимый
    # размер надписи не меняется. Канвас компаунда всегда кадровый, поэтому «вписывание» слоя
    # (`fit` в `_layer_matrix`) здесь тождество и в формулу не входит.
    if obj_kind == "texts" and k != 1.0:
        sc = seg.get("clip", {}).get("scale", {}) or {}
        seg.setdefault("clip", {})["scale"] = {"x": float(sc.get("x", 1.0)) / k,
                                               "y": float(sc.get("y", 1.0)) / k}
    seg["material_id"] = proxy["id"]
    seg["extra_material_refs"] = [ids["combo"]] + proxy_aux

    # --- вычистить уносимые материалы из корня (их единственным ссылателем был этот сегмент) ---
    still_ref = set()
    for t in draft["tracks"]:
        for s in t["segments"]:
            still_ref.add(s.get("material_id"))
            still_ref.update(s.get("extra_material_refs", []))
    for kind in list(draft["materials"].keys()):
        arr = draft["materials"][kind]
        if isinstance(arr, list):
            draft["materials"][kind] = [m for m in arr
                                        if not (isinstance(m, dict) and m.get("id") in moved
                                                and m.get("id") not in still_ref)]
    return {"inner": ids["inner"], "combo": ids["combo"], "proxy_seg": ids["proxy_seg"],
            "blur_seg": blur_seg["id"], "effect_id": blur_seg["id"],
            "obj_path": handle_path or obj_mat.get("path", ""), "name": name}


def _draftpath_placeholder(draft):
    """Префикс ##_draftpath_placeholder_<UUID>_## из существующего компаунда, иначе от id проекта."""
    for m in draft.get("materials", {}).get("drafts", []) or []:
        p = m.get("draft_config_path", "")
        if "placeholder_" in p and "_##" in p:
            return p.split("_##")[0] + "_##"
    return "##_draftpath_placeholder_%s_##" % draft.get("id", uid())


def wrap_in_project(proj_dir, seg_ids, blur_value=0.0, name_prefix="Сборный клип", nest=False, handles=None, stretches=None, log=print):
    """Заворачивает список верхних сегментов в сборные клипы с блюром — во ВСЕХ авторитетных копиях
    (root draft_info.json + Timelines/*/draft_info.json), одинаковыми id. ПРОЕКТ ДОЛЖЕН БЫТЬ ЗАКРЫТ.
    nest=True: заворачивать даже уже-компаунды (компаунд-в-компаунде). Бэкап .prewrap.bak."""
    root = os.path.join(proj_dir, "draft_info.json")
    if os.path.exists(os.path.join(proj_dir, ".locked")):
        raise RuntimeError("проект открыт (.locked) — закрой CapCut перед заворотом")
    files = [root] + glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json"))
    # общий id-бандл на каждый сегмент (одинаковый во всех файлах)
    n = 0
    plans = {sid: make_ids() for sid in seg_ids}
    names = {sid: "%s%d" % (name_prefix, i + 1) for i, sid in enumerate(seg_ids)}
    infos = []
    for fp in files:
        d = json.load(open(fp, encoding="utf-8"))
        touched = False
        for sid in seg_ids:
            r = wrap_segment(d, sid, plans[sid], blur_value=blur_value, name=names[sid], nest=nest,
                             handle_path=(handles or {}).get(sid),
                             text_stretch=(stretches or {}).get(sid))
            if r and not r.get("already"):
                touched = True
                if fp == root:
                    infos.append(r)
            elif r and r.get("already") and fp == root:
                log("сегмент %s уже компаунд — пропуск" % sid[:8])
        if touched:
            if not os.path.exists(fp + ".prewrap.bak"):
                open(fp + ".prewrap.bak", "wb").write(open(fp, "rb").read())
            with open(fp, "wb") as f:
                f.write(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            n += 1
            _write_subdraft(proj_dir, d, plans)      # диск-сабдрафты (на случай, если CapCut их проверяет)
    log("заворот: %d объектов, %d файлов" % (len(infos), n))
    return infos


def _write_subdraft(proj_dir, draft, plans):
    """Пишет subdraft/<inner_id>/{draft_content.json, sub_draft_config.json} для каждого нового компаунда.
    ponytail: CapCut читает inline-копию, эти файлы скорее для панели медиатеки; дёшево — пишем на всякий."""
    inner_ids = {p["inner"] for p in plans.values()}
    id2combo = {m["draft"]["id"]: m for m in draft.get("materials", {}).get("drafts", []) or []
                if isinstance(m.get("draft"), dict)}
    for iid in inner_ids:
        combo = id2combo.get(iid)
        if not combo:
            continue
        sd = os.path.join(proj_dir, "subdraft", iid)
        os.makedirs(sd, exist_ok=True)
        inner = combo["draft"]
        with open(os.path.join(sd, "draft_content.json"), "wb") as f:
            f.write(json.dumps(inner, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        cfg = {"audio_path": "", "cover_height": inner["canvas_config"]["height"],
               "cover_path": "draft_cover.jpg", "cover_width": inner["canvas_config"]["width"],
               "create_time": 0, "draft_json_file": "draft_content.json", "id": iid,
               "import_time_ms": 0, "is_from_multi_timeline": False, "is_from_sub_draft": True,
               "name": inner.get("name", ""), "project_id": iid,
               "rough_cut_duration": inner.get("duration", 0), "rough_cut_start": 0,
               "source": "timeline", "type": "video"}
        with open(os.path.join(sd, "sub_draft_config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)


def plain_video_segs(draft):
    """id всех НЕзавёрнутых верхних видео-сегментов (не камера/DoF-слой)."""
    draft_ids = {m["id"] for m in draft.get("materials", {}).get("drafts", []) or []}
    out = []
    for t in draft.get("tracks", []):
        if t.get("type") == "video" and not (t.get("name") or "").startswith("Камера 3D") and t.get("name") != "DoF контроллер":
            for s in t.get("segments", []):
                if s.get("material_id") and not is_compound(s, draft_ids):
                    out.append(s["id"])
    return out


def restore(proj_dir, log=print):
    """Откат заворота: вернуть все *.prewrap.bak (root + Timelines). Число восстановленных файлов."""
    n = 0
    for bak in [os.path.join(proj_dir, "draft_info.json.prewrap.bak")] + \
               glob.glob(os.path.join(proj_dir, "Timelines", "*", "draft_info.json.prewrap.bak")):
        if os.path.exists(bak):
            open(bak[:-len(".prewrap.bak")], "wb").write(open(bak, "rb").read())
            os.remove(bak); n += 1
    log("восстановлено %d файлов из .prewrap.bak" % n)
    return n


def _selftest():
    # СТРУКТУРНЫЙ self-check: заворачиваем реальный сегмент на КОПИИ проекта и сверяем схему
    # с эталонным CapCut-компаундом (те же ключи сегмента, proxy-материала, combination-материала).
    # Проект-фикстура. По умолчанию — проект автора; свой задать через NZLD_TEST_PROJECT.
    proj = os.path.expanduser("~/Movies/CapCut/User Data/Projects/com.lveditor.draft/"
                              + os.environ.get("NZLD_TEST_PROJECT", "0712 (1)"))
    d = json.load(open(os.path.join(proj, "draft_info.json"), encoding="utf-8"))
    draft_ids = {m["id"] for m in d["materials"].get("drafts", [])}
    # взять НЕзавёрнутый верхний видео-сегмент
    plain = None
    for t in d["tracks"]:
        if t.get("type") == "video" and not (t.get("name") or "").startswith("Камера 3D") and t.get("name") != "DoF контроллер":
            for s in t["segments"]:
                if not (set(s.get("extra_material_refs", [])) & draft_ids) and s.get("material_id"):
                    plain = s["id"]; break
        if plain:
            break
    assert plain, "не нашёл незавёрнутый сегмент для теста"
    before_vids = len(d["materials"]["videos"])
    ids = make_ids()
    r = wrap_segment(d, plain, ids, blur_value=0.3, name="Сборный клип TEST")
    assert r and not r.get("already"), r
    # 1) верхний сегмент теперь ссылается на combination-материал
    _, pseg = _find_seg(d, ids["proxy_seg"])
    assert pseg and ids["combo"] in pseg["extra_material_refs"], "proxy не ссылается на combo"
    # 2) combination-материал есть, с inline draft, а в нём объект + блюр
    combo = next(m for m in d["materials"]["drafts"] if m["id"] == ids["combo"])
    inner = combo["draft"]
    assert combo["type"] == "combination" and combo["combination_type"] == "none"
    ntr = {t["type"]: len(t["segments"]) for t in inner["tracks"]}
    assert ntr.get("video") == 1 and ntr.get("effect") == 1, ntr
    ve = inner["materials"]["video_effects"]
    blur = next(p["value"] for p in ve[0]["adjust_params"] if p["name"] == "effects_adjust_blur")
    assert abs(blur - 0.3) < 1e-9, blur
    # 3) proxy-видео: path пустой, маркер компаунда
    proxy = next(m for m in d["materials"]["videos"] if m["id"] == pseg["material_id"])
    assert proxy["path"] == "" and proxy["extra_type_option"] == 2, "proxy не помечен как компаунд"
    top_vid_ids = {m["id"] for m in d["materials"]["videos"]}
    assert r["obj_path"] and pseg["material_id"] in top_vid_ids, "proxy не в корне"
    assert len(d["materials"]["videos"]) == before_vids, "объект уехал внутрь, proxy пришёл → счётчик неизменен"
    # 4) ключи сегмента/материала совпадают с эталонными CapCut (ни одного лишнего/недостающего)
    SP = "/private/tmp/claude-502/-Users-naza-prod-Documents-plugin/baee150b-4b7c-471a-9420-0fb52b55f208/scratchpad"
    if os.path.exists(SP + "/ref_comp_segment.json"):
        ref_seg = set(json.load(open(SP + "/ref_comp_segment.json")).keys())
        ref_prx = set(json.load(open(SP + "/ref_proxy_video.json")).keys())
        assert set(pseg.keys()) == ref_seg, "разошлись ключи сегмента: %s" % (set(pseg.keys()) ^ ref_seg)
        assert set(proxy.keys()) == ref_prx, "разошлись ключи proxy: %s" % (set(proxy.keys()) ^ ref_prx)
    # 5) сериализуется в валидный JSON
    json.dumps(d, ensure_ascii=False)
    print("OK: заворот структурно валиден, схема совпала с эталонным CapCut-компаундом")
    print("   proxy_seg=%s combo=%s inner=%s blur=%.2f" % (ids["proxy_seg"][:8], ids["combo"][:8], ids["inner"][:8], blur))


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args or "--selftest" in args:
        _selftest()
    else:                                            # заворот РЕАЛЬНОГО проекта (для теста персистентности)
        name = args[0]
        proj = os.path.expanduser("~/Movies/CapCut/User Data/Projects/com.lveditor.draft/" + name)
        if not os.path.isdir(proj):
            sys.exit("нет проекта: " + proj)
        if "--restore" in args:
            restore(proj)
        else:
            blur = 0.3
            for a in args[1:]:
                try:
                    blur = float(a); break
                except ValueError:
                    pass
            d = json.load(open(os.path.join(proj, "draft_info.json"), encoding="utf-8"))
            segs = plain_video_segs(d)
            print("заворачиваю %d объектов (blur=%.2f): %s" % (len(segs), blur, [s[:8] for s in segs]))
            infos = wrap_in_project(proj, segs, blur_value=blur)
            for r in infos:
                print("  ✓ %s -> компаунд %s (эффект %s)" % (os.path.basename(r["obj_path"]) or "?", r["proxy_seg"][:8], r["blur_seg"][:8]))
            print("Готово. Открой проект в CapCut и проверь: объекты стали «Сборный клип», внутри — эффект «Размытие».")
            print("Откат: .venv/bin/python compound.py %r --restore" % name)
