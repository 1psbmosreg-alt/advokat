#!/usr/bin/env python3
"""Замер времени ответа и кода ответа сайтов, запись в CSV.

Режим по умолчанию: один замер всех адресов из targets.csv и выход
(запускать из cron раз в 10 минут). Режим --loop: сам повторяет замеры
с заданным интервалом в течение заданного срока.

Только стандартная библиотека Python 3.9+.
"""

import argparse
import csv
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UA = "site-load-monitor/1.0 (latency study; 1 request per 10 min)"
MAX_REDIRECTS = 5
MAX_BODY = 5 * 1024 * 1024
ROBOTS_TTL = 24 * 3600

FIELDS = [
    "ts_utc", "ts_msk", "weekday_msk", "hour_msk", "probe_id", "target", "url",
    "final_url", "status", "redirects", "dns_ms", "connect_ms", "tls_ms",
    "ttfb_ms", "total_ms", "bytes", "error",
]


def load_targets(path):
    targets = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, url = [p.strip() for p in line.split(",", 1)]
            targets.append((name, url))
    return targets


def proxy_for(scheme, host):
    if urllib.request.proxy_bypass_environment(host):
        return None
    for key in (f"{scheme}_proxy", f"{scheme.upper()}_PROXY"):
        if os.environ.get(key):
            return urllib.parse.urlsplit(os.environ[key])
    return None


def fetch_once(url, ua, timeout):
    """Один запрос без следования редиректам. Возвращает dict с таймингами."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    proxy = proxy_for(u.scheme, host)
    r = {"dns_ms": None, "connect_ms": None, "tls_ms": None}

    t0 = time.perf_counter()
    if proxy:
        # Через прокси DNS резолвит прокси; время DNS не измеряется.
        sock = socket.create_connection((proxy.hostname, proxy.port), timeout=timeout)
        t_conn = time.perf_counter()
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        line = resp.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200" not in line:
            sock.close()
            raise OSError(f"proxy CONNECT refused: {line}")
        r["connect_ms"] = (time.perf_counter() - t0) * 1000
    else:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        t_dns = time.perf_counter()
        r["dns_ms"] = (t_dns - t0) * 1000
        family, stype, proto, _, addr = infos[0]
        sock = socket.socket(family, stype, proto)
        sock.settimeout(timeout)
        sock.connect(addr)
        t_conn = time.perf_counter()
        r["connect_ms"] = (t_conn - t_dns) * 1000

    if u.scheme == "https":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
        t_tls = time.perf_counter()
        r["tls_ms"] = (t_tls - t_conn) * 1000

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.sock = sock
    t_req = time.perf_counter()
    conn.request("GET", path, headers={
        "Host": host, "User-Agent": ua, "Accept": "text/html,*/*",
        "Accept-Encoding": "identity", "Connection": "close",
    })
    resp = conn.getresponse()
    t_first = time.perf_counter()
    body = resp.read(MAX_BODY)
    t_end = time.perf_counter()
    conn.close()

    r.update(
        status=resp.status,
        location=resp.getheader("Location"),
        ttfb_ms=(t_first - t_req) * 1000,
        total_ms=(t_end - t0) * 1000,
        bytes=len(body),
    )
    return r


def measure(url, ua, timeout):
    """Запрос со следованием редиректам. Тайминги — последнего запроса,
    total_ms — сумма по всей цепочке."""
    total, redirects, current = 0.0, 0, url
    while True:
        r = fetch_once(current, ua, timeout)
        total += r["total_ms"]
        if r["status"] in (301, 302, 303, 307, 308) and r["location"] and redirects < MAX_REDIRECTS:
            current = urllib.parse.urljoin(current, r["location"])
            redirects += 1
            continue
        r.update(total_ms=total, redirects=redirects, final_url=current)
        return r


def robots_allowed(url, ua, cache_path, timeout):
    """Проверка robots.txt с кэшем на сутки (чтобы не удваивать число запросов)."""
    u = urllib.parse.urlsplit(url)
    robots_url = f"{u.scheme}://{u.netloc}/robots.txt"
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    entry = cache.get(robots_url)
    if not entry or time.time() - entry["fetched"] > ROBOTS_TTL:
        try:
            status, text = fetch_text(robots_url, ua, timeout)
            entry = {"fetched": time.time(), "status": status, "text": text}
        except Exception as e:  # сеть недоступна — не блокируем замер, но и не кэшируем
            return True, f"robots.txt недоступен: {e}"
        cache[robots_url] = entry
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    if entry["status"] != 200:
        return True, ""
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(entry["text"].splitlines())
    return rp.can_fetch(ua, url), ""


def fetch_text(url, ua, timeout, redirects=0):
    """GET с телом (для robots.txt). Возвращает (status, text)."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    proxy = proxy_for(u.scheme, host)
    if proxy:
        conn = cls(proxy.hostname, proxy.port, timeout=timeout)
        conn.set_tunnel(host, port)
    else:
        conn = cls(host, port, timeout=timeout)
    conn.request("GET", u.path or "/", headers={"User-Agent": ua})
    resp = conn.getresponse()
    text = resp.read(MAX_BODY).decode("utf-8", errors="replace")
    conn.close()
    location = resp.getheader("Location")
    if resp.status in (301, 302, 303, 307, 308) and location and redirects < MAX_REDIRECTS:
        return fetch_text(urllib.parse.urljoin(url, location), ua, timeout, redirects + 1)
    return resp.status, text


