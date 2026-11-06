#!/usr/bin/env python3
"""Сборка еженедельного отчёта о подготовке претендентов в адвокаты.

Объединяет ответы зональных агентов (NA, CL, EU, AS, RU) с базой прошлой недели,
находит изменения стоимости и сроков, считает ближайшие даты и рублёвые эквиваленты
(по курсам ЦБ РФ из fx.json), формирует HTML/текст письма, CSV и список перепроверки.

Пример:
  python3 build_report.py --run-date 2026-11-06 --period-start 2026-10-30 \
      --period-end 2026-11-06 --zones-dir /path/zones_out --fx /path/fx.json \
      --out-dir /path/out
Все суммы и даты считаются здесь, а не агентом.
"""
import argparse
import csv
import datetime as dt
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZONES = ["RU", "NA", "CL", "EU", "AS"]
ZONE_NAMES = {
    "RU": "Россия, ЕАЭС и Центральная Азия",
    "NA": "США и Канада",
    "CL": "Общее право (Великобритания, Ирландия, Австралия, Новая Зеландия, Залив)",
    "EU": "Континентальная Европа и наднациональный уровень",
    "AS": "Азия",
}
STAGE_RU = {
    "профессиональный_курс": "профессиональный курс",
    "стажировка_практика": "стажировка / практика",
    "квалификационный_экзамен": "квалификационный экзамен",
    "вступительный_экзамен": "вступительный экзамен",
    "подготовка_к_экзамену": "подготовка к экзамену",
}
HORIZON_DAYS = 90
RECHECK_DAYS = 28
RED = '<span style="color:#C00000;font-weight:bold">[ПРОВЕРИТЬ]</span>'


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    w = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {w}"


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_zone_outputs(zones_dir):
    out, problems = {}, []
    for z in ZONES:
        p = Path(zones_dir) / f"{z}.json"
        if not p.exists():
            problems.append(f"{z}: ответ зонального агента отсутствует")
            continue
        try:
            data = load_json(p)
        except json.JSONDecodeError as e:
            problems.append(f"{z}: невалидный JSON ({e})")
            continue
        for key in ("programs", "jurisdictions", "events"):
            data.setdefault(key, [])
        out[z] = data
    return out, problems


def cost_key(c):
    return (c.get("item_ru") or "", c.get("currency") or "", c.get("period_ru") or "")


def fmt_amount(a):
    if a is None:
        return "—"
    if float(a).is_integer():
        return f"{int(a):,}".replace(",", " ")
    return f"{a:,.2f}".replace(",", " ").replace(".", ",")


def rub(amount, currency, fx):
    if amount is None or not currency:
        return None
    if currency == "RUB":
        return float(amount)
    rate = (fx or {}).get("rates", {}).get(currency)
    return round(float(amount) * rate, 2) if rate else None


def diff_program(old, new):
    """Изменения между версией программы прошлой недели и текущей."""
    changes = []
    oc = {cost_key(c): c.get("amount") for c in old.get("costs") or []}
    nc = {cost_key(c): c.get("amount") for c in new.get("costs") or []}
    for k, amt in nc.items():
        if k in oc and oc[k] != amt and oc[k] is not None and amt is not None:
            delta = amt - oc[k]
            pct = delta / oc[k] * 100 if oc[k] else 0
            changes.append(("изменение_стоимости",
                            f"{k[0]}: {fmt_amount(oc[k])} → {fmt_amount(amt)} {k[1]} "
                            f"({'+' if delta > 0 else ''}{fmt_amount(round(delta, 2))}; {pct:+.1f}%)".replace(".", ",")))
        elif k not in oc and amt is not None and oc:
            changes.append(("изменение_стоимости", f"новая статья расходов: {k[0]} — {fmt_amount(amt)} {k[1]}"))
    for field, label in (("intakes", "наборы"), ("exam_dates", "даты экзаменов")):
        def norm(lst):
            return sorted(json.dumps({kk: vv for kk, vv in (x or {}).items() if kk != "note_ru"},
                                     sort_keys=True, ensure_ascii=False) for x in lst or [])
        if norm(old.get(field)) != norm(new.get(field)) and (new.get(field) or []):
            changes.append(("изменение_сроков", f"обновлены {label}"))
    if (old.get("upcoming_changes_ru") or "") != (new.get("upcoming_changes_ru") or "") and new.get("upcoming_changes_ru"):
        changes.append(("реформа", f"предстоящие изменения: {new['upcoming_changes_ru']}"))
    if (old.get("duration_months") != new.get("duration_months")) and new.get("duration_months") and old.get("duration_months"):
        changes.append(("изменение_программы",
                        f"длительность: {old['duration_months']} → {new['duration_months']} мес."))
    return changes


