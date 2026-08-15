"""Roto Brush через SAM 2 (Meta) — тычешь по объекту точками (зелёная=добавить,
красная=убрать), живой предпросмотр маски, потом точный вырез по всему клипу.
Локально на MPS. Вывод — ProRes 4444 .mov с альфой (CapCut его читает).

ponytail: маска SAM2 бинарная -> заполняем дыры + растушёвываем край для мягкой альфы."""
import glob
import os
import shutil
import subprocess
import tempfile

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # часть операций SAM2 не на MPS

FFMPEG = shutil.which("ffmpeg")
CKPT = os.path.expanduser("~/.cache/sam2/sam2.1_hiera_tiny.pt")
CFG = "configs/sam2.1/sam2.1_hiera_t.yaml"
PROC_H = 1024  # рабочая высота (SAM2 нативно ~1024)
_VID_PRED = None
_IMG_PRED = None
_last_img_key = None


def available():
    try:
        import sam2  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return FFMPEG is not None and os.path.exists(CKPT)


def _device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _video_predictor():
    global _VID_PRED
    if _VID_PRED is None:
        from sam2.build_sam import build_sam2_video_predictor
        _VID_PRED = build_sam2_video_predictor(CFG, CKPT, device=_device())
    return _VID_PRED


def _image_predictor():
    global _IMG_PRED
    if _IMG_PRED is None:
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        _IMG_PRED = SAM2ImagePredictor(build_sam2(CFG, CKPT, device=_device()))
    return _IMG_PRED


def _read_scaled(cv2, src_path, src_time_us):
    """Кадр видео в рабочей высоте PROC_H. -> (rgb, pw, ph, sc) где sc = px_исходника->px_кадра."""
    cap = cv2.VideoCapture(src_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, src_time_us / 1000)
    ok, fr = cap.read()
    cap.release()
    if not ok:
        raise ValueError("не смог прочитать кадр")
    H, W = fr.shape[:2]
    sc = PROC_H / H
    pw, ph = int(round(W * sc)), PROC_H
    return cv2.cvtColor(cv2.resize(fr, (pw, ph)), cv2.COLOR_BGR2RGB), pw, ph, sc


def preview_mask(src_path, src_time_us, points_src, labels):
    """Быстрая маска одного кадра по точкам (SAM2 image predictor). points_src в px исходника,
    labels: 1=добавить, 0=убрать. -> (mask bool [ph,pw], pw, ph)."""
    import cv2
    import numpy as np
    import torch

    global _last_img_key
    rgb, pw, ph, sc = _read_scaled(cv2, src_path, src_time_us)
    pred = _image_predictor()
    key = (src_path, round(src_time_us / 1000))
    with torch.inference_mode():
        if key != _last_img_key:
            pred.set_image(rgb)
            _last_img_key = key
        pc = np.array([[p[0] * sc, p[1] * sc] for p in points_src], np.float32)
        pl = np.array(labels, np.int32)
        masks, scores, _ = pred.predict(point_coords=pc, point_labels=pl, multimask_output=False)
    return masks[0].astype(bool), pw, ph


def _clean_alpha(cv2, np, mask, ow, oh):
    """Бинарная маска (любого размера) -> альфа (ow x oh): апскейл + заполнить дыры + мягкий край."""
    mu = cv2.resize((mask.astype(np.uint8) * 255), (ow, oh), interpolation=cv2.INTER_LINEAR)
    _, mu = cv2.threshold(mu, 127, 255, cv2.THRESH_BINARY)
    cnts, _ = cv2.findContours(mu, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    al = np.zeros_like(mu)
    cv2.drawContours(al, cnts, -1, 255, -1)
    f = max(1.0, oh / 640)  # растушёвка пропорционально разрешению
    return cv2.GaussianBlur(al, (0, 0), f)


def _extract(src_path, t0_us, t1_us, fps, out_dir, height=None, q=3):
    vf = f"fps={fps:.6f}" + (f",scale=-2:{height}" if height else "")
    subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{t0_us / 1e6:.4f}", "-t", f"{(t1_us - t0_us) / 1e6:.4f}",
         "-i", src_path, "-vf", vf, "-q:v", str(q), "-start_number", "0",
         os.path.join(out_dir, "%05d.jpg")], check=True)
    return sorted(glob.glob(os.path.join(out_dir, "*.jpg")))


