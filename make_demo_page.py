#!/usr/bin/env python3
"""Собирает автономную страницу-пример (картинки внутри как data-URI)."""
import base64
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo_out", "nzld_camera_demo.html")


def uri(name, mime):
    with open(os.path.join(HERE, "demo_out", name), "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


GIF = uri("nzld_camera_demo.gif", "image/gif")
CMP = uri("compare_web.jpg", "image/jpeg")

HTML = """<title>NZLD+ · поворот 3D-камеры</title>
<style>
  :root{
    --ink:#0D1116; --panel:#151B22; --line:#253038;
    --text:#D8E0E7; --muted:#7F8E9B; --teal:#00C1CD;
    --near:#09BAC7; --mid:#1CA9B9; --far:#3693A7;
    --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: light){
    :root{ --ink:#EDF1F4; --panel:#FFFFFF; --line:#D2DBE2;
           --text:#111A21; --muted:#57666F; }
  }
  :root[data-theme="dark"]{ --ink:#0D1116; --panel:#151B22; --line:#253038;
                            --text:#D8E0E7; --muted:#7F8E9B; }
  :root[data-theme="light"]{ --ink:#EDF1F4; --panel:#FFFFFF; --line:#D2DBE2;
                             --text:#111A21; --muted:#57666F; }

  body{ background:var(--ink); color:var(--text); font-family:var(--sans);
        font-size:16px; line-height:1.65; margin:0; padding:0 20px 96px; }
  .wrap{ max-width:940px; margin:0 auto; }
  .col{ max-width:64ch; }

  .eyebrow{ font-family:var(--mono); font-size:11px; letter-spacing:.18em;
            text-transform:uppercase; color:var(--teal); margin:0 0 10px; }
  h1{ font-family:var(--mono); font-weight:600; font-size:clamp(26px,4.6vw,40px);
      letter-spacing:-.01em; line-height:1.15; text-wrap:balance; margin:0 0 14px; }
  h2{ font-family:var(--mono); font-weight:600; font-size:17px; letter-spacing:.02em;
      text-wrap:balance; margin:0 0 12px; }
  p{ margin:0 0 14px; }
  .lede{ font-size:18px; color:var(--muted); }
  .muted{ color:var(--muted); }
  b{ font-weight:600; color:var(--text); }

  header{ padding:72px 0 40px; border-bottom:1px solid var(--line); }
  section{ padding:44px 0; border-bottom:1px solid var(--line); }
  section:last-of-type{ border-bottom:0; }
  .stack{ display:flex; flex-direction:column; gap:22px; }

  figure{ margin:0; display:flex; flex-direction:column; gap:10px; }
  figure img{ display:block; width:100%; height:auto; border-radius:2px;
              border:1px solid var(--line); background:#000; }
  figcaption{ font-family:var(--mono); font-size:12px; color:var(--muted); line-height:1.5; }

  table{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
  th,td{ text-align:left; padding:9px 12px 9px 0; border-bottom:1px solid var(--line);
         font-size:14px; vertical-align:baseline; }
  th{ font-family:var(--mono); font-size:11px; letter-spacing:.14em; text-transform:uppercase;
      color:var(--muted); font-weight:500; border-bottom-color:var(--teal); }
  td.n{ font-family:var(--mono); white-space:nowrap; }
  .scroll{ overflow-x:auto; }
  .chip{ display:inline-block; width:34px; height:8px; border-radius:1px;
         vertical-align:middle; margin-right:9px; }

  .diagram{ background:var(--panel); border:1px solid var(--line); border-radius:2px;
            padding:20px 18px 14px; }
  .diagram svg{ display:block; width:100%; height:auto; }

  .note{ border-left:2px solid var(--teal); padding:2px 0 2px 16px; }
  code{ font-family:var(--mono); font-size:13.5px; background:var(--panel);
        border:1px solid var(--line); border-radius:2px; padding:1px 5px; }
  pre{ font-family:var(--mono); font-size:13px; line-height:1.6; background:var(--panel);
       border:1px solid var(--line); border-radius:2px; padding:14px 16px; margin:0;
       overflow-x:auto; }
  ul{ margin:0; padding-left:20px; }
  li{ margin-bottom:8px; }
</style>

<div class="wrap">
<header>
  <p class="eyebrow">NZLD+ · плагин для CapCut</p>
  <h1>Камера поворачивается — сцена оживает</h1>
  <p class="lede col">Три плоских слоя разложены по глубине. Камера не просто едет вбок, а
  <b>разворачивается</b> вокруг точки в глубине сцены — и все слои сразу ловят свою перспективу.</p>
</header>

<section>
  <p class="eyebrow">Сцена</p>
  <h2>Что стоит в кадре</h2>
  <div class="stack">
    <div class="scroll">
      <table>
        <tr><th>Дорожка</th><th>Глубина</th><th>Роль</th></tr>
        <tr>
          <td><span class="chip" style="background:var(--near)"></span>ComfyUI_00002</td>
          <td class="n">0.10</td><td class="muted">ложка, чашка, сахар — ближний план</td>
        </tr>
        <tr>
          <td><span class="chip" style="background:var(--mid)"></span>ComfyUI_00001</td>
          <td class="n">0.32</td><td class="muted">средний план</td>
        </tr>
        <tr>
          <td><span class="chip" style="background:var(--far)"></span>Copy_of_Sugar…</td>
          <td class="n">0.62</td><td class="muted">фон: мониторы, кресло, персонаж</td>
        </tr>
      </table>
    </div>
    <div class="diagram">
      <svg viewBox="0 0 820 200" role="img" aria-label="Вид сцены сбоку: камера слева ходит по дуге вокруг точки в середине сцены, три плоскости по глубине">
        <line x1="96" y1="100" x2="800" y2="100" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 5"/>
        <path d="M40 84 h34 v32 h-34 z M74 92 l20 -10 v36 l-20 -10 z"
              fill="none" stroke="var(--teal)" stroke-width="2" stroke-linejoin="round"/>
        <text x="40" y="140" fill="var(--muted)" font-family="var(--mono)" font-size="11" letter-spacing="1.4">КАМЕРА</text>
        <rect x="228" y="34" width="9" height="132" rx="1" fill="var(--near)"/>
        <rect x="404" y="42" width="9" height="116" rx="1" fill="var(--mid)"/>
        <rect x="655" y="26" width="9" height="148" rx="1" fill="var(--far)"/>
        <circle cx="446" cy="100" r="4" fill="var(--teal)"/>
        <text x="404" y="86" fill="var(--teal)" font-family="var(--mono)" font-size="10" letter-spacing="1.2">ОСЬ ОБЛЁТА</text>
        <path d="M62 46 A 420 420 0 0 1 62 154" fill="none" stroke="var(--teal)" stroke-width="1.5"
              stroke-dasharray="4 4" opacity="0.75"/>
        <path d="M56 52 l6 -8 l7 7" fill="none" stroke="var(--teal)" stroke-width="1.5" stroke-linecap="round"/>
        <path d="M56 148 l6 8 l7 -7" fill="none" stroke="var(--teal)" stroke-width="1.5" stroke-linecap="round"/>
        <text x="214" y="186" fill="var(--muted)" font-family="var(--mono)" font-size="11">0.10</text>
        <text x="390" y="186" fill="var(--muted)" font-family="var(--mono)" font-size="11">0.32</text>
        <text x="646" y="186" fill="var(--muted)" font-family="var(--mono)" font-size="11">0.62</text>
        <text x="742" y="94" fill="var(--muted)" font-family="var(--mono)" font-size="11" letter-spacing="1.4">ДАЛЬШЕ</text>
      </svg>
    </div>
    <p class="col muted">Вид сбоку — та же схема, что рисует панель плагина. Камера разворачивается
    вокруг середины сцены: слои на этой глубине почти стоят, а всё ближе и дальше расходится в стороны.
    Цвет полосы кодирует глубину — ближний план держит фирменную бирюзу, дальний уходит в сине-серый.</p>
  </div>
</section>

<section>
  <p class="eyebrow">Движение</p>
  <h2>Один разворот — искажаются все объекты</h2>
  <div class="stack">
    <figure>
      <img src="__GIF__" alt="Камера панорамирует слева направо с наездом; слои разъезжаются с разной скоростью">
      <figcaption>Разворот ±30° на полный ход панорамы, наезд ×1.22, 5 секунд.
      Мониторы за спиной персонажа поворачиваются вместе с камерой, ложка и чашка уходят вперёд —
      каждый слой искажается по своей глубине.</figcaption>
    </figure>
    <p class="col">Движение камеры пользователь ставит родными средствами CapCut: выделяет клип камеры,
    двигает его в плеере и ставит ключи привычным ромбом. Никакого отдельного контрола для поворота
    заводить не пришлось — в панели включается «Поворот камеры», и та же панорама начинает читаться
    как разворот. Сила задаётся в градусах.</p>
  </div>
</section>

<section>
  <p class="eyebrow">Перспектива</p>
  <h2>С поворотом и без</h2>
  <div class="stack">
    <figure>
      <img src="__CMP__" alt="Слева камера просто едет вбок, справа разворачивается: вся сцена уходит в перспективу">
      <figcaption>Один и тот же момент. Слева камера едет вбок — слои остаются плоскими прямоугольниками.
      Справа она разворачивается: у каждого слоя свои углы, сцена читается объёмной.</figcaption>
    </figure>
    <p class="col">Пока камера просто едет вбок, слой остаётся прямоугольником — меняются только
    его положение и размер. Стоит камере развернуться, и четыре угла слоя оказываются на <b>разном</b>
    расстоянии от неё; дальше работает обычное деление на расстояние: ближний угол растёт, дальний
    сжимается. Отсюда сходящиеся края у каждого объекта сразу.</p>
  </div>
</section>

<section>
  <p class="eyebrow">Как считается</p>
  <h2>Одна формула на оба случая</h2>
  <div class="stack">
    <p class="col">Каждый угол каждого слоя переводится в мировые координаты, затем в систему
    камеры — сдвиг, потом разворот — и только после этого делится на расстояние:</p>
    <pre>мир     X = (bx + u)·z / f,   Y = (by + v)·z / f,   Z = z
камера  p = поворот( (X,Y,Z) − положение_камеры )
экран   x = f · p.x / p.z,   y = f · p.y / p.z</pre>
    <p class="col">При нулевом повороте выражение сворачивается ровно в прежнюю формулу параллакса —
    то есть обычный сдвиг остаётся частным случаем, а не отдельной веткой кода.</p>
    <div class="note col">
      <p style="margin:0"><b>Разворот идёт по дуге, а не на месте.</b> Камера на штативе объёма
      не даёт: все слои растянуты на кадр, значит с любой глубины видны под одним углом и
      исказились бы одинаково. Поэтому камера разворачивается <b>вокруг точки в глубине сцены</b> —
      слои на её глубине стоят, ближние и дальние расходятся в разные стороны.</p>
    </div>
  </div>
</section>

<section>
  <p class="eyebrow">Честно о границах</p>
  <h2>Что стоит знать</h2>
  <ul class="col">
    <li><b>Перспектива появляется в плеере после перезагрузки проекта.</b> Живой канал превью
    передаёт позицию, масштаб и поворот, но не углы. Панель пишет об этом прямо и даёт кнопку
    «Показать перспективу». Обычный параллакс без разворота остаётся живым, без перезаходов.</li>
    <li><b>Углы и трансформ складываются, а не заменяют друг друга.</b> Пока писалось и то и другое,
    на середине панорамы кадр ужимался вдвое. Теперь у повёрнутых слоёв положение и масштаб целиком
    описываются четырьмя углами.</li>
    <li><b>«Не выходить за рамку» работает и при развороте:</b> четырёхугольник дорастает, пока не
    накроет все четыре угла кадра, иначе отвернувшаяся камера показала бы пустоту за краем слоя.</li>
    <li><b>Кадры выше посчитаны теми же формулами,</b> что плагин отдаёт движку, и сверены с
    плеером покадрово. Именно это сравнение вскрыло, что у углов ось Y направлена вниз —
    с ошибочным знаком фон вставал зеркально.</li>
  </ul>
</section>
</div>
"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(HTML.replace("__GIF__", GIF).replace("__CMP__", CMP))
print(OUT, os.path.getsize(OUT) // 1024, "КБ")
