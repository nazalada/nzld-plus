#!/usr/bin/env python3
"""Отрисовка примера работы 3D-камеры NZLD+ — теми же формулами, что дилиб отдаёт движку.

ЗАЧЕМ ОТДЕЛЬНЫЙ РЕНДЕР, а не запись экрана: снимок экрана требует системного разрешения на
запись экрана, и кадры выходят с UI поверх. Здесь мы читаем ровно те же исходники, что и
плагин — риг (глубины, наклоны) и путь камеры из драфта — и считаем ту же проекцию:
центр слоя по формуле параллакса, четыре угла — по формуле наклона (layer_corners в ve_hook.cpp),
включая нормировку площади и «не выходить за рамку». Поэтому картинка совпадает с плеером.

Запуск:  .venv/bin/python demo_render.py "<проект>"
"""
import glob
import json
import math
import os
import sys

import cv2
import numpy as np

import camera3d
import capcut_track as ct

Z_REF = camera3d.Z_REF
FOCAL_FRAC = camera3d.FOCAL_FRAC
SIGN_Y = camera3d.SIGN_Y


def read_rig(path=None):
    """Риг -> (канвас, слои). Слой: id, z, bx, by, base_scale, base_rot, tiltx, tilty, path."""
    cw = ch = 0
    layers, tilts = [], {}
    pan_gain = 0.0
    with open(path or ct.VE_RIG_PATH, encoding="utf-8") as f:
        for line in f:
            t = line.split()
            if not t:
                continue
            if t[0] == "CANVAS":
                cw, ch = float(t[1]), float(t[2])
            elif t[0] == "LAYER":
                layers.append({"id": t[1], "z": float(t[2]), "bx": float(t[3]), "by": float(t[4]),
                               "base_scale": float(t[5]), "base_rot": float(t[6]),
                               "path": line.split(None, 10)[10].strip()})
            elif t[0] == "TILT":
                tilts[t[1]] = (float(t[2]), float(t[3]))
            elif t[0] == "PAN" and int(t[1]):
                pan_gain = float(t[2])
    for L in layers:
        L["tiltx"], L["tilty"] = tilts.get(L["id"], (0.0, 0.0))
    return (cw, ch), layers, pan_gain


def camera_path(proj_dir, n):
    """Поза камеры в n равных моментах: (cx, cy, cz, roll). Ключи берём из слоя-камеры драфта —
    того самого, который пользователь двигает руками в плеере."""
    draft = ct.read_draft(proj_dir)
    cw, ch = draft["canvas_config"]["width"], draft["canvas_config"]["height"]
    seg = next((s for t in draft["tracks"] if ct._is_cam_track(t.get("name"))
                for s in (t.get("segments") or [])), None)
    if not seg:
        raise SystemExit("в проекте нет слоя-камеры")
    dur = seg["target_timerange"]["duration"]
    curves = {c["property_type"]: [(k["time_offset"], k["values"][0]) for k in c["keyframe_list"]]
              for c in (seg.get("common_keyframes") or [])}

    def at(prop, default):
        pts = sorted(curves.get(prop) or [])
        if not pts:
            return lambda _t: default
        def f(t):
            if t <= pts[0][0]:
                return pts[0][1]
            if t >= pts[-1][0]:
                return pts[-1][1]
            for i in range(1, len(pts)):
                if t <= pts[i][0]:
                    (t0, v0), (t1, v1) = pts[i - 1], pts[i]
                    u = (t - t0) / (t1 - t0) if t1 > t0 else 0
                    return v0 + (v1 - v0) * u
            return pts[-1][1]
        return f

    fpx, fpy, fsx = at("KFTypePositionX", 0.0), at("KFTypePositionY", 0.0), at("KFTypeScaleX", 1.0)
    f, hw, hh = FOCAL_FRAC * ch, cw / 2.0, ch / 2.0
    return [(fpx(dur * i / (n - 1)), fpy(dur * i / (n - 1)), max(0.05, fsx(dur * i / (n - 1))))
            for i in range(n)], (cw, ch, f, hw, hh)