def remove_background(src_path, t0_us, t1_us, points_src, labels, out_path,
                     query_us=None, out_fps=30, log=print):
    """SAM2 video: точки/мазки на кадре query_us -> маска по всему [t0,t1] в обе стороны.
    Маска считается на рабочем размере (PROC_H, память), а вырез — в НАТИВНОМ разрешении
    исходника (маска апскейлится). -> ProRes 4444 .mov с альфой и звуком исходника."""
    import cv2
    import numpy as np
    import torch

    cap = cv2.VideoCapture(src_path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    src_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
    cap.release()
    step = max(1, round(src_fps / out_fps))
    fps = src_fps / step
    tt = (f"{t0_us / 1e6:.4f}", f"{(t1_us - t0_us) / 1e6:.4f}")

    fs = tempfile.mkdtemp(prefix="rotos_")  # кадры для SAM2 (рабочий размер)
    fo = tempfile.mkdtemp(prefix="rotoo_")  # кадры для вывода (нативный размер)
    try:
        fsam = _extract(src_path, t0_us, t1_us, fps, fs, height=PROC_H)
        fout = _extract(src_path, t0_us, t1_us, fps, fo, height=None, q=2)  # нативное
        if not fsam or not fout:
            raise ValueError("не удалось извлечь кадры")
        ph, pw = cv2.imread(fsam[0]).shape[:2]
        sc = pw / src_w if src_w else 1.0
        pc = np.array([[p[0] * sc, p[1] * sc] for p in points_src], np.float32)
        pl = np.array(labels, np.int32)
        qf = 0 if query_us is None else max(0, min(len(fsam) - 1, round((query_us - t0_us) / 1e6 * fps)))

        pred, dev = _video_predictor(), _device()
        log(f"Roto (SAM 2) на {dev}, {len(fsam)} кадров...")
        masks = {}
        with torch.inference_mode():
            state = pred.init_state(fs, offload_video_to_cpu=True)
            pred.add_new_points_or_box(state, frame_idx=qf, obj_id=1, points=pc, labels=pl)
            for fi, _o, ml in pred.propagate_in_video(state):
                masks[fi] = (ml[0, 0] > 0).cpu().numpy()
            if qf > 0:
                for fi, _o, ml in pred.propagate_in_video(state, reverse=True):
                    masks[fi] = (ml[0, 0] > 0).cpu().numpy()

        oh0, ow0 = cv2.imread(fout[0]).shape[:2]
        ow, oh = ow0 // 2 * 2, oh0 // 2 * 2
        ff = subprocess.Popen(
            [FFMPEG, "-y",
             "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{ow}x{oh}", "-r", f"{fps}", "-i", "-",
             "-ss", tt[0], "-t", tt[1], "-i", src_path,
             "-map", "0:v:0", "-map", "1:a:0?",  # видео из пайпа, аудио из исходника
             "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
             "-c:a", "pcm_s16le", "-shortest", out_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        n = 0
        for i, fp in enumerate(fout):
            m = masks.get(i)
            rgb = cv2.cvtColor(cv2.imread(fp)[:oh, :ow], cv2.COLOR_BGR2RGB)
            al = np.zeros((oh, ow), np.uint8) if m is None else _clean_alpha(cv2, np, m, ow, oh)
            ff.stdin.write(np.dstack([rgb, al]).tobytes())
            n += 1
        ff.stdin.close()
        err = ff.stderr.read().decode(errors="ignore")
        ff.wait()
        if ff.returncode != 0 or n == 0:
            raise ValueError(f"кодирование не удалось: {err[-300:]}")
        return {"frames": n, "out": out_path, "size": [ow, oh], "fps": round(fps, 3),
                "mb": round(os.path.getsize(out_path) / 1e6, 1)}
    finally:
        shutil.rmtree(fs, ignore_errors=True)
        shutil.rmtree(fo, ignore_errors=True)
