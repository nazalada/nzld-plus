#!/usr/bin/env python3
"""Заворот текста: «ужать переростка» и не тронуть остальное.

Коробка компаунда на углы больше НЕ влияет — cornerpin сажает на цель сами чернила, — поэтому
растягивать надпись незачем. Осталась одна причина трогать масштаб: надпись ШИРЕ внутреннего
канваса режется по его краю. Отсюда k = min(1, W_канваса / w_чернил): ужимаем внутри и ровно
на столько же возвращаем на прокси, чтобы картинка не поехала.

Запуск: .venv/bin/python test_text_stretch.py
"""
import os
import sys

import compound

CW, CH = 1080, 1920
W_INK, H_INK = 325.8, 227.2    # чернила надписи со стенда 0808, px канваса (движок, гейт BBOX2)
S0 = 0.8                       # масштаб текста у пользователя — должен пережить заворот


def text_draft():
    """Минимальный драфт с ТЕКСТОВЫМ сегментом. Видео-материал обязателен: прокси для текста
    лепится по образцу живого видео того же проекта."""
    return {
        "canvas_config": {"width": CW, "height": CH},
        "duration": 5000000,
        "materials": {
            "videos": [{"id": "MAT-VID", "type": "video", "path": "/tmp/x.mov",
                        "media_path": "/tmp/x.mov", "material_name": "x"}],
            "texts": [{"id": "MAT-TXT", "content": "{}", "font_size": 15.0}],
            "speeds": [{"id": "AUX-1"}],
            "drafts": [],
        },
        "tracks": [
            {"id": "TRK-V", "type": "video", "segments": [{
                "id": "SEG-VID", "material_id": "MAT-VID",
                "target_timerange": {"start": 0, "duration": 5000000},
                "extra_material_refs": [], "common_keyframes": [], "keyframe_refs": [],
                "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0},
                         "rotation": 0.0, "alpha": 1.0,
                         "flip": {"vertical": False, "horizontal": False}}}]},
            {"id": "TRK-T", "type": "text", "segments": [{
                "id": "SEG-TXT", "material_id": "MAT-TXT",
                "target_timerange": {"start": 0, "duration": 5000000},
                "extra_material_refs": ["AUX-1"], "common_keyframes": [], "keyframe_refs": [],
                "clip": {"scale": {"x": S0, "y": S0}, "transform": {"x": 0.1, "y": -0.2},
                         "rotation": 0.0, "alpha": 1.0,
                         "flip": {"vertical": False, "horizontal": False}}}]},
        ],
    }


def wrapped(k):
    """Завернуть текст → (масштаб внутри, масштаб прокси, канвас компаунда, габарит прокси-материала)."""
    d = text_draft()
    r = compound.wrap_segment(d, "SEG-TXT", compound.make_ids(), text_stretch=k, name="Текст 3D 1")
    assert r and not r.get("already"), "заворот текста не состоялся: %s" % (r,)
    inner = [m for m in d["materials"]["drafts"] if m["id"] == r["combo"]][0]["draft"]
    ins = [s for t in inner["tracks"] if t["type"] == "video" for s in t["segments"]][0]
    proxy = [s for t in d["tracks"] for s in t["segments"] if s["id"] == r["proxy_seg"]][0]
    pm = [m for m in d["materials"]["videos"] if m["id"] == proxy["material_id"]][0]
    cc = inner["canvas_config"]
    return (ins["clip"]["scale"]["x"], proxy["clip"]["scale"]["x"],
            (cc["width"], cc["height"]), (pm["width"], pm["height"]))


def k_for(ink_w):
    """Ровно та же формула, что в wrap_texts_for_corners: только УЖИМАЕМ переростка."""
    return min(1.0, CW / ink_w)