def corners(L, cx, cy, cz, f, hw, hh, keepin, yaw=0.0, pitch=0.0):
    """Четыре угла слоя в NDC. Порядок UL, UR, LL, LR — как kCP в ve_hook.cpp.
    Это построчный порт layer_corners(): держать формулы в одном виде важнее краткости."""
    z, bs, bx, by = L["z"], L["base_scale"], L["bx"], L["by"]
    ax, ay = math.radians(L["tiltx"]), math.radians(L["tilty"])
    br = math.radians(L["base_rot"])
    hwl, hhl = hw * bs, hh * bs
    cyw, syw = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cpt, spt = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    if not (ax or ay) and not (yaw or pitch):
        # БИЛЛБОРД — считаем ровно как drive_layers: центр + масштаб, и «не выходить за рамку»
        # зажимает ПОЗИЦИЮ (у наклонённого слоя вместо зажима работает дорастание, см. ниже).
        d = max(1e-3, z - cz)
        vx = (bx * z - f * cx) / d / hw
        vy = ((by * z - f * cy) / d) / hh
        vs = bs * (z / d)
        if keepin:
            half = vs * 0.5
            lim = (half - 0.5) * 2.0 if half > 0.5 else 0.0
            vx = max(-lim, min(lim, vx))
            vy = max(-lim, min(lim, vy))
        pts = [(-vs, -vs), (vs, -vs), (-vs, vs), (vs, vs)]   # UL, UR, LL, LR (Y вниз)
        if br:
            pts = [(x * math.cos(br) - y * math.sin(br), x * math.sin(br) + y * math.cos(br))
                   for x, y in pts]
        return [[vx + x, vy + y] for x, y in pts]
    out = []
    for sx4, sy4 in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        u, v = sx4 * hwl, sy4 * hhl
        if br:
            u, v = u * math.cos(br) - v * math.sin(br), u * math.sin(br) + v * math.cos(br)
        wu, wv = u * math.cos(ay), v * math.cos(ax)
        # мировая точка угла -> система камеры (сдвиг, затем поворот) -> перспектива.
        # Y ВНИЗ, без SIGN_Y: у corner-pin своя система координат (см. ve_hook.cpp/layer_corners)
        Xw, Yw = (bx + wu) * z / f, (by + wv) * z / f
        Zw = z + (u * math.sin(ay) + v * math.sin(ax)) * z / f
        p1x, p1y, p1z = Xw - cx, Yw - cy, Zw - cz
        p2x, p2z = cyw * p1x - syw * p1z, syw * p1x + cyw * p1z
        p3y, p3z = cpt * p1y - spt * p2z, spt * p1y + cpt * p2z
        p3z = max(1e-3, p3z)
        out.append([(f * p2x / p3z) / hw, (f * p3y / p3z) / hh])
    dc = max(1e-3, z - cz)
    ccx, ccy = ((bx * z - f * cx) / dc) / hw, ((by * z - f * cy) / dc) / hh
    if yaw or pitch:
        # поворот камеры нормировать по площади нельзя: изменение размера при развороте — это
        # и есть эффект. Остаётся только «не выходить за рамку».
        return grow_to_frame(out, ccx, ccy) if keepin else out
    # нормировка площади: наклон меняет форму, а не размер (иначе ближний край подъезжает и слой пухнет)
    sc = z / dc
    a_flat = (2 * hwl * sc / hw) * (2 * hhl * sc / hh)
    order = [0, 1, 3, 2]
    def area(q):
        s = sum(q[order[k]][0] * q[order[(k + 1) % 4]][1] - q[order[(k + 1) % 4]][0] * q[order[k]][1]
                for k in range(4))
        return abs(s) * 0.5
    a_tilt = area(out)
    if a_tilt < 1e-9:
        return out
    kk = math.sqrt(a_flat / a_tilt)
    out = [[ccx + (p[0] - ccx) * kk, ccy + (p[1] - ccy) * kk] for p in out]
    return grow_to_frame(out, ccx, ccy) if keepin else out


def grow_to_frame(out, ccx, ccy):
    """Дорастить четырёхугольник вокруг центра, пока все четыре угла кадра не окажутся внутри."""
    order = [0, 1, 3, 2]
    need = 1.0
    for px, py in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        rx, ry = px - ccx, py - ccy
        if math.hypot(rx, ry) < 1e-9:
            continue
        best = 0.0
        for e in range(4):
            a, b = out[order[e]], out[order[(e + 1) % 4]]
            ax2, ay2 = a[0] - ccx, a[1] - ccy
            bx2, by2 = b[0] - ccx, b[1] - ccy
            den = rx * (ay2 - by2) - ry * (ax2 - bx2)
            if abs(den) < 1e-12:
                continue
            t = (ax2 * (ay2 - by2) - ay2 * (ax2 - bx2)) / den
            u = (rx * ay2 - ry * ax2) / den
            if t > 0 and -1e-9 <= u <= 1 + 1e-9:
                best = max(best, t)
        if best > 1e-9:
            need = max(need, 1.0 / best)
    if need > 1.0:
        out = [[ccx + (p[0] - ccx) * need, ccy + (p[1] - ccy) * need] for p in out]
    return out


