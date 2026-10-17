#!/usr/bin/env python3
"""Преобразование JSON-отчёта агента мониторинга ИИ в текст письма.

Текст собирается только из полей отчёта: скрипт ничего не добавляет от себя,
поэтому письмо не может содержать сведений, которых нет в проверенном JSON.

  python3 render_report.py report.json            # тело письма (простой текст)
  python3 render_report.py --subject report.json  # тема письма (одна строка)
"""

import argparse
import json
import sys

AGENT_TITLES = {
    "NA": "США и Канада",
    "CL": "общее право: Великобритания, Ирландия, Австралия, Новая Зеландия, Залив",
}

CATEGORY_TITLES = [
    ("санкции_за_ошибки_ИИ", "Санкции за ошибки ИИ"),
    ("регулирование", "Регулирование"),
    ("правила_судов", "Правила судов"),
    ("судебная_практика", "Судебная практика"),
    ("этика", "Профессиональная этика"),
    ("ИИ_в_работе_судов", "ИИ в работе судов"),
    ("допуск_и_образование", "Допуск к профессии и образование"),
    ("рынок_легалтех", "Рынок легалтех"),
    ("исследование", "Исследования"),
]

VERIFICATION_TEXT = {
    "первоисточник": "по первоисточнику",
    "вторичные_источники": "по двум и более вторичным источникам; первоисточник не открыт — сверить по ссылке",
}

INCOMPLETE_PREFIX = "ОХВАТ НЕПОЛНЫЙ"

FOOTER = (
    "Отчёт сформирован автоматически по проверенному JSON-отчёту агента. "
    "Это инструмент мониторинга, а не источник для ссылок: перед использованием любого акта, "
    "решения или мнения в документе для суда откройте первоисточник и проверьте актуальность."
)


def label(agent):
    region = AGENT_TITLES.get(agent)
    return f"{region} (агент {agent})" if region else f"агент {agent}"


def incomplete(data):
    return str(data.get("coverage_note") or "").lstrip().upper().startswith(INCOMPLETE_PREFIX)


def subject(data):
    items = data.get("items") or []
    high = sum(1 for i in items if isinstance(i.get("significance"), int) and i["significance"] >= 4)
    text = (f"ИИ в юридической практике: {label(data.get('agent_code', '?'))} — "
            f"отчёт {data.get('generated_at', '?')}: материалов {len(items)}, значимость 4–5: {high}")
    return text + "; охват неполный" if incomplete(data) else text


def render_item(n, item):
    lines = []
    head = f"{n}. [{item.get('item_id')}] значимость {item.get('significance')}/5 · {item.get('jurisdiction_name_ru')}"
    lines.append(head)
    lines.append(f"   {item.get('title_ru')}")
    lines.append(f"   ({item.get('title_original')})")
    lines.append(f"   Орган: {item.get('issuer_ru')} ({item.get('issuer_original')})")
    lines.append(f"   Вид: {item.get('doc_type')}; статус: {item.get('status')}")
    dates = f"   Дата: {item.get('date_published')}"
    if item.get("date_effective"):
        dates += f"; вступает в силу / действует с: {item['date_effective']}"
    lines.append(dates)
    if item.get("identifier"):
        lines.append(f"   Реквизиты: {item['identifier']}")
    lines.append(f"   Суть: {item.get('summary_ru')}")
    lines.append(f"   Главное правило: {item.get('key_rule_ru')}")
    if item.get("quote_original"):
        lines.append(f"   Цитата (оригинал): «{item['quote_original']}»")
    lines.append(f"   Применимость для РФ: {item.get('ru_relevance')}")
    lines.append(f"   Почему {item.get('significance')}/5: {item.get('significance_reason')}")
    lines.append(f"   Проверено: {VERIFICATION_TEXT.get(item.get('verification'), item.get('verification'))}")
    lines.append(f"   Источник: {item.get('source_url') or 'URL первоисточника не установлен'}")
    for url in item.get("secondary_urls") or []:
        lines.append(f"   Доп. источник: {url}")
    if item.get("follow_up"):
        lines.append(f"   Отслеживать: {item['follow_up']}")
    if item.get("tags"):
        lines.append(f"   Теги: {', '.join(item['tags'])}")
    return "\n".join(lines)


def body(data):
    items = data.get("items") or []
    period = data.get("period") or {}
    out = [
        f"ИИ в юридической практике и правосудии: {label(data.get('agent_code', '?'))}",
        f"Период: {period.get('start')} — {period.get('end')}. Сформирован: {data.get('generated_at')}. "
        f"Материалов: {len(items)}.",
        "",
    ]
    if incomplete(data):
        out.append("ВНИМАНИЕ: проверка выполнена не полностью — что не проверено, указано "
                   "в разделе «Охват проверки».")
        out.append("")
    if not items:
        if incomplete(data):
            out.append("Подтверждённых новшеств не найдено. Проверка неполная, поэтому это не означает, "
                       "что новшеств за период не было.")
        else:
            out.append("За период новшеств не найдено.")
        out.append("")
    else:
        by_sig = sorted(items, key=lambda i: -(i.get("significance") or 0))
        top = [i for i in by_sig if (i.get("significance") or 0) >= 4]
        if top:
            out.append("ГЛАВНОЕ (значимость 4–5)")
            for item in top:
                out.append(f"• [{item.get('item_id')}] {item.get('title_ru')} — {item.get('key_rule_ru')}")
            out.append("")
        n = 0
        known = {code for code, _ in CATEGORY_TITLES}
        groups = CATEGORY_TITLES + [(None, "Прочее")]
        for code, title in groups:
            group = [i for i in by_sig if (i.get("category") == code if code else i.get("category") not in known)]
            if not group:
                continue
            out.append(title.upper())
            out.append("")
            for item in group:
                n += 1
                out.append(render_item(n, item))
                out.append("")
    out.append("ОХВАТ ПРОВЕРКИ")
    out.append(str(data.get("coverage_note") or ""))
    out.append("")
    out.append(FOOTER)
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="JSON-отчёт агента → текст письма")
    parser.add_argument("report")
    parser.add_argument("--subject", action="store_true", help="вывести только тему письма")
    args = parser.parse_args(argv)
    with open(args.report, encoding="utf-8") as fh:
        data = json.load(fh)
    sys.stdout.write(subject(data) + "\n" if args.subject else body(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
