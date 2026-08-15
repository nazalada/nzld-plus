#!/usr/bin/env python3
"""Локальное UI-приложение трекера: таймлайн проекта как в CapCut + выбор/трек в браузере.
Запуск: .venv/bin/python app.py  (само откроет браузер)
"""
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import capcut_track as core
import dof

# Построчный вывод. Лаунчер перенаправляет stdout в ve_server.log, а при перенаправлении Python
# буферизует блоками по 8 КБ — лог оставался ПУСТЫМ, пока сервер не завершится. Дважды за день
# это стоило диагностики вслепую: создание камеры и подготовка глубины резкости молчали, хотя
# внутри всё логировалось. Ставим здесь, а не флагом -u в лаунчере: сервер поднимают и вручную.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):   # не TextIO (перехвачен тестом) — не наша забота
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

# Настройки панели NZLD+ (что выбрано в чекбоксах, показывались ли подсказки и т.п.).
# Лежат рядом с каналами плагина, а не в проекте: они пользовательские, а не проектные.
PREFS_PATH = os.path.expanduser("~/Movies/CapCut/User Data/plugin_prefs.json")


def _prefs_load():
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _prefs_save(d):
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, PREFS_PATH)   # атомарно: настройки не должны биться при обрыве


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # тихо

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, e, code=400):
        import sys
        import traceback
        traceback.print_exc(); sys.stderr.flush()   # полный трейсбэк в ve_server.log (Python буферизует — flush)
        self._send(code, {"ok": False, "error": str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/projects":
                self._send(200, core.list_projects())
            elif u.path == "/api/timeline":
                self._send(200, core.load_timeline(q["dir"]))
            elif u.path == "/api/frame":
                t = int(q["t"]) if q.get("t") else None
                jpeg, w, h = core.get_frame(q["dir"], q["seg"], t)
                self._send(200, jpeg, "image/jpeg", {"X-W": str(w), "X-H": str(h)})
            elif u.path == "/api/composite":
                jpeg, w, h = core.render_composite(q["dir"], int(q["t"]), exclude=q.get("exclude"))
                self._send(200, jpeg, "image/jpeg", {"X-W": str(w), "X-H": str(h)})
            elif u.path == "/api/layers":   # слои как отдельные текстуры — клиентский композит + живая камера
                self._send(200, core.layers_at(q["dir"], int(q["t"])))
            elif u.path == "/api/scene":   # сохранённый сетап проекта (слои+глубины, камера, DoF) для перезагрузки
                self._send(200, core.load_scene(q["dir"]) or {})
            elif u.path == "/api/cam":   # СУЩНОСТЬ КАМЕРЫ: дорожки проекта + камеры + их привязки
                import camera
                self._send(200, camera.state(q["dir"]))
            elif u.path == "/api/progress":   # живой прогресс трекинга (для веб-морды в реальном времени)
                pct, msg = 0, ""
                try:
                    with open(core.VE_PROGRESS_PATH) as f:
                        line = f.read().strip()
                    pct = int(line.split(" ", 1)[0]) if line else 0
                    msg = line.split(" ", 1)[1] if " " in line else ""
                except (OSError, ValueError):
                    pass
                self._send(200, {"pct": pct, "msg": msg})
            elif u.path == "/api/prefs":   # настройки панели, переживают перезапуск
                self._send(200, _prefs_load())
            elif u.path == "/api/trackkeys":   # сколько ключей трекинга уже на слоях (для «перезаписать?»)
                ids = [i for i in (q.get("ids") or "").split(",") if i]
                self._send(200, {"keys": core.count_track_keys(q["dir"], ids)})
            elif u.path == "/api/surfquad":   # куда пользователь растянул ручки выбора поверхности
                self._send(200, {"quad": core.surface_quad()})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 — вернуть ошибку в UI, не ронять сервер
            self._err(e)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/dbg":   # диагностика из QML (console.log CapCut глотает)
                with open("/tmp/ve_qml_dbg.log", "a", encoding="utf-8") as f:
                    f.write(str(body.get("m", "")) + "\n")
                self._send(200, {"ok": True})
            elif u.path == "/api/prefs":   # панель сохраняет свои настройки целиком
                _prefs_save(body)
                self._send(200, {"ok": True})
            elif u.path == "/api/cam/keepin":   # «не выходить за рамку» — флаг в шапке рига
                import camera
                self._send(200, camera.set_keepin(body["dir"], body.get("tok", ""), bool(body.get("on"))))
            elif u.path == "/api/reopen":   # переоткрыть проект (наклон плоскости оседает только на загрузке)
                core.capcut_ui.reopen(os.path.basename(body["dir"]))
                self._send(200, {"ok": True})
            elif u.path == "/api/cleartrack":   # снять ключи, наложенные трекингом
                res = core.clear_track_keys(body["dir"], body.get("targets") or [])
                if body.get("reopen", True):
                    try:
                        core.capcut_ui.reopen(os.path.basename(body["dir"]))
                    except Exception:  # noqa: BLE001 — ключи уже сняты, переоткрытие вторично
                        pass
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/surfpick":   # режим «выбрать поверхность по углам» в плеере
                self._send(200, {"ok": True, **core.surface_pick(bool(body.get("on")), body.get("quad"))})
            elif u.path == "/api/track":
                # Поверхность выбрана по углам -> рамки может не быть вовсе; её bbox выводим сами,
                # он нужен только как запасной прямоугольник для не-планарных путей.
                _q, _roi = body.get("quad"), body.get("roi")
                if not _roi and _q:
                    _xs, _ys = [p[0] for p in _q], [p[1] for p in _q]
                    _roi = (min(_xs), min(_ys), max(_xs) - min(_xs), max(_ys) - min(_ys))
                res = core.run_track(body["dir"], body["vid"], body.get("targets") or body.get("target") or [],
                                     tuple(_roi), start_us=body.get("start"),
                                     engine=body.get("engine", "ml"), props=body.get("props"),
                                     level=int(body.get("level", 3)), reopen=True,
                                     live=bool(body.get("live")), reframe=bool(body.get("reframe")),
                                     planar=bool(body.get("planar")), quad=body.get("quad"))
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/track3d":
                # 3D-ТРЕК: решение камеры → плоскость из облака → объект в её репере.
                # Контракт с панелью: quad — те же четыре ручки выбора поверхности (px канваса,
                # UL UR DL DR), obj — привязка объекта в ДОЛЯХ КВАДА: смещение x/y, высота z,
                # размер w/h. obj не прислали = сам квад, то есть натяжка на обведённое.
                # Тест параллакса стоит ВНУТРИ, до тяжёлого солва: клип без смещения камеры
                # получает отказ за секунды.
                res = core.run_track3d(body["dir"], body["vid"],
                                       body.get("targets") or body.get("target") or [],
                                       body["quad"], start_us=body.get("start"),
                                       live=bool(body.get("live", True)), reopen=True,
                                       obj=body.get("obj"))
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/preview3d/prepare":
                # Решение камеры ОДИН раз: дальше крутилки объекта живые.
                res = core.preview3d_prepare(body["dir"], body["vid"], body["target"],
                                             body["quad"], start_us=body.get("start"))
                self._send(200, res)
            elif u.path == "/api/preview3d/apply":
                # Крутилку подвинули: пересчитать углы на текущем кадре и отдать движку.
                # Ключи не пишутся — рисует живое поле углов, поэтому это не «применение».
                res = core.preview_apply(proj_dir=body.get("dir"), vid_id=body.get("vid"),
                                         target_id=body.get("target"), quad=body.get("quad"),
                                         obj=body.get("obj"), playhead_us=body.get("playhead"),
                                         key=body.get("key"))
                self._send(200, res)
            elif u.path == "/api/camera3d":
                res = core.run_camera3d(body["dir"], body["layers"], body["depths"],
                                        preset=body.get("preset", "parallax_pan"),
                                        intensity=float(body.get("intensity", 0.6)),
                                        want_scale=bool(body.get("scale", True)),
                                        live=bool(body.get("live")), reopen=True,
                                        campath=body.get("campath"),
                                        make_layer=bool(body.get("make_layer")),
                                        dof=bool(body.get("dof")),
                                        aperture=float(body.get("aperture", 0.3)),
                                        focus=float(body.get("focus", 1.96)),
                                        scene=body.get("scene"), paths=body.get("paths"),
                                        starts=body.get("starts"))
                self._send(200, {"ok": True, **res})
            elif u.path.startswith("/api/cam/"):   # СУЩНОСТЬ КАМЕРЫ: создать / привязать дорожку / DoF / удалить
                import camera
                act = u.path[len("/api/cam/"):]
                if act == "create":
                    res = camera.create(body["dir"])
                elif act == "bind":   # depth=None -> отвязать; seg -> «нитка» (дорожку резолвим по клипу)
                    res = camera.bind(body["dir"], body["tok"], body.get("track"),
                                      body.get("depth"), seg_id=body.get("seg"))
                elif act == "preset":   # готовое движение одним нажатием
                    res = camera.preset(body["dir"], body["tok"], body["name"],
                                        float(body.get("intensity", 0.6)))
                elif act == "key":      # поставить/обновить ключ позы на плейхеде
                    res = camera.set_key(body["dir"], body["tok"], int(body["t"]), body.get("pose") or {})
                elif act == "delkey":
                    res = camera.del_key(body["dir"], body["tok"], int(body["t"]))
                elif act == "dof":
                    res = camera.set_dof(body["dir"], body["tok"], on=body.get("on"),
                                         focus=body.get("focus"), aperture=body.get("aperture"))
                elif act == "dofprep":   # сборные клипы + слой-контроллер + карты: без них галочка DoF ничего не делает
                    res = camera.prep_dof(body["dir"], body["tok"],
                                          focus=float(body.get("focus", 1.96)),
                                          aperture=float(body.get("aperture", 0.3)))
                    res.update(camera.dof_ready(body["dir"]))
                elif act == "pan":    # поворот камеры: панорама становится облётом сцены
                    res = camera.set_pan(body["dir"], body["tok"], float(body.get("gain", 0)))
                elif act == "pivot":  # глубина оси облёта
                    res = camera.set_pivot(body["dir"], body["tok"], float(body.get("depth", 0.5)))
                elif act == "roll":   # завал горизонта всей сцены (градусы)
                    res = camera.set_roll(body["dir"], body["tok"], float(body.get("deg", 0)))
                elif act == "tilt":   # наклон плоскости дорожки в 3D
                    res = camera.set_tilt(body["dir"], body["tok"], body["track"],
                                          body.get("tiltx", 0), body.get("tilty", 0))
                elif act == "restore":   # вернуть удалённый клип камеры (привязки живы в сайдкаре)
                    res = camera.restore(body["dir"], body["tok"])
                elif act == "delete":
                    res = camera.delete(body["dir"], body["tok"])
                else:
                    return self._send(404, {"error": "not found"})
                self._send(200, {"ok": True, **(res if isinstance(res, dict) else {"res": res})})
            elif u.path == "/api/restore_cam":   # откат вживления слоя-камеры (.precam.bak)
                n = core.restore_camera_layer(body["dir"])
                self._send(200, {"ok": True, "restored": n})
            elif u.path == "/api/commit_cam":   # зафиксировать ключи слоя-камеры в реальные ключи объектов
                res = core.commit_camera(body["dir"], body["layers"], body["depths"],
                                         want_scale=bool(body.get("scale", True)))
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/add_object":   # инкрементально добавить объект(ы) в сцену без перезапекания
                res = core.run_add_object(body["dir"], body["additions"],
                                          focus=float(body.get("focus", 1.96)),
                                          aperture=float(body.get("aperture", 0.3)),
                                          scene=body.get("scene"))
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/split_scenes":   # разбить дубли одной сцены на таймлайне на независимые сцены
                res = core.split_scenes(body["dir"])
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/dof":   # глубина резкости: слой-контроллер + карты + тумблер + реопен
                res = dof.run_dof(body["dir"], focus=float(body.get("focus", 1.96)),
                                  aperture=float(body.get("aperture", 0.3)),
                                  on=bool(body.get("on", True)))
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/roto":
                res = core.run_roto(body["dir"], body["seg"], body["points"], body["labels"],
                                    start_us=body.get("start"))
                self._send(200, {"ok": True, **res})
            elif u.path == "/api/roto_preview":
                png, w, h = core.run_roto_preview(body["dir"], body["seg"], int(body["t"]),
                                                  body["points"], body["labels"])
                self._send(200, png, "image/png", {"X-W": str(w), "X-H": str(h)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._err(e, 500)


def open_browser(url):
    if os.environ.get("BROWSER") == "none":
        return
    # окно-приложение без вкладок/адресной строки, если есть Chromium-браузер
    for app in ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium"):
        if os.path.isdir(f"/Applications/{app}.app"):
            subprocess.Popen(["open", "-na", app, "--args", f"--app={url}"])
            return
    webbrowser.open(url)


def main():
    port = int(os.environ.get("PORT", 8756))
    url = f"http://localhost:{port}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:  # порт занят — значит уже запущен, просто открываем окно
        print(f"уже запущен → {url}")
        open_browser(url)
        return
    # Сброс прогресса на старте. Раньше это делал только CapCut Probe.command, поэтому сервер,
    # убитый ПОСРЕДИ операции (краш, kill, перезапуск руками), оставлял в файле <100 — и дилиб
    # рисовал зависшую плашку до следующего полного запуска. Теперь её снимает любой старт сервера.
    try:
        with open(core.VE_PROGRESS_PATH, "w", encoding="utf-8") as f:
            f.write("100 Готово")
    except OSError:
        pass
    print(f"CapCut Tracker → {url}\n(закрой это окно Терминала, чтобы остановить)")
    threading.Timer(0.6, lambda: open_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nвыход")


if __name__ == "__main__":
    main()
