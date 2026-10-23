#!/usr/bin/env python3
"""Анализ CSV, собранного probe.py: сводка по часам и дням недели (МСК),
окна минимальной задержки, HTML-отчёт с графиками (SVG, без зависимостей).

Пример: python3 analyze.py data/measurements.csv --out report
"""

import argparse
import csv
import html
import os
import statistics
from collections import defaultdict

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def quantile(values, q):
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def fmt(x, nd=0):
    return "" if x is None else f"{x:.{nd}f}"


def load(path, metric):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ok = r["status"].isdigit() and int(r["status"]) < 400 and not r["error"].startswith(
                ("disallowed",)) and r[metric] != ""
            rows.append({
                "target": r["target"], "probe": r["probe_id"],
                "wd": int(r["weekday_msk"]), "hour": int(r["hour_msk"]),
                "day": r["ts_msk"][:10],
                "value": float(r[metric]) if ok else None,
                "failed": not ok,
            })
    return rows


def aggregate(rows, key):
    groups = defaultdict(lambda: {"values": [], "n": 0, "fail": 0})
    for r in rows:
        g = groups[key(r)]
        g["n"] += 1
        if r["failed"]:
            g["fail"] += 1
        else:
            g["values"].append(r["value"])
    out = {}
    for k, g in groups.items():
        v = g["values"]
        out[k] = {
            "n": g["n"], "fail": g["fail"],
            "median": statistics.median(v) if v else None,
            "p90": quantile(v, 0.9), "min": min(v) if v else None,
            "max": max(v) if v else None,
            "fail_pct": 100 * g["fail"] / g["n"],
        }
    return out


def heatmap_svg(cells, title):
    """cells: {(wd, hour): median}"""
    cw, ch, left, top = 26, 22, 34, 34
    vals = [v for v in cells.values() if v is not None]
    w, h = left + 24 * cw + 10, top + 7 * ch + 40
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{html.escape(title)}">']
    for hr in range(24):
        parts.append(f'<text x="{left + hr * cw + cw / 2}" y="{top - 8}" class="ax">{hr}</text>')
    for wd in range(1, 8):
        y = top + (wd - 1) * ch
        parts.append(f'<text x="{left - 6}" y="{y + ch / 2 + 4}" class="ax" style="text-anchor:end">'
                     f'{WEEKDAYS[wd - 1]}</text>')
        for hr in range(24):
            v = cells.get((wd, hr))
            x = left + hr * cw
            if v is None:
                parts.append(f'<rect x="{x}" y="{y}" width="{cw - 2}" height="{ch - 2}" class="empty"/>')
                continue
            t = 0 if len(vals) < 2 or max(vals) == min(vals) else (v - min(vals)) / (max(vals) - min(vals))
            # от светлого (быстро) к насыщенному (медленно)
            light = 92 - 52 * t
            parts.append(f'<rect x="{x}" y="{y}" width="{cw - 2}" height="{ch - 2}" '
                         f'fill="hsl(18 80% {light:.0f}%)"><title>{WEEKDAYS[wd - 1]} {hr}:00 — '
                         f'медиана {v:.0f} мс</title></rect>')
    if vals:
        parts.append(f'<text x="{left}" y="{h - 12}" class="ax" style="text-anchor:start">'
                     f'светлее — быстрее ({min(vals):.0f} мс), темнее — медленнее ({max(vals):.0f} мс)</text>')
    parts.append("</svg>")
    return "".join(parts)


