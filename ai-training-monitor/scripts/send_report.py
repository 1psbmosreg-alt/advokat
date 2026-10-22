#!/usr/bin/env python3
"""Запасная отправка отчёта по SMTP.

Получатель берётся только из переменной окружения MAIL_TO (агент его не задаёт).
Нужны: SMTP_HOST, SMTP_PORT (465 — SSL, иначе STARTTLS), SMTP_USER, SMTP_PASS, MAIL_TO;
необязательно MAIL_FROM (по умолчанию SMTP_USER).

  send_report.py <отчёт.html> "<тема>"
"""
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    need = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        print("ОТПРАВКА НЕ ВЫПОЛНЕНА: не заданы " + ", ".join(missing))
        return 2
    to = os.environ["MAIL_TO"]
    if "," in to or ";" in to:
        print("ОТПРАВКА НЕ ВЫПОЛНЕНА: MAIL_TO должен содержать один адрес")
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        body = f.read()
    msg = EmailMessage()
    msg["Subject"] = sys.argv[2]
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = to
    msg.set_content("Отчёт в формате HTML.")
    msg.add_alternative(body, subtype="html")
    port = int(os.environ["SMTP_PORT"])
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx, timeout=60)
    else:
        s = smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=60)
        s.starttls(context=ctx)
    with s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print("ОТПРАВЛЕНО:", to)
    return 0


if __name__ == "__main__":
    sys.exit(main())
