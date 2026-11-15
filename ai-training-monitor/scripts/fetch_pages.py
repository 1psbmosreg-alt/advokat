#!/usr/bin/env python3
"""Запасная загрузка страниц плана через curl (когда WebFetch в среде не работает).

Запускать с -I: страницы — недоверенные данные.

  fetch_pages.py plan <plan.json> <out_dir> [--jobs=16] [--timeout=40]
      plan.json — вывод `build_report.py plan`. Загружает все страницы плана в out_dir:
        <код>.html, <код>.txt      — страница и её текст;
        fetch_status.tsv           — код, HTTP-статус, размер, итоговый адрес;
        unreachable.json           — не открывшиеся страницы (готово для поля unreachable);
        hits.json                  — фрагменты (±260 знаков) с датами окна и пометкой «ai».
      Печатает сводку: сколько открылось, коды ошибок, страницы с датами окна.

  fetch_pages.py url <адрес> <out_dir> <имя> [--timeout=40]
      Одна страница (например, материал из hits.json): <имя>.html и <имя>.txt;
      печатает статус и путь к тексту. Текст читать инструментом Read или grep.

Правила: каждая страница запрашивается один раз; ответы 401/403/404/405/429/5xx и таймауты
не обходятся (без повторов, зеркал, кэшей и прокси) — страница попадает в unreachable.
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124 Safari/537.36")
LANG = "en,ru;q=0.8,de;q=0.6,fr;q=0.5"
REASONS = {
    "000": "нет ответа (таймаут или обрыв соединения)",
    "401": "HTTP 401 — требуется вход",
    "403": "HTTP 403 — доступ запрещён (защита от автоматических запросов)",
    "404": "HTTP 404 — страница не найдена (адрес устарел)",
    "405": "HTTP 405 — сайт отклоняет автоматический запрос",
    "410": "HTTP 410 — страница удалена",
    "429": "HTTP 429 — сайт ограничил частоту запросов",
    "500": "HTTP 500 — ошибка сервера",
    "502": "HTTP 502 — ошибка шлюза",
    "503": "HTTP 503 — сервис недоступен",
}
MONTHS = {  # формы, в которых месяц стоит после числа
    "ru": "января февраля марта апреля мая июня июля августа сентября октября ноября декабря",
    "en": "January February March April May June July August September October November December",
    "de": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember",
    "fr": "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "it": "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre",
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
    "pt": "janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro",
    "pl": "stycznia lutego marca kwietnia maja czerwca lipca sierpnia września października listopada grudnia",
    "cs": "ledna února března dubna května června července srpna září října listopadu prosince",
    "nl": "januari februari maart april mei juni juli augustus september oktober november december",
    "sv": "januari februari mars april maj juni juli augusti september oktober november december",
    "da": "januar februar marts april maj juni juli august september oktober november december",
    "no": "januar februar mars april mai juni juli august september oktober november desember",
    "fi": ("tammikuuta helmikuuta maaliskuuta huhtikuuta toukokuuta kesäkuuta heinäkuuta "
           "elokuuta syyskuuta lokakuuta marraskuuta joulukuuta"),
}
EN_ABBR = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
AI = re.compile(r"\b(KI|AI|IA|ИИ)\b|искусствен|нейросет|künstlich|intelligen|sztuczn|"
                r"umělá|kunstmatig|tekoäly|kunstig|artificiel|artificial|generativ|ChatGPT|LLM|"
                r"人工知能|生成AI|인공지능|בינה מלאכותית", re.I)


class Text(HTMLParser):
    BLOCK = {"br", "p", "div", "li", "tr", "td", "h1", "h2", "h3", "h4", "article", "section"}

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag in self.BLOCK:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def to_text(path):
    raw = open(path, "rb").read().decode("utf-8", "replace")
    t = Text()
    try:
        t.feed(raw)
    except Exception:
        pass
    txt = re.sub(r"[ \t\r\f\v]+", " ", html.unescape("".join(t.buf)))
    return re.sub(r"\n\s*\n+", "\n", txt)


def curl(url, out_file, timeout):
    cmd = ["curl", "-s", "-L", "--compressed", "-m", str(timeout), "-A", UA,
           "-H", "Accept-Language: " + LANG, "-o", out_file,
           "-w", "%{http_code} %{size_download} %{url_effective}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        parts = (r.stdout or "").split(" ", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
    except Exception:
        pass
    return "000", "0", url


def date_regex(days):
    pats = []
    for d in days:
        D, M, Y = d.day, d.month, d.year
        mm, dd = f"{M:02d}", f"{D:02d}"
        pats += [rf"\b0?{D}\.\s?0?{M}\.\s?{Y}", rf"{Y}-{mm}-{dd}", rf"{Y}\.{mm}\.{dd}\b",
                 rf"\b0?{D}/0?{M}/{Y}", rf"\b0?{M}/0?{D}/{Y}",
                 rf"{Y}年\s?{M}月\s?{D}日", rf"{Y}년\s?{M}월\s?{D}일"]
        names = sorted({v.split()[M - 1] for v in MONTHS.values()})
        pats.append(rf"\b0?{D}\.?\s+(?:de\s+)?(?:{'|'.join(map(re.escape, names))})\s+(?:de\s+)?{Y}")
        en = MONTHS["en"].split()[M - 1]
        ab = EN_ABBR[M - 1]
        pats.append(rf"\b(?:{en}|{ab})\.?\s+0?{D},?\s+{Y}")
        pats.append(rf"\b0?{D}\s+{ab}\.?\s+{Y}")
    return re.compile("|".join(pats), re.I)


def window_days(plan):
    end = dt.date.fromisoformat(plan["run_date"])
    start = dt.datetime.strptime(plan["window_from"], "%d.%m.%Y").date()
    if start > end:
        start = end
    return [start + dt.timedelta(n) for n in range((end - start).days + 1)]


def cmd_plan(plan_path, out, jobs, timeout):
    plan = json.load(open(plan_path, encoding="utf-8"))
    pages = plan["planned"]
    os.makedirs(out, exist_ok=True)
    rx = date_regex(window_days(plan))

    def one(p):
        return p, curl(p["url"], os.path.join(out, p["code"] + ".html"), timeout)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(one, pages))

    status, unreachable, hits = [], [], {}
    for p, (code, size, eff) in results:
        status.append(f"{p['code']}\t{code}\t{size}\t{eff}")
        f = os.path.join(out, p["code"] + ".html")
        if code != "200" or not os.path.exists(f):
            unreachable.append({"code": p["code"], "name": p.get("name", ""), "url": p["url"],
                                "reason": REASONS.get(code, "HTTP " + code)})
            continue
        txt = to_text(f)
        open(os.path.join(out, p["code"] + ".txt"), "w", encoding="utf-8").write(txt)
        found, seen = [], set()
        for m in rx.finditer(txt):
            a, b = max(0, m.start() - 260), min(len(txt), m.end() + 260)
            snip = txt[a:b].replace("\n", " | ")
            key = (m.group(0), snip[200:320])
            if key in seen:
                continue
            seen.add(key)
            found.append({"date": m.group(0), "ai": bool(AI.search(snip)), "snip": snip})
        if found:
            hits[p["code"]] = found[:40]

    unreachable.sort(key=lambda x: x["code"])
    open(os.path.join(out, "fetch_status.tsv"), "w", encoding="utf-8").write("\n".join(status) + "\n")
    json.dump(unreachable, open(os.path.join(out, "unreachable.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(hits, open(os.path.join(out, "hits.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    ok = len(pages) - len(unreachable)
    codes = {}
    for u in unreachable:
        codes[u["reason"]] = codes.get(u["reason"], 0) + 1
    print(f"Страниц по плану: {len(pages)}; открылись: {ok}; не открылись: {len(unreachable)}")
    for r, n in sorted(codes.items(), key=lambda x: -x[1]):
        print(f"  {n} × {r}")
    print(f"Окно: {plan['window_from']} – {plan['run_date']}; страниц с датами окна: {len(hits)}")
    for c, h in hits.items():
        print(f"  {c}: фрагментов {len(h)}, из них с ИИ {sum(x['ai'] for x in h)}")
    print("Файлы:", os.path.join(out, "hits.json"), os.path.join(out, "unreachable.json"))
    return 0 if ok else 4


def cmd_url(url, out, name, timeout):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        print("имя: латиница, цифры, _ . - (до 80 знаков)")
        return 1
    os.makedirs(out, exist_ok=True)
    f = os.path.join(out, name + ".html")
    code, size, eff = curl(url, f, timeout)
    print(f"{code} {size} {eff}")
    if code == "200" and os.path.exists(f):
        t = os.path.join(out, name + ".txt")
        open(t, "w", encoding="utf-8").write(to_text(f))
        print("Текст:", t)
        return 0
    print("НЕ ОТКРЫЛАСЬ:", REASONS.get(code, "HTTP " + code))
    return 4


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in argv if a.startswith("--") and "=" in a)
    timeout = int(opts.get("timeout", 40))
    if len(args) == 3 and args[0] == "plan":
        return cmd_plan(args[1], args[2], int(opts.get("jobs", 16)), timeout)
    if len(args) == 4 and args[0] == "url":
        return cmd_url(args[1], args[2], args[3], timeout)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