def render(proj_dir, nframes=48, out_dir=None, keepin=None, no_tilt=False, save=True):
    (cw0, ch0), layers, pan_gain = read_rig()
    if not layers:
        raise SystemExit("риг пуст — привяжи дорожки к камере")
    if no_tilt:   # тот же кадр, но без перспективы — для сравнения «до/после»
        layers = [dict(L, tiltx=0.0, tilty=0.0) for L in layers]
        pan_gain = 0.0
    poses, (cw, ch, f, hw, hh) = camera_path(proj_dir, nframes)
    if keepin is None:
        keepin = "KEEPIN 1" in open(ct.VE_RIG_PATH, encoding="utf-8").read()
    imgs = {}
    for L in layers:
        im = cv2.imread(L["path"], cv2.IMREAD_UNCHANGED)
        if im is None:
            raise SystemExit(f"не читается слой: {L['path']}")
        if im.shape[2] == 3:
            im = cv2.cvtColor(im, cv2.COLOR_BGR2BGRA)
        imgs[L["id"]] = im
    z_min = min(L["z"] for L in layers)
    pivot = (z_min + max(L["z"] for L in layers)) * 0.5   # точка облёта — середина сцены
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_out")
    os.makedirs(out_dir, exist_ok=True)
    W, H = int(cw), int(ch)
    frames = []
    for i, (pxv, pyv, sxv) in enumerate(poses):
        if pan_gain > 0:   # ОБЛЁТ: панорама = разворот камеры вокруг точки в глубине сцены
            yaw, pitch = -pxv * pan_gain, pyv * pan_gain
            ty, tp = math.radians(yaw), math.radians(pitch)
            cx = -pivot * math.sin(ty) * math.cos(tp)
            cy = pivot * math.sin(tp)
            cz = pivot * (1.0 - math.cos(ty) * math.cos(tp))
        else:
            yaw = pitch = 0.0
            cx = -pxv * Z_REF * hw / f
            cy = pyv * Z_REF * hh / f
            cz = 0.0
        cz += z_min * (1.0 - 1.0 / sxv)         # наезд асимптотит к ближнему слою
        canvas = np.zeros((H, W, 3), np.uint8)
        for L in sorted(layers, key=lambda l: -l["z"]):   # дальние сначала
            q = corners(L, cx, cy, cz, f, hw, hh, keepin, yaw, pitch)
            dst = np.float32([[(x + 1) * 0.5 * W, (y + 1) * 0.5 * H] for x, y in q])
            im = imgs[L["id"]]
            sh, sw = im.shape[:2]
            src = np.float32([[0, 0], [sw, 0], [0, sh], [sw, sh]])
            warp = cv2.warpPerspective(im, cv2.getPerspectiveTransform(src, dst), (W, H),
                                       flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            a = (warp[:, :, 3:4].astype(np.float32) / 255.0)
            canvas = (canvas * (1 - a) + warp[:, :, :3] * a).astype(np.uint8)
        frames.append(canvas)
        if save:
            cv2.imwrite(os.path.join(out_dir, f"frame_{i:03d}.png"), canvas)
    return frames, out_dir, (W, H)


def label(img, text, sub=""):
    """Подпись на кадре: плашка + текст. Латиницей — cv2.putText кириллицу не умеет."""
    h, w = img.shape[:2]
    bar = int(h * 0.085)
    ov = img.copy()
    cv2.rectangle(ov, (0, 0), (w, bar), (18, 14, 10), -1)
    img = cv2.addWeighted(ov, 0.72, img, 0.28, 0)
    cv2.putText(img, text, (int(w * 0.02), int(bar * 0.68)), cv2.FONT_HERSHEY_DUPLEX,
                h / 620.0, (205, 193, 0), max(1, h // 500), cv2.LINE_AA)
    if sub:
        cv2.putText(img, sub, (int(w * 0.02) + int(w * 0.30), int(bar * 0.68)),
                    cv2.FONT_HERSHEY_DUPLEX, h / 900.0, (225, 225, 225), max(1, h // 700), cv2.LINE_AA)
    return img


def main():
    proj = sys.argv[1] if len(sys.argv) > 1 else "3D CAMERA"
    proj_dir = next((p["dir"] for p in ct.list_projects() if p["name"] == proj), None)
    if not proj_dir:
        raise SystemExit(f"проект «{proj}» не найден")
    frames, out_dir, (W, H) = render(proj_dir)
    # ролик
    mp4 = os.path.join(out_dir, "nzld_camera_demo.mp4")
    vw = cv2.VideoWriter(mp4, cv2.VideoWriter_fourcc(*"avc1"), 24, (W, H))
    for fr in frames:
        vw.write(fr)
    vw.release()
    # раскадровка: 4 ключевых момента подряд
    picks = [0, len(frames) // 3, 2 * len(frames) // 3, len(frames) - 1]
    tw = 640
    tiles = [cv2.resize(frames[i], (tw, int(tw * H / W))) for i in picks]
    sheet = np.vstack([np.hstack(tiles[:2]), np.hstack(tiles[2:])])
    png = os.path.join(out_dir, "nzld_camera_demo.png")
    cv2.imwrite(png, sheet)
    # сравнение: тот же момент без наклона плоскости и с ним
    flat, _, _ = render(proj_dir, nframes=len(frames), no_tilt=True, save=False)
    k = len(frames) // 3
    cw2 = 900
    a = label(cv2.resize(flat[k], (cw2, int(cw2 * H / W))), "BILLBOARD", "plane faces the camera")
    b = label(cv2.resize(frames[k], (cw2, int(cw2 * H / W))), "TILTED PLANE", "far edge converges")
    cmp_png = os.path.join(out_dir, "nzld_perspective_compare.png")
    cv2.imwrite(cmp_png, np.hstack([a, b]))
    print(f"кадров: {len(frames)}  ->  {out_dir}")
    print(f"ролик:       {mp4}")
    print(f"раскадровка: {png}")
    print(f"сравнение:   {cmp_png}")


if __name__ == "__main__":
    main()