def line_svg(series, title):
    """series: {hour: (median, p90)}"""
    w, h, left, top, bottom = 640, 220, 50, 16, 30
    pts = [(hr, *series[hr]) for hr in range(24) if hr in series and series[hr][0] is not None]
    if not pts:
        return "<p>Нет данных.</p>"
    ymax = max(p[2] or p[1] for p in pts) * 1.1
    sx = lambda hr: left + hr * (w - left - 10) / 23
    sy = lambda v: top + (h - top - bottom) * (1 - v / ymax)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{html.escape(title)}">']
    for i in range(5):
        v = ymax * i / 4
        parts.append(f'<line x1="{left}" x2="{w - 10}" y1="{sy(v)}" y2="{sy(v)}" class="grid"/>'
                     f'<text x="{left - 6}" y="{sy(v) + 4}" class="ax" style="text-anchor:end">{v:.0f}</text>')
    for hr in range(0, 24, 3):
        parts.append(f'<text x="{sx(hr)}" y="{h - 10}" class="ax">{hr}:00</text>')
    p90 = " ".join(f"{sx(p[0]):.1f},{sy(p[2]):.1f}" for p in pts if p[2] is not None)
    med = " ".join(f"{sx(p[0]):.1f},{sy(p[1]):.1f}" for p in pts)
    parts.append(f'<polyline points="{p90}" class="p90"/><polyline points="{med}" class="med"/>')
    parts.append("</svg>")
    return "".join(parts)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv")
    p.add_argument("--out", default="report")
    p.add_argument("--metric", default="ttfb_ms", choices=["ttfb_ms", "total_ms"],
                   help="ttfb_ms — время до первого байта (ближе к нагрузке на сервер)")
    p.add_argument("--window", type=int, default=3, help="длина окна в часах для поиска минимума")
    args = p.parse_args()

    rows = load(args.csv, args.metric)
    os.makedirs(args.out, exist_ok=True)
    targets = sorted({r["target"] for r in rows})
    probes = sorted({r["probe"] for r in rows})
    days = sorted({r["day"] for r in rows})

    by_cell = aggregate(rows, lambda r: (r["target"], r["wd"], r["hour"]))
    by_hour = aggregate(rows, lambda r: (r["target"], r["hour"]))
    by_wd = aggregate(rows, lambda r: (r["target"], r["wd"]))
    by_target = aggregate(rows, lambda r: r["target"])

    with open(os.path.join(args.out, "summary_weekday_hour.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["target", "weekday", "hour_msk", "n", "failed", "fail_pct",
                    f"median_{args.metric}", f"p90_{args.metric}", "min", "max"])
        for (t, wd, hr), s in sorted(by_cell.items()):
            w.writerow([t, WEEKDAYS[wd - 1], hr, s["n"], s["fail"], fmt(s["fail_pct"], 1),
                        fmt(s["median"], 1), fmt(s["p90"], 1), fmt(s["min"], 1), fmt(s["max"], 1)])

    # Окна минимальной и максимальной задержки: скользящее окно по часам (по кругу недели).
    windows = {}
    for t in targets:
        seq = []
        for wd in range(1, 8):
            for hr in range(24):
                s = by_cell.get((t, wd, hr))
                seq.append(((wd, hr), s["median"] if s else None))
        cand = []
        for i in range(len(seq)):
            win = [seq[(i + k) % len(seq)] for k in range(args.window)]
            vals = [v for _, v in win if v is not None]
            if len(vals) == args.window:
                cand.append((statistics.mean(vals), win[0][0]))
        cand.sort()
        windows[t] = {"best": cand[:5], "worst": cand[-5:][::-1]}

    def wlabel(start):
        wd, hr = start
        end = (hr + args.window) % 24
        return f"{WEEKDAYS[wd - 1]} {hr:02d}:00–{end:02d}:00"

    css = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--empty:#f0f0f2;--med:#c2410c;--p90:#94a3b8}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f4;--muted:#a1a1a6;--line:#3a3a3e;--empty:#26262a;--med:#fb923c;--p90:#64748b}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:0 auto;padding:16px}