def main():
    # ── надпись помещается в кадр: не трогаем НИЧЕГО ──────────────────────────────────
    # Коробка на углы больше не влияет (цель ставится чернилам), поэтому растягивать незачем.
    k = k_for(W_INK)
    assert k == 1.0, "надпись уже кадра, а k получился %.3f — значит формула снова растягивает" % k
    ins, prx, box, mat = wrapped(k)
    assert (ins, prx) == (1.0, S0), "при k=1 масштабы обязаны остаться как были: %s" % ((ins, prx),)
    assert box == (CW, CH) == mat, "канвас компаунда и габарит прокси = кадр, а вышло %s / %s" % (box, mat)
    print("надпись %.0f px в кадре %d: k=1.0, масштабы и коробка не тронуты" % (W_INK, CW))

    # ── ПЕРЕРОСТОК шире кадра: ужимаем, иначе режется по краю внутреннего канваса ──────
    wide = CW * 2.2
    kw = k_for(wide)
    assert abs(kw - CW / wide) < 1e-12, "переросток должен ужиматься ровно в W/w_чернил"
    ins2, prx2, box2, mat2 = wrapped(kw)
    assert abs(wide * kw - CW) < 1e-6, "после ужатия надпись обязана ровно влезать: %.2f ≠ %d" % (wide * kw, CW)
    assert abs(ins2 - kw) < 1e-9, "внутри компаунда масштаб должен быть k, а он %.6f" % ins2
    assert abs(prx2 - S0 / kw) < 1e-9, "на прокси масштаб должен быть s0/k, а он %.6f" % prx2
    # КАРТИНКА НЕ МЕНЯЕТСЯ: внутри ужали, снаружи ровно настолько же вернули
    assert abs(ins2 * prx2 - S0) < 1e-9, "видимый размер надписи уехал: %.6f ≠ %.6f" % (ins2 * prx2, S0)
    assert box2 == (CW, CH) == mat2, "канвас компаунда обязан остаться кадровым: %s" % (box2,)
    print("переросток %.0f px: k=%.3f, влез ровно в %d, видимый размер %.3f — как был"
          % (wide, kw, CW, ins2 * prx2))

    # ── ЖИВОЙ заворот: модель в памяти обязана кормить математику углов ───────────────
    # После живой команды компаунда на диске ещё нет, а весь хвост читает драфт из памяти.
    # Если patch_live_wrap соврёт, углы поедут молча — поэтому проверяем то, что читает хвост.
    import capcut_track
    d = text_draft()
    kw = k_for(CW * 2.2)
    proxy = capcut_track.patch_live_wrap(d, "SEG-TXT", {"proxy": "NEW", "k": kw,
                                                        "ink": (W_INK, H_INK)})
    ok, kind = capcut_track.cornerpin_capable(d, proxy)
    assert ok and kind == "videos", "прокси в модели не годится под углы: %s" % kind
    fx, fy = capcut_track.ink_fractions(d, proxy, (W_INK, H_INK))
    # доля считается от РЕАЛЬНОГО размера чернил внутри: масштаб текста s0, ужатый на k
    assert abs(fx - W_INK * S0 * kw / CW) < 1e-9 and abs(fy - H_INK * S0 * kw / CH) < 1e-9, \
        "доли чернил в коробке посчитались не так: %.4f × %.4f" % (fx, fy)
    inner = d["materials"]["drafts"][-1]["draft"]["tracks"][0]["segments"][0]
    assert abs(inner["clip"]["scale"]["x"] * proxy["clip"]["scale"]["x"] - S0) < 1e-9, \
        "видимый размер надписи в живой модели уехал"
    assert d["materials"]["drafts"][-1]["draft"]["canvas_config"] == {"width": CW, "height": CH}, \
        "канвас живого компаунда обязан быть кадровым — движок делает только такой"
    print("живая модель: коробка %d×%d, чернила заняли %.4f × %.4f, видимый размер сохранён"
          % (CW, CH, fx, fy))

    # ── живой заворот не должен раздвигать чужие клипы ────────────────────────────────
    # Команда кладёт компаунд на дорожку 0 (`track_index: 0` зашит в payload гейта). Если там
    # что-то стоит в это же время, движок его сдвинет — на стенде исходное видео уехало с 0 на
    # 2 с, и трекер пошёл по сдвинутому клипу. Пока настоящий track_index не пробросили,
    # живой путь берётся только когда двигать нечего.
    d = text_draft()                       # видео на дорожке 0 и текст пересекаются по времени
    ok_live, why = capcut_track._livewrap_safe(d, ["SEG-TXT"])
    assert not ok_live and "дорожке 0" in why, "страховка не заметила чужой клип на нулевой дорожке"
    d["tracks"][0]["segments"][0]["target_timerange"] = {"start": 9 * 10 ** 6, "duration": 10 ** 6}
    ok_live, _ = capcut_track._livewrap_safe(d, ["SEG-TXT"])
    assert ok_live, "двигать нечего, а живой путь всё равно запрещён"
    print("страховка дорожки 0: пересечение — на файловый путь, пусто — живой")

    # ── гейт живого заворота обязан убираться за собой ────────────────────────────────
    # Оставленный входной гейт стреляет заново при открытии ЛЮБОГО проекта — по протухшему
    # id, вслепую. Про ОТВЕТ здесь не судим: если CapCut запущен, выстрел настоящий, а
    # `ve_wrap_out.txt` умеет соврать (id берётся как «новое в getseg-карте», см. заметку).
    import capcut_track
    capcut_track.wrap_live({"id": "00000000-0000-0000-0000-000000000000",
                            "target_timerange": {"start": 0, "duration": 1000000}},
                           timeout=0.5, log=lambda *_: None)
    assert not os.path.exists(capcut_track.VE_WRAP_PATH), "ve_wrap.txt остался заряженным"
    print("гейт живого заворота: снят в любом случае")

    print("ВСЁ ПРОШЛО")
    return 0


sys.exit(main())