def upcoming(programs, run_date):
    rows = []
    end = run_date + dt.timedelta(days=HORIZON_DAYS)
    for p in programs.values():
        for it in p.get("intakes") or []:
            for f, label in (("application_deadline", "срок подачи заявлений"), ("start", "начало обучения")):
                d = parse_date(it.get(f))
                if d and run_date <= d <= end:
                    rows.append((d, p, label, it.get("note_ru")))
        for ex in p.get("exam_dates") or []:
            for f, label in (("booking_deadline", "срок записи на экзамен"), ("date", "экзамен")):
                d = parse_date(ex.get(f))
                if d and run_date <= d <= end:
                    rows.append((d, p, label, ex.get("note_ru")))
    rows.sort(key=lambda r: (r[0], r[1]["program_id"]))
    return rows


def mark(p, field):
    if field in (p.get("unverified_fields") or []) or p.get("verification") == "вторичные_источники":
        return " " + RED
    return ""


def cost_cell(p, fx):
    parts = []
    for c in p.get("costs") or []:
        r = rub(c.get("amount"), c.get("currency"), fx)
        r_txt = f" (≈{fmt_amount(round(r))} руб.)" if r is not None and c.get("currency") != "RUB" else ""
        parts.append(f"{esc(c.get('item_ru'))}: {fmt_amount(c.get('amount'))} {esc(c.get('currency'))}"
                     f"{r_txt}{', ' + esc(c.get('period_ru')) if c.get('period_ru') else ''}")
    return ("<br>".join(parts) or "не установлено") + mark(p, "costs")


