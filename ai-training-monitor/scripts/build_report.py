#!/usr/bin/env python3
"""Мониторинг обучения адвокатов ИИ: проверка данных агента, дедупликация,
динамика по группам, вёрстка письма (HTML + Markdown), накопительная таблица.

Вся арифметика отчёта выполняется здесь, а не моделью.

Режимы:
  build_report.py plan  --watchlist W --data-dir D [--date ГГГГ-ММ-ДД]
      список страниц на сегодня (уровень A; по понедельникам и при первом
      запуске — также B) и окно мониторинга; печатает JSON.
  build_report.py build RUN.json --watchlist W --data-dir D [--dry-run] [--rerun]
      проверяет RUN.json, отбрасывает повторы, обновляет state/, пишет
      <D>/<дата>.html, <D>/<дата>.md, <D>/runs/<дата>.json; печатает тему письма
      и сводку. Код выхода 2 — ошибки формата (ничего не записано).
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys

GROUPS = ["РФ", "I", "II", "III", "IV"]
TYPES = ["курс_ИИ", "расписание", "обязательность_норматив", "руководство_ИИ",
         "сертификация", "платформа_провайдер", "исследование_отчёт", "иное"]
LEVELS = ["М0", "М1", "М2", "М3", "М4", "М5", "М6", "—"]
LEVEL_NAMES = {"М0": "стажёры", "М1": "базовый модуль", "М2": "практикум",
               "М3": "специализации", "М4": "экспертный уровень",
               "М5": "руководители", "М6": "доверители", "—": "—"}
VERIF = ["ПРОВЕРЕНО", "ПО ВТОРИЧНОМУ ИСТОЧНИКУ", "НЕ ПРОВЕРЕНО"]
CHANGES = ["новое", "обновление"]
FETCH_MODES = ["страницы", "только_поиск"]
DEDUP_DAYS = 60
FIRST_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 30
EVENT_FIELDS = ["event_id", "date_event", "group", "country", "org", "type", "summary",
                "level", "impact", "verification", "url", "change", "importance"]
CSV_FIELDS = ["run_date"] + EVENT_FIELDS
ID_RE = re.compile(r"^[A-ZА-Я]{2}[A-Za-zА-Яа-яЁё0-9-]*-\d{4}-\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today_msk():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).date()


def parse_date(s):
    return dt.date.fromisoformat(s)


def ru(d):
    return d.strftime("%d.%m.%Y")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_watchlist(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_events(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_events(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


# ---------------------------------------------------------------- plan

def make_plan(watchlist, data_dir, run_date):
    last = load_json(os.path.join(data_dir, "state", "last_run.json"), None)
    first_run = last is None
    if first_run:
        window_from = run_date - dt.timedelta(days=FIRST_WINDOW_DAYS)
    else:
        window_from = parse_date(last["run_date"])
        window_from = max(window_from, run_date - dt.timedelta(days=MAX_WINDOW_DAYS))
    with_b = first_run or run_date.weekday() == 0
    planned, skipped = [], []
    for r in watchlist:
        if r["status"].strip() != "активен":
            skipped.append(r)
            continue
        if r["tier"] == "A" or (with_b and r["tier"] == "B"):
            planned.append(r)
    return {
        "run_date": run_date.isoformat(),
        "window_from": ru(window_from),
        "first_run": first_run,
        "tiers": "A+B" if with_b else "A",
        "planned_count": len(planned),
        "inactive_count": len(skipped),
        "search_domains": {g: sorted({re.sub(r"^www\\.", "", r["url"].split("/")[2])
                                      for r in planned if r["group"] == g}) for g in GROUPS},
        "planned": [{"code": r["code"], "group": r["group"], "country": r["country"],
                     "org": r["org"], "name": r["name"], "url": r["url"], "tier": r["tier"]}
                    for r in planned],
    }


# ---------------------------------------------------------------- validate

def validate(run):
    errors, warns = [], []
    for k in ("run_date", "window_from", "events", "unreachable"):
        if k not in run:
            errors.append(f"нет поля {k}")
    if errors:
        return errors, warns
    if not DATE_RE.match(str(run["run_date"])):
        errors.append("run_date: нужен формат ГГГГ-ММ-ДД")
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", str(run["window_from"])):
        errors.append("window_from: нужен формат ДД.ММ.ГГГГ")
    ids = set()
    for i, e in enumerate(run["events"], 1):
        p = f"events[{i}] {e.get('event_id', '?')}"
        for k in EVENT_FIELDS:
            if k not in e:
                errors.append(f"{p}: нет поля {k}")
        if any(k not in e for k in EVENT_FIELDS):
            continue
        if not ID_RE.match(str(e["event_id"])):
            errors.append(f"{p}: event_id должен иметь вид СТРАНА-ОРГАН-суть-ГГГГ-ММ")
        if e["event_id"] in ids:
            errors.append(f"{p}: event_id повторяется в файле")
        ids.add(e["event_id"])
        if e["date_event"] not in (None, "") and not DATE_RE.match(str(e["date_event"])):
            errors.append(f"{p}: date_event — ГГГГ-ММ-ДД или null")
        for k, allowed in (("group", GROUPS), ("type", TYPES), ("level", LEVELS),
                           ("verification", VERIF), ("change", CHANGES)):
            if e[k] not in allowed:
                errors.append(f"{p}: {k}={e[k]!r} не из перечня {allowed}")
        if e["importance"] not in (1, 2, 3):
            errors.append(f"{p}: importance — 1, 2 или 3")
        if not str(e["url"]).startswith(("http://", "https://")):
            errors.append(f"{p}: url должен начинаться с http(s)://")
        for k in ("summary", "impact", "country", "org"):
            if not str(e[k]).strip():
                errors.append(f"{p}: пустое поле {k}")
        if e["verification"] == "ПРОВЕРЕНО" and run.get("fetch_mode") == "только_поиск":
            errors.append(f"{p}: ПРОВЕРЕНО невозможно в режиме только_поиск — страница не открывалась")
        if e["verification"] == "ПРОВЕРЕНО" and not e["date_event"]:
            warns.append(f"{p}: ПРОВЕРЕНО без даты события — допустимо, если даты нет на странице")
    if run.get("fetch_mode", "страницы") not in FETCH_MODES:
        errors.append(f"fetch_mode: {run.get('fetch_mode')!r} не из перечня {FETCH_MODES}")
    for i, u in enumerate(run.get("unreachable", []), 1):
        if not u.get("url") or not u.get("reason"):
            errors.append(f"unreachable[{i}]: нужны url и reason")
    return errors, warns


# ---------------------------------------------------------------- render

CSS_TD = "border:1px solid #999;padding:4px 6px;vertical-align:top;"
CSS_TH = CSS_TD + "background:#e8e8e8;text-align:left;"
FONT = "font-family:'Times New Roman',Times,serif;font-size:14px;line-height:1.35;"


def h(s):
    return html.escape("" if s is None else str(s))


def table_html(headers, rows):
    out = ['<table style="border-collapse:collapse;margin:6px 0 14px 0;">', "<tr>"]
    out += [f'<th style="{CSS_TH}">{h(x)}</th>' for x in headers]
    out.append("</tr>")
    for r in rows:
        out.append("<tr>" + "".join(f'<td style="{CSS_TD}">{c}</td>' for c in r) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def table_md(headers, rows):
    def esc(x):
        return re.sub(r"<[^>]+>", "", str(x)).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def render(ctx):
    events = ctx["new"]
    sec_h, sec_m = [], []

    def add(title, body_h, body_m):
        sec_h.append(f'<h3 style="margin:16px 0 4px 0;">{h(title)}</h3>\n{body_h}')
        sec_m.append(f"## {title}\n\n{body_m}")

    s = ctx["summary"]
    if ctx["search_only"]:
        warn = ("ОХВАТ НЕПОЛНЫЙ: в среде запуска не работает чтение страниц (WebFetch), "
                "страницы реестра не открывались. События найдены только поиском "
                "(WebSearch) и не могут иметь статус ПРОВЕРЕНО. Чтобы включить полный "
                "режим, расширьте сетевой доступ облачной среды Claude Code.")
        sec_h.append(f'<p style="border:2px solid #b00;padding:6px;"><b>{h(warn)}</b></p>')
        sec_m.append(f"**{warn}**")
    rows = [["Окно мониторинга", f'{s["window_from"]} – {s["run_date_ru"]}'],
            ["Уровни проверки", s["tiers"]],
            ["Новых событий", s["new"]],
            ["Отброшено повторов", s["dupes"]],
            ["Страниц по плану", s["planned"]],
            ["Не открылись", s["unreachable"]],
            ["Открыто (по плану минус недоступные)", s["opened"]]]
    if ctx["empty"]:
        rows.insert(2, ["Итог", "ИЗМЕНЕНИЙ НЕТ"])
    add("1. Сводка периода",
        table_html(["Показатель", "Значение"], [[h(a), h(b)] for a, b in rows]),
        table_md(["Показатель", "Значение"], rows))

    top = [e for e in events if e["importance"] == 3]
    if top:
        items_h = "<ol>" + "".join(
            f'<li><b>{h(e["country"])}, {h(e["org"])}</b>: {h(e["summary"])} '
            f'<i>({h(e["level"])}; {h(e["verification"])})</i> '
            f'<a href="{h(e["url"])}">источник</a></li>' for e in top) + "</ol>"
        items_m = "\n".join(f'{i}. **{e["country"]}, {e["org"]}**: {e["summary"]} '
                            f'({e["level"]}; {e["verification"]}) — {e["url"]}'
                            for i, e in enumerate(top, 1))
    else:
        items_h, items_m = "<p>Событий высшей важности нет.</p>", "Событий высшей важности нет."
    add("2. Главное", items_h, items_m)

    heads = ["№", "Дата", "Гр.", "Страна / орган", "Тип", "Что произошло",
             "Для нашей системы", "Проверка", "Источник"]
    rh, rm = [], []
    for i, e in enumerate(events, 1):
        typ = e["type"] + (" (обновление)" if e["change"] == "обновление" else "")
        lvl = f'{e["level"]} {LEVEL_NAMES[e["level"]]}'.strip() if e["level"] != "—" else "—"
        base = [i, e["date_event"] or "—", e["group"], f'{e["country"]} / {e["org"]}', typ,
                e["summary"], f'{lvl}: {e["impact"]}', e["verification"]]
        rh.append([h(x) for x in base] + [f'<a href="{h(e["url"])}">ссылка</a>'])
        rm.append(base + [e["url"]])
    if events:
        add("3. Новые события", table_html(heads, rh), table_md(heads, rm))
    else:
        add("3. Новые события", "<p>Изменений нет.</p>", "Изменений нет.")

    dh = ["Группа", "Сегодня", "7 дней", "30 дней"]
    dr = [[g, ctx["dyn"]["today"][g], ctx["dyn"]["d7"][g], ctx["dyn"]["d30"][g]]
          for g in GROUPS]
    dr.append(["Итого", sum(ctx["dyn"]["today"].values()), sum(ctx["dyn"]["d7"].values()),
               sum(ctx["dyn"]["d30"].values())])
    note = (f'Счёт по дате записи в накопительную таблицу (state/events.csv); '
            f'всего записей в таблице: {ctx["dyn"]["total"]}.')
    add("4. Динамика по группам", table_html(dh, [[h(c) for c in r] for r in dr])
        + f"<p>{h(note)}</p>", table_md(dh, dr) + "\n\n" + note)

    un = ctx["unreachable"]
    if ctx["search_only"]:
        txt = (f"Не открывалась ни одна страница плана ({s['unreachable']}): WebFetch "
               "недоступен в среде запуска. Полный список — в реестре watchlist.csv.")
        add("5. Источники, которые не открылись", f"<p>{h(txt)}</p>", txt)
    elif un:
        uh = ["Код", "Страница", "Причина"]
        add("5. Источники, которые не открылись",
            table_html(uh, [[h(u.get("code", "—")), f'<a href="{h(u["url"])}">{h(u.get("name") or u["url"])}</a>',
                             h(u["reason"])] for u in un]),
            table_md(uh, [[u.get("code", "—"), u.get("name") or "", u["reason"] + " — " + u["url"]]
                          for u in un]))
    else:
        add("5. Источники, которые не открылись", "<p>Все страницы плана открылись.</p>",
            "Все страницы плана открылись.")

    pr = ctx["proposals"]
    if pr:
        ph = ["Источник", "Почему"]
        add("6. Предложения по реестру",
            table_html(ph, [[f'<a href="{h(p.get("url"))}">{h(p.get("name"))}</a>', h(p.get("why"))]
                            for p in pr]),
            table_md(ph, [[f'{p.get("name")} — {p.get("url")}', p.get("why")] for p in pr]))
    else:
        add("6. Предложения по реестру", "<p>Нет.</p>", "Нет.")

    if ctx["notes"]:
        add("Примечания агента", f'<p>{h(ctx["notes"])}</p>', ctx["notes"])

    legend = ("ПРОВЕРЕНО — страница первоисточника открыта в этом запуске, даты и цифры взяты "
              "оттуда; ПО ВТОРИЧНОМУ ИСТОЧНИКУ — сведения из прессы или анонса, первоисточник "
              "не открыт; НЕ ПРОВЕРЕНО — сведения из поисковой выдачи. Отчёт — сигнал для "
              "проверки, а не исчерпывающий реестр; использовать событие в документах можно "
              "только после сверки с первоисточником. Все числа посчитаны скриптом build_report.py.")
    add("Статусы проверки", f"<p>{h(legend)}</p>", legend)

    title = ctx["subject"]
    body_h = (f'<!doctype html><html><head><meta charset="utf-8"><title>{h(title)}</title></head>'
              f'<body style="{FONT}"><div style="{FONT}"><h2 style="margin:0 0 8px 0;">{h(title)}</h2>\n'
              + "\n".join(sec_h) + "</div></body></html>\n")
    body_m = f"# {title}\n\n" + "\n\n".join(sec_m) + "\n"
    return body_h, body_m


# ---------------------------------------------------------------- build

def build(args):
    with open(args.run, encoding="utf-8") as f:
        run = json.load(f)
    errors, warns = validate(run)
    for w in warns:
        print("WARN:", w)
    if errors:
        for e in errors:
            print("ERROR:", e)
        print("Проверка НЕ ПРОЙДЕНА: исправьте формат (не факты) и запустите снова.")
        return 2

    run_date = parse_date(run["run_date"])
    d = args.data_dir
    seen_p = os.path.join(d, "state", "seen.json")
    ev_p = os.path.join(d, "state", "events.csv")
    last_p = os.path.join(d, "state", "last_run.json")
    seen = load_json(seen_p, {})
    rows = read_events(ev_p)
    last = load_json(last_p, None)
    watch = read_watchlist(args.watchlist)

    if last and last.get("run_date") == run["run_date"]:
        if not args.rerun:
            print(f"ERROR: отчёт за {run['run_date']} уже собран (state/last_run.json). "
                  "Для повторной сборки — флаг --rerun.")
            return 3
        seen = {k: v for k, v in seen.items() if v != run["run_date"]}
        rows = [r for r in rows if r["run_date"] != run["run_date"]]
        last = last.get("previous")

    # первый запуск — уровни A и B; далее A, по понедельникам A и B
    if last is None:
        planned = [r for r in watch if r["status"].strip() == "активен"]
        tiers = "A+B"
    else:
        with_b = run_date.weekday() == 0
        planned = [r for r in watch if r["status"].strip() == "активен"
                   and (r["tier"] == "A" or (with_b and r["tier"] == "B"))]
        tiers = "A+B" if with_b else "A"
    by_url = {r["url"]: r for r in watch}
    by_code = {r["code"]: r for r in watch}
    planned_urls = {r["url"] for r in planned}

    unreachable = []
    for u in run.get("unreachable", []):
        w = by_code.get(u.get("code")) or by_url.get(u.get("url"))
        unreachable.append({"code": u.get("code") or (w["code"] if w else "—"),
                            "name": u.get("name") or (w["name"] if w else ""),
                            "url": u["url"], "reason": u["reason"],
                            "planned": bool(w and w["url"] in planned_urls)})
    search_only = run.get("fetch_mode") == "только_поиск"
    if search_only:
        # WebFetch не работает в среде: недоступны все страницы плана
        listed = {u["url"] for u in unreachable}
        unreachable += [{"code": r["code"], "name": r["name"], "url": r["url"],
                         "reason": "WebFetch недоступен в среде запуска (сетевая политика)",
                         "planned": True} for r in planned if r["url"] not in listed]
    unreach_planned = sum(1 for u in unreachable if u["planned"])

    cutoff = run_date - dt.timedelta(days=DEDUP_DAYS)
    new, dupes = [], []
    for e in run["events"]:
        first = seen.get(e["event_id"])
        if first and parse_date(first) >= cutoff:
            dupes.append(e["event_id"])
            continue
        new.append(e)
    gorder = {g: i for i, g in enumerate(GROUPS)}
    new.sort(key=lambda e: (-e["importance"], gorder[e["group"]], e["country"],
                            e["date_event"] or "", e["event_id"]))

    for e in new:
        seen[e["event_id"]] = run["run_date"]
        rows.append(dict(e, run_date=run["run_date"]))

    def count(since):
        c = {g: 0 for g in GROUPS}
        for r in rows:
            if parse_date(r["run_date"]) > since and r["group"] in c:
                c[r["group"]] += 1
        return c

    dyn = {"today": count(run_date - dt.timedelta(days=1)),
           "d7": count(run_date - dt.timedelta(days=7)),
           "d30": count(run_date - dt.timedelta(days=30)),
           "total": len(rows)}

    n = len(new)
    subject = (f"ИИ-обучение адвокатов: мониторинг {ru(run_date)} "
               + (f"({n} {plural(n, 'новое событие', 'новых события', 'новых событий')})"
                  if n else "(изменений нет)"))
    if search_only:
        subject += " — ОХВАТ НЕПОЛНЫЙ"
    ctx = {"new": new, "search_only": search_only, "empty": n == 0, "unreachable": unreachable,
           "proposals": run.get("source_proposals", []), "notes": run.get("notes", ""),
           "dyn": dyn, "subject": subject,
           "summary": {"window_from": run["window_from"], "run_date_ru": ru(run_date),
                       "tiers": tiers, "new": n, "dupes": len(dupes),
                       "planned": len(planned), "unreachable": unreach_planned,
                       "opened": len(planned) - unreach_planned}}
    body_h, body_m = render(ctx)

    stem = run["run_date"]
    if args.dry_run:
        out_h = os.path.join(args.out or ".", f"{stem}.preview.html")
        with open(out_h, "w", encoding="utf-8") as f:
            f.write(body_h)
        print("DRY-RUN: state не изменён; превью:", out_h)
    else:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{stem}.html"), "w", encoding="utf-8") as f:
            f.write(body_h)
        with open(os.path.join(d, f"{stem}.md"), "w", encoding="utf-8") as f:
            f.write(body_m)
        save_json(os.path.join(d, "runs", f"{stem}.json"), run)
        save_json(seen_p, dict(sorted(seen.items())))
        write_events(ev_p, rows)
        save_json(last_p, {"run_date": stem, "window_from": run["window_from"],
                           "new_events": n, "duplicates": len(dupes),
                           "planned": len(planned), "unreachable": unreach_planned,
                           "fetch_mode": run.get("fetch_mode", "страницы"),
                           "subject": subject, "previous": last})
        with open(os.path.join(d, f"{stem}.subject.txt"), "w", encoding="utf-8") as f:
            f.write(subject + "\n")
    print("SUBJECT:", subject)
    print(f"Новых: {n}; повторов отброшено: {len(dupes)}"
          + (f" ({', '.join(dupes)})" if dupes else "")
          + f"; по плану: {len(planned)}; не открылись: {unreach_planned}; "
          f"7 дней: {sum(dyn['d7'].values())}; 30 дней: {sum(dyn['d30'].values())}")
    print("Проверка ПРОЙДЕНА")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--watchlist", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--date")
    b = sub.add_parser("build")
    b.add_argument("run")
    b.add_argument("--watchlist", required=True)
    b.add_argument("--data-dir", required=True)
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--rerun", action="store_true")
    b.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "plan":
        rd = parse_date(a.date) if a.date else today_msk()
        print(json.dumps(make_plan(read_watchlist(a.watchlist), a.data_dir, rd),
                         ensure_ascii=False, indent=1))
        return 0
    return build(a)


if __name__ == "__main__":
    sys.exit(main())