h1{font-size:22px}h2{font-size:18px;margin-top:32px}.muted{color:var(--muted)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left}
.ax{font-size:11px;fill:var(--muted);text-anchor:middle}.empty{fill:var(--empty)}
.grid{stroke:var(--line)}.med{fill:none;stroke:var(--med);stroke-width:2}.p90{fill:none;stroke:var(--p90);stroke-width:1.5;stroke-dasharray:4 3}
.warn{border-left:3px solid var(--med);padding:4px 12px}
"""
    metric_name = "время до первого байта (TTFB)" if args.metric == "ttfb_ms" else "полное время загрузки"
    out = [f"<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width'>"
           f"<title>Задержка сайтов</title><style>{css}</style><body>",
           "<h1>Задержка ответа сайтов по часам и дням недели</h1>",
           f"<p class=muted>Метрика: {metric_name}, мс. Время — московское. "
           f"Период: {days[0] if days else '—'} — {days[-1] if days else '—'} ({len(days)} дн.). "
           f"Точки замера: {html.escape(', '.join(probes))}.</p>",
           "<p class=warn>Это косвенный показатель. Задержка ответа отражает загрузку сервера лишь отчасти: "
           "кэширование и запас мощности сглаживают пики. Посещаемость эти данные не показывают. "
           "Если точка замера вне России, в задержку входит международный канал.</p>",
           "<h2>Итог по адресам</h2><table><tr><th>Адрес</th><th>Замеров</th><th>Ошибок, %</th>"
           "<th>Медиана, мс</th><th>p90, мс</th><th>Мин–макс, мс</th></tr>"]
    for t in targets:
        s = by_target[t]
        out.append(f"<tr><td>{html.escape(t)}</td><td>{s['n']}</td><td>{fmt(s['fail_pct'], 1)}</td>"
                   f"<td>{fmt(s['median'])}</td><td>{fmt(s['p90'])}</td>"
                   f"<td>{fmt(s['min'])}–{fmt(s['max'])}</td></tr>")
    out.append("</table>")

    for t in targets:
        out.append(f"<h2>{html.escape(t)}</h2>")
        cells = {(wd, hr): s["median"] for (tt, wd, hr), s in by_cell.items() if tt == t}
        out.append("<p class=muted>Медиана по дню недели и часу</p>" + heatmap_svg(cells, t))
        hours = {hr: (s["median"], s["p90"]) for (tt, hr), s in by_hour.items() if tt == t}
        out.append("<p class=muted>По часам суток, все дни: сплошная — медиана, пунктир — p90</p>"
                   + line_svg(hours, t))
        out.append("<table><tr><th>День</th><th>Замеров</th><th>Ошибок, %</th><th>Медиана, мс</th>"
                   "<th>p90, мс</th></tr>")
        for wd in range(1, 8):
            s = by_wd.get((t, wd))
            if s:
                out.append(f"<tr><td>{WEEKDAYS[wd - 1]}</td><td>{s['n']}</td><td>{fmt(s['fail_pct'], 1)}</td>"
                           f"<td>{fmt(s['median'])}</td><td>{fmt(s['p90'])}</td></tr>")
        out.append("</table>")
        wb = windows[t]
        out.append(f"<p><b>Окна минимальной задержки ({args.window} ч):</b> "
                   + "; ".join(f"{wlabel(st)} — {v:.0f} мс" for v, st in wb["best"]) + "</p>")
        out.append(f"<p><b>Окна максимальной задержки:</b> "
                   + "; ".join(f"{wlabel(st)} — {v:.0f} мс" for v, st in wb["worst"]) + "</p>")
    if len(days) < 7:
        out.append(f"<p class=warn>Данных меньше чем за неделю ({len(days)} дн.): часть ячеек пуста, "
                   "выводы о днях недели предварительные.</p>")
    out.append("</body></html>")
    with open(os.path.join(args.out, "report.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    print(f"Замеров: {len(rows)}, адресов: {len(targets)}, дней: {len(days)}")
    for t in targets:
        s = by_target[t]
        print(f"  {t}: медиана {fmt(s['median'])} мс, p90 {fmt(s['p90'])} мс, ошибок {fmt(s['fail_pct'], 1)}%")
        print("    минимум: " + "; ".join(wlabel(st) for _, st in windows[t]["best"][:3]))
    print(f"Отчёт: {os.path.join(args.out, 'report.html')}")


if __name__ == "__main__":
    main()
