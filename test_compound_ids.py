#!/usr/bin/env python3
"""Инвариант заворота: обе копии драфта получают ОДИНАКОВЫЕ id.

Зачем именно этот тест: CapCut грузит Timelines/<main>/draft_info.json, а весь наш код читает
корневую копию. Пока копии идентичны — безопасно. Когда часть id чеканилась внутри пофайлового
цикла, копии расходились, ve_dof_map.txt ссылался на несуществующие в модели сегменты, и глубина
резкости молча не работала («DOF resolve: слоёв=0 из 3 пар»). Ошибка была НЕВИДИМА: оба файла
по отдельности валидны, а сломано только их СОВПАДЕНИЕ — глазами не заметишь.

Запуск: .venv/bin/python test_compound_ids.py
"""
import copy
import sys

import compound


def minimal_draft():
    """Минимальный драфт, которого хватает wrap_segment: один видеосегмент с материалом и aux."""
    return {
        "canvas_config": {"width": 1920, "height": 1080},
        "duration": 5000000,
        "materials": {
            "videos": [{"id": "MAT-OBJ", "type": "video", "path": "/tmp/x.png",
                        "media_path": "/tmp/x.png", "material_name": "x"}],
            "speeds": [{"id": "AUX-1"}],
            "canvases": [{"id": "AUX-2"}],
            "drafts": [],
        },
        "tracks": [{"id": "TRK", "type": "video", "segments": [{
            "id": "SEG-OBJ", "material_id": "MAT-OBJ",
            "target_timerange": {"start": 0, "duration": 5000000},
            "extra_material_refs": ["AUX-1", "AUX-2"],
            "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0},
                     "rotation": 0.0, "alpha": 1.0,
                     "flip": {"vertical": False, "horizontal": False}},
            "common_keyframes": [], "keyframe_refs": [],
        }]}],
    }


def all_ids(node, acc):
    """Все значения полей id/material_id/extra_material_refs — рекурсивно, в порядке обхода."""
    if isinstance(node, dict):
        for k in ("id", "material_id"):
            v = node.get(k)
            if isinstance(v, str) and v:
                acc.append("%s=%s" % (k, v))
        for v in node.get("extra_material_refs") or []:
            acc.append("ref=%s" % v)
        for k in sorted(node):
            if k not in ("id", "material_id", "extra_material_refs"):
                all_ids(node[k], acc)
    elif isinstance(node, list):
        for v in node:
            all_ids(v, acc)
    return acc


def main():
    bad = 0
    ids = compound.make_ids()          # ОДИН бандл на заворот — как в wrap_in_project

    # два файла = две копии одного проекта, заворачиваются одним и тем же бандлом
    a, b = minimal_draft(), minimal_draft()
    ra = compound.wrap_segment(a, "SEG-OBJ", ids, blur_value=0.0, name="Сборный 1")
    rb = compound.wrap_segment(b, "SEG-OBJ", ids, blur_value=0.0, name="Сборный 1")
    if not ra or not rb or ra.get("already") or rb.get("already"):
        print("ПРОВАЛ: заворот не состоялся (%s / %s)" % (ra, rb)); return 1

    ia, ib = all_ids(a, []), all_ids(b, [])
    if ia != ib:
        bad += 1
        print("ПРОВАЛ: копии драфта получили РАЗНЫЕ id — движок не найдёт то, что мы записали в карту")
        for x, y in zip(ia, ib):
            if x != y:
                print("   root: %s\n   tl  : %s" % (x, y))
        if len(ia) != len(ib):
            print("   разное число id: %d против %d" % (len(ia), len(ib)))
    else:
        print("копии совпали: %d идентификаторов" % len(ia))

    # сегмент блюра — тот самый id, что уходит в ve_dof_map.txt
    def blur_seg_id(d):
        for m in d["materials"]["drafts"]:
            for t in m["draft"]["tracks"]:
                if t.get("type") == "effect":
                    return t["segments"][0]["id"]
        return None
    ba, bb = blur_seg_id(a), blur_seg_id(b)
    if not ba or ba != bb:
        bad += 1
        print("ПРОВАЛ: id сегмента блюра разошёлся (%s против %s)" % (ba, bb))
    elif ba != ids["blur_seg"]:
        bad += 1
        print("ПРОВАЛ: id блюра не из бандла (%s против %s)" % (ba, ids["blur_seg"]))
    else:
        print("id сегмента блюра совпал и взят из бандла: %s" % ba)

    # разные завороты обязаны давать РАЗНЫЕ id, иначе склеим два компаунда в один
    other = compound.make_ids()
    if other["blur_seg"] == ids["blur_seg"] or other["inner"] == ids["inner"]:
        bad += 1
        print("ПРОВАЛ: два разных заворота выдали одинаковые id")
    else:
        print("разные завороты дают разные id")

    print("ВСЁ ПРОШЛО" if not bad else "ПРОВАЛОВ: %d" % bad)
    return 1 if bad else 0


sys.exit(main())