def run_round(targets, args):
    now = datetime.now(timezone.utc)
    msk = now.astimezone(MSK)
    new_file = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
    cache_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), ".robots_cache.json")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    rows = []
    for name, url in targets:
        row = dict.fromkeys(FIELDS, "")
        row.update(
            ts_utc=now.isoformat(timespec="seconds"), ts_msk=msk.isoformat(timespec="seconds"),
            weekday_msk=msk.isoweekday(), hour_msk=msk.hour, probe_id=args.probe_id,
            target=name, url=url,
        )
        allowed, note = robots_allowed(url, args.user_agent, cache_path, args.timeout)
        if not allowed:
            row["error"] = "disallowed by robots.txt; not requested"
            rows.append(row)
            continue
        try:
            r = measure(url, args.user_agent, args.timeout)
            for k in ("final_url", "status", "redirects", "bytes"):
                row[k] = r[k]
            for k in ("dns_ms", "connect_ms", "tls_ms", "ttfb_ms", "total_ms"):
                row[k] = "" if r[k] is None else round(r[k], 1)
            row["error"] = note
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"[:300]
        rows.append(row)

    with open(args.out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    for row in rows:
        print(f"{row['ts_msk']} {row['target']}: status={row['status'] or '-'} "
              f"ttfb={row['ttfb_ms'] or '-'}ms total={row['total_ms'] or '-'}ms {row['error']}")


def first_measurement(path):
    try:
        with open(path, encoding="utf-8") as f:
            row = next(csv.DictReader(f), None)
    except OSError:
        return None
    return datetime.fromisoformat(row["ts_utc"]) if row else None


def parse_duration(s):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(s[:-1]) * units[s[-1]] if s[-1] in units else int(s)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--targets", default=os.path.join(HERE, "targets.csv"))
    p.add_argument("--out", default=os.path.join(HERE, "data", "measurements.csv"))
    p.add_argument("--probe-id", default=socket.gethostname(),
                   help="метка точки замера, например msk-vps или cloud-eu")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--until", help="не замерять после этого момента (ISO, время МСК), "
                                   "например 2026-11-01T00:00 — для cron")
    p.add_argument("--max-days", type=float,
                   help="не замерять, если с первой строки --out прошло столько суток "
                        "(для запуска по расписанию без фиксированной даты окончания)")
    p.add_argument("--loop", action="store_true", help="повторять замеры самостоятельно")
    p.add_argument("--interval", default="10m", help="интервал в режиме --loop (10m, 1h)")
    p.add_argument("--duration", default="7d", help="срок в режиме --loop (7d)")
    args = p.parse_args()

    if args.until:
        until = datetime.fromisoformat(args.until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=MSK)
        if datetime.now(timezone.utc) >= until:
            print("Срок мониторинга истёк (--until); замер не выполняется.")
            return 0

    if args.max_days:
        first = first_measurement(args.out)
        if first and datetime.now(timezone.utc) - first >= timedelta(days=args.max_days):
            print(f"Прошло {args.max_days:g} дн. с первого замера ({first:%Y-%m-%d %H:%M} UTC); "
                  "замер не выполняется.")
            return 0

    targets = load_targets(args.targets)
    if not args.loop:
        run_round(targets, args)
        return 0

    interval = parse_duration(args.interval)
    end = time.time() + parse_duration(args.duration)
    print(f"Мониторинг до {datetime.fromtimestamp(end, MSK):%Y-%m-%d %H:%M} МСК, "
          f"интервал {interval} с")
    next_run = time.time()
    while next_run < end:
        run_round(targets, args)
        next_run += interval
        time.sleep(max(0, next_run - time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