def build(args):
    run_date = dt.date.fromisoformat(args.run_date)
    baseline_path = Path(args.baseline)
    baseline = load_json(baseline_path) if baseline_path.exists() else {"programs": {}, "jurisdictions": {}}
    old_programs = baseline.get("programs", {})
    fx = load_json(args.fx) if args.fx and Path(args.fx).exists() else None

    zone_out, problems = load_zone_outputs(args.zones_dir)
    programs = {pid: dict(p) for pid, p in old_programs.items()}
    jurisdictions = dict(baseline.get("jurisdictions", {}))
    changes, new_ids, events, coverage = [], [], [], {}

    for z, data in zone_out.items():
        coverage[z] = data.get("coverage_note") or ""
        for j in data["jurisdictions"]:
            if j.get("jurisdiction"):
                jurisdictions[j["jurisdiction"]] = {**j, "zone": z}
        for p in data["programs"]:
            pid = p.get("program_id")
            if not pid:
                problems.append(f"{z}: программа без program_id пропущена")
                continue
            p["zone"] = z
            if pid in old_programs:
                for typ, txt in diff_program(old_programs[pid], p):
                    changes.append({"program_id": pid, "type": typ, "summary": txt, "zone": z,
                                    "source_url": p.get("source_url")})
            else:
                new_ids.append(pid)
            programs[pid] = p
        for e in data["events"]:
            e["zone"] = z
            events.append(e)

    first_run = not old_programs
    up = upcoming(programs, run_date)

    stale = []
    for pid, p in programs.items():
        ch = parse_date(p.get("checked_at"))
        soon = any(r[1]["program_id"] == pid and (r[0] - run_date).days <= 60 for r in up)
        if ch is None or (run_date - ch).days >= RECHECK_DAYS or soon:
            stale.append(pid)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    new_baseline = {
        "updated_at": run_date.isoformat(),
        "fx": fx,
        "jurisdictions": dict(sorted(jurisdictions.items())),
        "programs": dict(sorted(programs.items())),
    }
    with open(out / "baseline.json", "w", encoding="utf-8") as f:
        json.dump(new_baseline, f, ensure_ascii=False, indent=1)
    with open(out / "recheck.json", "w", encoding="utf-8") as f:
        json.dump(sorted(stale), f, ensure_ascii=False, indent=1)

    with open(out / "programs.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Зона", "Юрисдикция", "ID", "Этап", "Обязательность", "Провайдер", "Программа",
                    "Формат", "Длительность", "Стоимость", "≈ руб.", "Требования", "Источник",
                    "Проверено", "Требует проверки"])
        for pid, p in sorted(programs.items(), key=lambda kv: (ZONES.index(kv[1].get("zone", "RU")) if kv[1].get("zone") in ZONES else 9, kv[0])):
            costs = "; ".join(f"{c.get('item_ru')}: {c.get('amount')} {c.get('currency')}" for c in p.get("costs") or [])
            rubs = [rub(c.get("amount"), c.get("currency"), fx) for c in p.get("costs") or []]
            rub_txt = "; ".join(str(round(r)) if r is not None else "—" for r in rubs)
            w.writerow([p.get("zone"), p.get("jurisdiction"), pid, STAGE_RU.get(p.get("stage"), p.get("stage")),
                        p.get("mandatory"), p.get("provider_ru") or p.get("provider_original"),
                        p.get("program_name_ru") or p.get("program_name_original"), p.get("format"),
                        p.get("duration_ru"), costs, rub_txt, p.get("entry_requirements_ru"),
                        p.get("source_url"), p.get("checked_at"),
                        ", ".join(p.get("unverified_fields") or []) or ("все поля" if p.get("verification") == "вторичные_источники" else "")])

    # ---------- HTML ----------
    n_events = len([e for e in events if (e.get("significance") or 0) >= 3])
    subject = (f"{'ПРОБНЫЙ ЗАПУСК. ' if args.trial else ''}Подготовка претендентов в адвокаты: "
               f"обзор за {parse_date(args.period_start):%d.%m}–{parse_date(args.period_end):%d.%m.%Y} — "
               f"{plural(len(programs), 'программа', 'программы', 'программ')}, "
               f"{plural(n_events, 'событие', 'события', 'событий')}, "
               f"{plural(len(changes), 'изменение', 'изменения', 'изменений')}, "
               f"{plural(len(up), 'дата', 'даты', 'дат')} на {HORIZON_DAYS} дней")
    css = ("body{font-family:'Times New Roman',Times,serif;font-size:12pt;line-height:1.35;color:#000;max-width:1000px}"
           "h1{font-size:14pt;text-align:center;margin:0 0 6pt}h2{font-size:13pt;margin:16pt 0 6pt}"
           "h3{font-size:12pt;margin:12pt 0 4pt}p{text-align:justify;margin:0 0 6pt}"
           "table{border-collapse:collapse;width:100%;font-size:10.5pt;margin:4pt 0 10pt}"
           "th,td{border:1px solid #808080;padding:3pt 4pt;vertical-align:top;text-align:left}"
           "th{background:#EDEDED}.small{font-size:10pt;color:#333}")
    h = [f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>"]
    if args.trial:
        h.append("<p style='color:#C00000;font-weight:bold;text-align:center'>ПРОБНЫЙ ЗАПУСК АГЕНТА — "
                 "сведения требуют выборочной проверки перед использованием</p>")
    if args.warning:
        h.append(f"<p style='color:#C00000;font-weight:bold'>{esc(args.warning)}</p>")
    h.append("<h1>Подготовка претендентов в адвокаты: международный обзор</h1>")
    h.append(f"<p style='text-align:center'>Период: {parse_date(args.period_start):%d.%m.%Y} – "
             f"{parse_date(args.period_end):%d.%m.%Y}. Дата формирования: {run_date:%d.%m.%Y}.</p>")
    h.append("<p class='small'>Сведения собраны с официальных сайтов регуляторов, палат, экзаменационных органов "
             f"и провайдеров. Отметка {RED} — значение не подтверждено первоисточником в этом запуске. "
             "Рублёвые эквиваленты — справочно, "
             + (f"по курсам ЦБ РФ на {esc(fx.get('date'))}." if fx else "курсы ЦБ РФ не получены, пересчёт не выполнен.")
             + "</p>")

    # 1. Главное
    h.append("<h2>1. Главное за неделю</h2>")
    key_events = sorted([e for e in events if (e.get("significance") or 0) >= 3],
                        key=lambda e: (-(e.get("significance") or 0), e.get("date") or ""))
    if key_events:
        h.append("<table><tr><th>Знач.</th><th>Юрисдикция</th><th>Дата</th><th>Событие</th><th>Источник</th></tr>")
        for e in key_events:
            ver = " " + RED if e.get("verification") == "вторичные_источники" else ""
            h.append(f"<tr><td>{esc(e.get('significance'))}</td><td>{esc(e.get('jurisdiction'))}</td>"
                     f"<td>{esc(e.get('date'))}</td><td>{esc(e.get('summary_ru'))}{ver}</td>"
                     f"<td><a href='{esc(e.get('source_url'))}'>источник</a></td></tr>")
        h.append("</table>")
    else:
        h.append("<p>Значимых событий (значимость 3 и выше) за период не выявлено.</p>")

    # 2. Ближайшие даты
    h.append(f"<h2>2. Ближайшие сроки и даты ({HORIZON_DAYS} дней)</h2>")
    if up:
        h.append("<table><tr><th>Дата</th><th>Через, дн.</th><th>Юрисдикция</th><th>Программа</th><th>Что</th><th>Примечание</th></tr>")
        for d, p, label, note in up:
            h.append(f"<tr><td>{d:%d.%m.%Y}</td><td>{(d - run_date).days}</td><td>{esc(p.get('jurisdiction'))}</td>"
                     f"<td><a href='{esc(p.get('source_url'))}'>{esc(p.get('program_name_ru') or p.get('program_name_original'))}</a>"
                     f"{mark(p, 'intakes') or mark(p, 'exam_dates')}</td><td>{esc(label)}</td><td>{esc(note)}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p>Дат в пределах горизонта не установлено.</p>")

    # 3. Изменения
    h.append("<h2>3. Изменения по сравнению с прошлой неделей</h2>")
    if first_run:
        h.append(f"<p>Первый запуск: база сформирована заново ({len(programs)} программ). "
                 "Сравнение начнётся со следующего отчёта.</p>")
    elif changes or new_ids:
        h.append("<table><tr><th>Программа</th><th>Тип</th><th>Что изменилось</th></tr>")
        for c in changes:
            p = programs[c["program_id"]]
            h.append(f"<tr><td><a href='{esc(c['source_url'])}'>{esc(p.get('program_name_ru') or c['program_id'])}</a> "
                     f"({esc(p.get('jurisdiction'))})</td><td>{esc(c['type'])}</td><td>{esc(c['summary'])}</td></tr>")
        for pid in new_ids:
            p = programs[pid]
            h.append(f"<tr><td><a href='{esc(p.get('source_url'))}'>{esc(p.get('program_name_ru') or pid)}</a> "
                     f"({esc(p.get('jurisdiction'))})</td><td>новая программа в базе</td><td></td></tr>")
        h.append("</table>")
    else:
        h.append("<p>Изменений стоимости, сроков и программ по перепроверенным позициям не выявлено.</p>")

    # 4. Пути допуска по юрисдикциям
    h.append("<h2>4. Путь к статусу адвоката по юрисдикциям</h2>")
    for z in ZONES:
        js = [j for j in jurisdictions.values() if j.get("zone") == z]
        if not js:
            continue
        h.append(f"<h3>{esc(ZONE_NAMES[z])}</h3>")
        h.append("<table><tr><th>Юрисдикция</th><th>Статус</th><th>Этапы</th><th>Минимальный срок</th><th>Регулятор</th></tr>")
        for j in sorted(js, key=lambda x: x["jurisdiction"]):
            h.append(f"<tr><td>{esc(j.get('jurisdiction_name_ru'))}</td><td>{esc(j.get('profession_title_original'))}</td>"
                     f"<td>{esc(j.get('admission_path_ru'))}</td><td>{esc(j.get('total_min_duration_ru'))}</td>"
                     f"<td><a href='{esc(j.get('source_url'))}'>{esc(j.get('regulator_original'))}</a></td></tr>")
        h.append("</table>")

    # 5. Программы (только в полной версии; в письме — вложение, чтобы Gmail не обрезал текст >102 КБ)
    sec5_start = len(h)
    h.append("<h2>5. Программы: планы, форматы, сроки, стоимость</h2>")
    for z in ZONES:
        ps = [p for p in programs.values() if p.get("zone") == z]
        if not ps:
            continue
        h.append(f"<h3>{esc(ZONE_NAMES[z])} — {len(ps)}</h3>")
        h.append("<table><tr><th>Юрисдикция / провайдер</th><th>Программа и учебный план</th><th>Формат, длительность</th>"
                 "<th>Стоимость</th><th>Требования</th></tr>")
        for p in sorted(ps, key=lambda x: (x.get("jurisdiction") or "", x["program_id"])):
            h.append(
                f"<tr><td>{esc(p.get('jurisdiction'))}<br>{esc(p.get('provider_ru') or p.get('provider_original'))}</td>"
                f"<td><b><a href='{esc(p.get('source_url'))}'>{esc(p.get('program_name_ru') or p.get('program_name_original'))}</a></b>"
                f" — {esc(STAGE_RU.get(p.get('stage'), p.get('stage')))}, {esc((p.get('mandatory') or '').replace('_', ' '))}"
                f"<br>{esc(p.get('curriculum_ru'))}{mark(p, 'curriculum_ru')}"
                + (f"<br><i>Изменения: {esc(p.get('upcoming_changes_ru'))}</i>" if p.get("upcoming_changes_ru") else "")
                + (f"<br>Сдали: {esc(p['pass_rate'].get('value_percent'))}% ({esc(p['pass_rate'].get('period_ru'))})"
                   if isinstance(p.get("pass_rate"), dict) and p["pass_rate"].get("value_percent") is not None else "")
                + f"</td><td>{esc('; '.join(x for x in (p.get('format'), p.get('duration_ru')) if x) or 'не установлено')}{mark(p, 'duration_ru')}</td>"
                f"<td>{cost_cell(p, fx)}"
                + (f"<br><i>{esc(p.get('funding_ru'))}</i>" if p.get("funding_ru") else "")
                + f"</td><td>{esc(p.get('entry_requirements_ru'))}</td></tr>")
        h.append("</table>")

    sec5_end = len(h)
    # 6. Покрытие
    h.append("<h2>6. Полнота проверки</h2>")
    for z in ZONES:
        h.append(f"<p><b>{z}.</b> {esc(coverage.get(z) or 'нет данных')}</p>")
    if problems:
        h.append("<p><b>Сбои:</b> " + esc("; ".join(problems)) + "</p>")
    h.append(f"<p class='small'>К перепроверке на следующей неделе: {len(stale)} программ. "
             "Карточки программ (раздел 5) — во вложении report_full.html; таблица — во вложении programs.csv (Excel).</p>")
    h.append("</body></html>")
    (out / "report_full.html").write_text("\n".join(h), encoding="utf-8")
    short = h[:sec5_start] + ["<h2>5. Программы: планы, форматы, сроки, стоимость</h2>",
                              f"<p>Карточки {len(programs)} программ — во вложении report_full.html.</p>"] + h[sec5_end:]
    (out / "report.html").write_text("\n".join(short), encoding="utf-8")
    (out / "subject.txt").write_text(subject, encoding="utf-8")

    txt = ([args.warning, ""] if args.warning else []) + [subject, "", "Главное за неделю:"]
    txt += [f"- [{e.get('significance')}] {e.get('jurisdiction')} {e.get('date')}: {e.get('summary_ru')} ({e.get('source_url')})"
            for e in key_events] or ["- значимых событий нет"]
    txt += ["", f"Ближайшие даты ({HORIZON_DAYS} дн.):"]
    txt += [f"- {d:%d.%m.%Y} {p.get('jurisdiction')} {p.get('program_name_ru') or p['program_id']}: {label}"
            + (f" ({n})" if n else "") for d, p, label, n in up] or ["- нет"]
    txt += ["", "Полный отчёт — во вложении report_full.html; таблица программ — во вложении programs.csv."]
    (out / "report.txt").write_text("\n".join(txt), encoding="utf-8")

    print(json.dumps({"subject": subject, "programs": len(programs), "events": len(events),
                      "changes": len(changes), "new_programs": len(new_ids), "upcoming": len(up),
                      "recheck": len(stale), "problems": problems}, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-date", required=True)
    ap.add_argument("--period-start", required=True)
    ap.add_argument("--period-end", required=True)
    ap.add_argument("--zones-dir", required=True)
    ap.add_argument("--baseline", default=str(HERE / "data" / "baseline.json"))
    ap.add_argument("--fx", default=None)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--trial", action="store_true")
    ap.add_argument("--warning", default=None, help="строка «МОНИТОРИНГ ВЫПОЛНЕН ЧАСТИЧНО: …» в начало письма")
    build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
