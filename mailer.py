# -*- coding: utf-8 -*-
"""Рассылка долгов врачам через корпоративный SMTP.
Поддерживает режим DRYRUN (ничего не отправляет, только логирует)."""
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
import appconfig as cfg


def _cfg():
    try:
        port = int(cfg.get("SMTP_PORT", "587") or 587)
    except ValueError:
        port = 587
    return {
        "host": cfg.get("SMTP_HOST", ""),
        "port": port,
        "user": cfg.get("SMTP_USER", ""),
        "password": cfg.get("SMTP_PASS", ""),
        "from_addr": cfg.get("SMTP_FROM", "") or cfg.get("SMTP_USER", ""),
        "from_name": cfg.get("SMTP_FROM_NAME", "ГБУЗ ПК «Осинская ЦРБ» — мониторинг СЭМД"),
        "tls": cfg.get("SMTP_TLS", "1") != "0",
        "dryrun": cfg.get("SMTP_DRYRUN", "0") == "1",
    }


def configured():
    c = _cfg()
    return bool(c["host"] and c["from_addr"])


def is_dryrun():
    return _cfg()["dryrun"]


def build_debt_html(vrach, debts):
    rows = "".join(
        f"<tr><td>{i}</td><td>{d.get('patient','')}</td><td>{d.get('case_no','')}</td>"
        f"<td>{d.get('doc_type','')}</td><td>{d.get('d_start','')}</td></tr>"
        for i, d in enumerate(debts, 1)
    )
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Уважаемый(ая) {vrach}!</p>
<p>У вас <b>{len(debts)}</b> неподписанных медицинских документов, подлежащих регистрации в РЭМД.
Просьба подписать их в ЕИСЗ ПК в ближайшее время — без подписи документы не передаются в федеральный реестр,
что влияет на показатели учреждения.</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>№</th><th>Пациент</th><th>№ случая</th><th>Вид документа</th><th>Дата</th></tr>
{rows}
</table>
<p style="color:#666;font-size:12px">Письмо сформировано автоматически системой мониторинга СЭМД.
Отдел информационных технологий.</p>
</body></html>"""


def build_dept_html(podr, vrachi, total_nepodp):
    rows = "".join(
        f"<tr><td>{i}</td><td>{v['vrach']}</td><td style='text-align:right'>{v['nepodp']}</td>"
        f"<td style='text-align:right'>{v['debts']}</td></tr>"
        for i, v in enumerate(vrachi, 1)
    )
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Уважаемый(ая) заведующий(ая) подразделением «{podr}»!</p>
<p>По вашему подразделению в ЕИСЗ ПК <b>{total_nepodp}</b> неподписанных медицинских документов,
подлежащих регистрации в РЭМД. Просьба организовать подписание силами врачей подразделения —
неподписанные документы не передаются в федеральный реестр и снижают показатели учреждения.</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>№</th><th>Врач</th><th>Не подписано</th><th>Долгов (документов)</th></tr>
{rows}
</table>
<p style="color:#666;font-size:12px">Письмо сформировано автоматически системой мониторинга СЭМД.
Отдел информационных технологий.</p>
</body></html>"""


def send(to_addr, subject, html):
    """Отправляет письмо. Возвращает (ok, message)."""
    c = _cfg()
    if c["dryrun"]:
        return True, "dryrun (не отправлено)"
    if not c["host"] or not c["from_addr"]:
        return False, "SMTP не настроен"
    if not to_addr:
        return False, "нет адреса"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((c["from_name"], c["from_addr"]))
    msg["To"] = to_addr
    msg.set_content("Просмотрите письмо в HTML-формате.")
    msg.add_alternative(html, subtype="html")
    try:
        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=30) as s:
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as s:
                if c["tls"]:
                    s.starttls()
                if c["user"]:
                    s.login(c["user"], c["password"])
                s.send_message(msg)
        return True, "отправлено"
    except Exception as e:
        return False, f"ошибка: {e}"
