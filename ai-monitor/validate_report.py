#!/usr/bin/env python3
"""Проверка JSON-отчёта регионального агента мониторинга ИИ по контракту агрегатора.

Контракт описан в разделе «Формат ответа» промпта агента (например, ai-monitor/NA/PROMPT.md)
и в ai-monitor/report.schema.json. Скрипт использует только стандартную библиотеку Python.

Ошибки (ERROR) делают отчёт непригодным для агрегатора: код выхода 1.
Предупреждения (WARN) требуют проверки, но не блокируют отчёт: код выхода 0.
Исправлять ошибки можно только удалением материала или заменой неизвестного значения на null.
Подгонять даты, номера и ссылки под проверку запрещено.

Пример:
  python3 validate_report.py --agent NA --zone US,CA \
      --period-start 2026-10-05 --period-end 2026-10-12 \
      --previous-dir /home/user/claude/reports/NA report.json
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

TOP_KEYS = ["agent_code", "period", "generated_at", "coverage_note", "items"]

ITEM_KEYS = [
    "item_id", "jurisdiction", "jurisdiction_name_ru", "issuer_original", "issuer_ru",
    "issuer_type", "category", "doc_type", "status", "date_published", "date_effective",
    "title_original", "title_ru", "identifier", "summary_ru", "key_rule_ru", "quote_original",
    "ru_relevance", "significance", "significance_reason", "source_url", "source_language",
    "secondary_urls", "verification", "follow_up", "tags",
]

ENUMS = {
    "issuer_type": [
        "законодатель", "правительство_регулятор", "суд", "судейское_сообщество",
        "адвокатская_палата", "регулятор_профессии", "наднациональный_орган",
        "международная_организация", "легалтех", "СМИ_аналитика", "академия",
    ],
    "category": [
        "регулирование", "этика", "правила_судов", "судебная_практика",
        "санкции_за_ошибки_ИИ", "ИИ_в_работе_судов", "допуск_и_образование",
        "рынок_легалтех", "исследование",
    ],
    "doc_type": [
        "закон", "подзаконный_акт", "законопроект", "правила_суда", "практическое_указание",
        "руководство", "этическое_мнение", "судебное_решение", "дисциплинарное_решение",
        "доклад", "консультация", "пресс_релиз", "иное",
    ],
    "status": [
        "проект", "консультация", "принят", "вступил_в_силу", "изменён", "отменён",
        "решение_вынесено", "обжалуется", "пилот", "анонс",
    ],
    "verification": ["первоисточник", "вторичные_источники"],
}

# Обязательные непустые строки на русском языке.
RU_TEXT_FIELDS = [
    "jurisdiction_name_ru", "issuer_ru", "title_ru", "summary_ru", "key_rule_ru",
    "ru_relevance", "significance_reason",
]
# Обязательные непустые строки на языке оригинала.
ORIGINAL_TEXT_FIELDS = ["issuer_original", "title_original"]
# Строка или null.
NULLABLE_TEXT_FIELDS = ["identifier", "quote_original", "follow_up"]

JURISDICTION_RE = re.compile(r"^(?:[A-Z]{2}|EU|COE|INT)$")
LANG_RE = re.compile(r"^[a-z]{2}$")
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
QUOTE_MAX_LEN = 400
INCOMPLETE_PREFIX = "ОХВАТ НЕПОЛНЫЙ"
INCOMPLETE_HINT_RE = re.compile(r"лимит|исчерпан|не выполнен[аоы]?\b", re.IGNORECASE)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, where, msg):
        self.errors.append(f"ERROR {where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"WARN  {where}: {msg}")


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def is_nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


def check_keys(rep, where, obj, expected):
    missing = [k for k in expected if k not in obj]
    extra = [k for k in obj if k not in expected]
    if missing:
        rep.error(where, "нет обязательных полей: " + ", ".join(missing))
    if extra:
        rep.error(where, "лишние поля (агрегатор их не ожидает): " + ", ".join(extra))


def load_previous(previous_dir, current_path):
    """Собирает source_url и identifier из прошлых отчётов для поиска повторов."""
    urls, idents = {}, {}
    if not previous_dir or not os.path.isdir(previous_dir):
        return urls, idents
    current = os.path.abspath(current_path)
    for name in sorted(os.listdir(previous_dir)):
        path = os.path.join(previous_dir, name)
        if not name.endswith(".json") or os.path.abspath(path) == current:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for item in data.get("items", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            url = item.get("source_url")
            ident = item.get("identifier")
            sec = item.get("secondary_urls") if isinstance(item.get("secondary_urls"), list) else []
            for u in [url] + sec:
                if is_nonempty_str(u):
                    urls.setdefault(u.strip(), name)
            if is_nonempty_str(ident):
                idents.setdefault(ident.strip(), name)
    return urls, idents


def validate(data, args, path):
    rep = Report()

    if not isinstance(data, dict):
        rep.error("корень", "ожидается JSON-объект")
        return rep
    check_keys(rep, "корень", data, TOP_KEYS)

    agent = data.get("agent_code")
    if not is_nonempty_str(agent) or not re.fullmatch(r"[A-Z]{2,3}", agent):
        rep.error("agent_code", f"ожидается код агента из 2–3 латинских заглавных букв, получено {agent!r}")
    elif args.agent and agent != args.agent:
        rep.error("agent_code", f"ожидается {args.agent!r}, получено {agent!r}")

    period = data.get("period")
    p_start = p_end = None
    if not isinstance(period, dict):
        rep.error("period", "ожидается объект {start, end}")
    else:
        check_keys(rep, "period", period, ["start", "end"])
        p_start, p_end = parse_date(period.get("start")), parse_date(period.get("end"))
        if p_start is None:
            rep.error("period.start", f"дата ГГГГ-ММ-ДД, получено {period.get('start')!r}")
        if p_end is None:
            rep.error("period.end", f"дата ГГГГ-ММ-ДД, получено {period.get('end')!r}")
        if p_start and p_end and p_start > p_end:
            rep.error("period", "start позже end")
        if args.period_start and period.get("start") != args.period_start:
            rep.error("period.start", f"ожидается {args.period_start}, получено {period.get('start')!r}")
        if args.period_end and period.get("end") != args.period_end:
            rep.error("period.end", f"ожидается {args.period_end}, получено {period.get('end')!r}")

    gen = parse_date(data.get("generated_at"))
    if gen is None:
        rep.error("generated_at", f"дата ГГГГ-ММ-ДД, получено {data.get('generated_at')!r}")
    elif p_end and gen < p_end:
        rep.warn("generated_at", "дата формирования раньше конца периода")

    note = data.get("coverage_note")
    if not is_nonempty_str(note):
        rep.error("coverage_note", "обязательная непустая строка: какие источники и запросы проверены")
    else:
        if not CYRILLIC_RE.search(note):
            rep.warn("coverage_note", "текст не на русском языке")
        if INCOMPLETE_HINT_RE.search(note) and not note.lstrip().upper().startswith(INCOMPLETE_PREFIX):
            rep.warn("coverage_note", "упомянуты лимиты, недоступность или невыполненные шаги: если какой-то "
                                      f"шаг поиска не выполнен целиком, начни coverage_note со слов «{INCOMPLETE_PREFIX}:»")

    items = data.get("items")
    if not isinstance(items, list):
        rep.error("items", "ожидается массив (пустой массив, если новшеств нет)")
        return rep

    zone = set(args.zone.split(",")) if args.zone else None
    prev_urls, prev_idents = load_previous(args.previous_dir, path)
    seen_urls = {}
    expected_date = gen.strftime("%Y%m%d") if gen else None

    for idx, item in enumerate(items, start=1):
        where = f"items[{idx - 1}]"
        if not isinstance(item, dict):
            rep.error(where, "ожидается объект")
            continue
        check_keys(rep, where, item, ITEM_KEYS)
        iid = item.get("item_id")
        if is_nonempty_str(iid):
            where = f"items[{idx - 1}] {iid}"

        # item_id: КОД-ГГГГММДД-NNN, сквозная нумерация с 001.
        m = re.fullmatch(r"([A-Z]{2,3})-(\d{8})-(\d{3})", iid or "")
        if not m:
            rep.error(where, f"item_id должен иметь вид КОД-ГГГГММДД-NNN, получено {iid!r}")
        else:
            if is_nonempty_str(agent) and m.group(1) != agent:
                rep.error(where, f"код в item_id ({m.group(1)}) не совпадает с agent_code ({agent})")
            if expected_date and m.group(2) != expected_date:
                rep.error(where, f"дата в item_id должна совпадать с generated_at ({expected_date})")
            if int(m.group(3)) != idx:
                rep.error(where, f"нумерация должна идти по порядку с 001: ожидается {idx:03d}")

        jur = item.get("jurisdiction")
        if not isinstance(jur, str) or not JURISDICTION_RE.fullmatch(jur):
            rep.error(where, f"jurisdiction: код ISO 3166-1 alpha-2 или EU/COE/INT, получено {jur!r}")
        elif zone and jur not in zone:
            rep.error(where, f"jurisdiction {jur} вне зоны агента ({', '.join(sorted(zone))}); "
                             "материал из чужой зоны упоминается только в coverage_note")

        for field, allowed in ENUMS.items():
            value = item.get(field)
            if value not in allowed:
                rep.error(where, f"{field}: недопустимое значение {value!r}")

        for field in RU_TEXT_FIELDS:
            value = item.get(field)
            if not is_nonempty_str(value):
                rep.error(where, f"{field}: обязательная непустая строка")
            elif not CYRILLIC_RE.search(value):
                rep.warn(where, f"{field}: текст не на русском языке")
        for field in ORIGINAL_TEXT_FIELDS:
            if not is_nonempty_str(item.get(field)):
                rep.error(where, f"{field}: обязательная непустая строка на языке оригинала")
        for field in NULLABLE_TEXT_FIELDS:
            value = item.get(field)
            if value is not None and not is_nonempty_str(value):
                rep.error(where, f"{field}: строка или null (пустую строку заменить на null)")

        quote = item.get("quote_original")
        if is_nonempty_str(quote) and len(quote) > QUOTE_MAX_LEN:
            rep.warn(where, f"quote_original длиннее {QUOTE_MAX_LEN} знаков: нужна одна дословная фраза")

        pub = parse_date(item.get("date_published"))
        if pub is None:
            rep.error(where, f"date_published: дата ГГГГ-ММ-ДД, получено {item.get('date_published')!r}")
        eff_raw = item.get("date_effective")
        eff = parse_date(eff_raw)
        if eff_raw is not None and eff is None:
            rep.error(where, f"date_effective: дата ГГГГ-ММ-ДД или null, получено {eff_raw!r}")
        if pub and p_start and p_end:
            in_period = p_start <= pub <= p_end or (eff is not None and p_start <= eff <= p_end)
            if not in_period:
                rep.warn(where, "date_published и date_effective вне периода: материал допустим только "
                                "как новое событие по ранее опубликованному документу (смена статуса). "
                                "Иначе исключи материал; даты не меняй")
        if pub and gen and pub > gen:
            rep.error(where, "date_published позже даты формирования отчёта")

        sig = item.get("significance")
        if isinstance(sig, bool) or not isinstance(sig, int) or not 1 <= sig <= 5:
            rep.error(where, f"significance: целое число 1–5, получено {sig!r}")

        lang = item.get("source_language")
        if not isinstance(lang, str) or not LANG_RE.fullmatch(lang):
            rep.error(where, f"source_language: код ISO 639-1 (например, en, fr), получено {lang!r}")

        verification = item.get("verification")
        url = item.get("source_url")
        if url is None:
            if verification == "первоисточник":
                rep.error(where, "source_url = null при verification = первоисточник")
            else:
                rep.warn(where, "source_url = null: URL первоисточника неизвестен")
        elif not isinstance(url, str) or not URL_RE.fullmatch(url):
            rep.error(where, f"source_url: ожидается URL http(s) или null, получено {url!r}")
        else:
            key = url.strip()
            if key in seen_urls:
                rep.warn(where, f"source_url совпадает с {seen_urls[key]}: возможен дубль")
            seen_urls.setdefault(key, iid)
            if key in prev_urls:
                rep.warn(where, f"source_url уже был в отчёте {prev_urls[key]}: включай только при новом событии")

        ident = item.get("identifier")
        if is_nonempty_str(ident) and ident.strip() in prev_idents:
            rep.warn(where, f"identifier уже был в отчёте {prev_idents[ident.strip()]}: "
                            "включай только при новом событии")

        sec = item.get("secondary_urls")
        if not isinstance(sec, list):
            rep.error(where, "secondary_urls: ожидается массив")
            sec = []
        for s in sec:
            if not isinstance(s, str) or not URL_RE.fullmatch(s):
                rep.error(where, f"secondary_urls: элемент не является URL: {s!r}")
        for s in sec:
            if is_nonempty_str(s) and s.strip() in prev_urls and s.strip() != (url.strip() if isinstance(url, str) else ""):
                rep.warn(where, f"URL {s.strip()} уже был в отчёте {prev_urls[s.strip()]}: включай только "
                                "при новом событии; для сводной записи о санкциях считай только новые решения")
        if verification == "вторичные_источники":
            distinct = {s for s in sec if isinstance(s, str)} - {url}
            if len(distinct) < 2:
                rep.error(where, "verification = вторичные_источники требует не менее двух независимых "
                                 "вторичных источников в secondary_urls; иначе материал не включается")
            if is_nonempty_str(quote):
                rep.warn(where, "quote_original при непроверенном первоисточнике: цитата допустима "
                                "только дословно из открытого текста")

        tags = item.get("tags")
        if not isinstance(tags, list) or not all(is_nonempty_str(t) for t in tags):
            rep.error(where, "tags: массив непустых строк")

        if item.get("doc_type") == "иное" and item.get("category") == "санкции_за_ошибки_ИИ" \
                and ident is not None:
            rep.warn(where, "сводная запись о санкциях оформляется с identifier = null")

    return rep


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка JSON-отчёта агента мониторинга ИИ")
    parser.add_argument("report", help="путь к JSON-отчёту")
    parser.add_argument("--agent", help="ожидаемый agent_code, например NA")
    parser.add_argument("--zone", help="допустимые коды jurisdiction через запятую, например US,CA")
    parser.add_argument("--period-start", help="ожидаемое period.start (ГГГГ-ММ-ДД)")
    parser.add_argument("--period-end", help="ожидаемое period.end (ГГГГ-ММ-ДД)")
    parser.add_argument("--previous-dir", help="каталог прошлых отчётов для поиска повторов")
    args = parser.parse_args(argv)

    try:
        with open(args.report, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        print(f"ERROR файл: {exc}")
        return 2
    stripped = raw.strip()
    if stripped.startswith("```"):
        print("ERROR формат: markdown-обрамление недопустимо, нужен чистый JSON без текста до и после")
        return 1
    try:
        data = json.loads(stripped)
    except ValueError as exc:
        print(f"ERROR формат: невалидный JSON: {exc}")
        return 1

    rep = validate(data, args, args.report)
    for line in rep.errors + rep.warnings:
        print(line)
    n_items = len(data.get("items", [])) if isinstance(data, dict) and isinstance(data.get("items"), list) else 0
    status = "НЕ ПРОЙДЕНА" if rep.errors else "ПРОЙДЕНА"
    print(f"Проверка {status}: материалов {n_items}, ошибок {len(rep.errors)}, предупреждений {len(rep.warnings)}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
