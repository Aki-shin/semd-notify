# -*- coding: utf-8 -*-
"""Рассылка долгов врачам через корпоративный SMTP.
Поддерживает режим DRYRUN (ничего не отправляет, только логирует)."""
import time
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
    # ПДн-минимизация: в письмо НЕ включаем ФИО пациента и дату рождения.
    # Документ идентифицируется по № случая — врач находит его в ЕИСЗ ПК.
    body = []
    for i, d in enumerate(debts, 1):
        start, end = d.get("d_start", ""), d.get("d_end", "")
        period = start if (not end or end == start) else f"{start} – {end}"
        body.append(
            f"<tr><td>{i}</td><td>{d.get('case_no','')}</td>"
            f"<td>{d.get('doc_type','')}</td><td>{period}</td></tr>"
        )
    rows = "".join(body)
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Уважаемый(ая) {vrach}!</p>
<p>У вас <b>{len(debts)}</b> неподписанных медицинских документов, подлежащих регистрации в РЭМД.
Просьба подписать их в ЕИСЗ ПК в ближайшее время — без подписи документы не передаются в федеральный реестр,
что влияет на показатели учреждения.</p>
<p style="color:#444">Документы перечислены <b>по номеру случая</b> (без персональных данных пациентов).
Откройте случай в ЕИСЗ ПК по указанному номеру, чтобы найти и подписать документ.</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>№</th><th>№ случая</th><th>Вид документа</th><th>Период случая</th></tr>
{rows}
</table>
<p style="color:#666;font-size:12px">Письмо сформировано автоматически системой мониторинга СЭМД.
Отдел информационных технологий.</p>
</body></html>"""


def build_dept_html(podr, vrachi, total_nepodp, total_debts=0, from_debts=False):
    rows = "".join(
        f"<tr><td>{i}</td><td>{v['vrach']}</td><td style='text-align:right'>{v['nepodp']}</td>"
        f"<td style='text-align:right'>{v['debts']}</td></tr>"
        for i, v in enumerate(vrachi, 1)
    )
    if from_debts:
        intro = (f"По отделению «{podr}» в ЕИСЗ ПК <b>{total_debts}</b> медицинских документов, "
                 "подлежащих регистрации в РЭМД и не подписанных (по списку неподписанных документов). "
                 "Просьба организовать подписание силами врачей отделения — "
                 "неподписанные документы не передаются в федеральный реестр и снижают показатели учреждения.")
    else:
        intro = (f"По вашему подразделению в ЕИСЗ ПК <b>{total_nepodp}</b> неподписанных медицинских документов, "
                 "подлежащих регистрации в РЭМД. Просьба организовать подписание силами врачей подразделения — "
                 "неподписанные документы не передаются в федеральный реестр и снижают показатели учреждения.")
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Уважаемый(ая) заведующий(ая) подразделением «{podr}»!</p>
<p>{intro}</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>№</th><th>Врач</th><th>Не подписано</th><th>Долгов (документов)</th></tr>
{rows}
</table>
<p style="color:#666;font-size:12px">Письмо сформировано автоматически системой мониторинга СЭМД.
Отдел информационных технологий.</p>
</body></html>"""


def build_report_html(data):
    """Отчёт ответственному за исправление: проблемы, не привязанные к конкретному врачу."""
    f = data.get("funnel", {})
    errs = data.get("errors", []) or []
    un = data.get("unassigned", []) or []
    docerr = data.get("docerr", []) or []
    mo_gap = data.get("mo_gap")
    err_rows = "".join(
        f"<tr><td>{e['code']}</td><td style='text-align:right'>{e['c']}</td></tr>" for e in errs[:15]
    ) or "<tr><td colspan='2'>нет данных (отчёт ФЛК не загружен)</td></tr>"
    un_rows = "".join(
        f"<tr><td>{u['vrach']}</td><td>{u['doc_type']}</td><td style='text-align:right'>{u['nepodp']}</td></tr>"
        for u in un
    ) or "<tr><td colspan='3'>нет</td></tr>"
    de_rows = "".join(
        f"<tr><td>{d['doc_type']}</td><td style='text-align:right'>{d['not_found']}</td>"
        f"<td style='text-align:right'>{d['validation']}</td><td style='text-align:right'>{d['position']}</td></tr>"
        for d in docerr[:15]
    )
    de_block = (f"""<h3>Ошибки по видам документов</h3>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>Вид документа</th><th>Не найдена запись справочника</th><th>Ошибка валидации</th><th>Должность</th></tr>
{de_rows}
</table>""") if docerr else ""
    mo_block = (f"<p>Подписаны врачом, но <b>не подписаны МО</b> (застряли на подписи МО): "
                f"<b>{mo_gap}</b> — это автоподписание МО, не вина врачей.</p>") if mo_gap else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Отчёт по проблемам передачи документов в РЭМД, <b>требующим вмешательства</b>
(не привязаны к конкретному врачу — рассылкой врачам не закрываются).</p>
<p>Сформировано: <b>{f.get('sform',0)}</b> · подписано: {f.get('podp',0)} ({f.get('pct_podp',0)}%) ·
в РЭМД: {f.get('zareg',0)} ({f.get('pct_zareg',0)}%).</p>
{mo_block}
<h3>Документы без указанного врача</h3>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>Врач</th><th>Вид документа</th><th>Не подписано</th></tr>
{un_rows}
</table>
<h3>Ошибки регистрации (ФЛК) — топ кодов</h3>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>Код ошибки</th><th>Кол-во</th></tr>
{err_rows}
</table>
{de_block}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД.</p>
</body></html>"""


def report_recipient():
    """(имя, e-mail) ответственного за исправление ошибок."""
    return cfg.get("RESP_NAME", ""), cfg.get("RESP_EMAIL", "")


def _batch_cfg():
    def _i(key, default):
        try:
            return int(cfg.get(key, str(default)) or default)
        except ValueError:
            return default
    return {"delay": _i("SMTP_BATCH_DELAY", 2),   # сек между письмами
            "size": _i("SMTP_BATCH_SIZE", 25),     # пачка: после N — пауза
            "pause": _i("SMTP_BATCH_PAUSE", 30)}   # пауза между пачками, сек


def _connect(c):
    if c["port"] == 465:
        s = smtplib.SMTP_SSL(c["host"], c["port"], timeout=30)
    else:
        s = smtplib.SMTP(c["host"], c["port"], timeout=30)
        if c["tls"]:
            s.starttls()
    if c["user"]:
        s.login(c["user"], c["password"])
    return s


def _build_msg(c, to_addr, subject, html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((c["from_name"], c["from_addr"]))
    msg["To"] = to_addr
    msg.set_content("Просмотрите письмо в HTML-формате.")
    msg.add_alternative(html, subtype="html")
    return msg


def send(to_addr, subject, html):
    """Отправляет одно письмо. Возвращает (ok, message)."""
    c = _cfg()
    if c["dryrun"]:
        return True, "dryrun (не отправлено)"
    if not c["host"] or not c["from_addr"]:
        return False, "SMTP не настроен"
    if not to_addr:
        return False, "нет адреса"
    try:
        s = _connect(c)
        try:
            s.send_message(_build_msg(c, to_addr, subject, html))
        finally:
            try:
                s.quit()
            except Exception:
                pass
        return True, "отправлено"
    except Exception as e:
        return False, f"ошибка: {e}"


def send_batch(items, on_result=None):
    """Пакетная рассылка: одно SMTP-соединение + троттлинг (защита от спам-блокировки).
    items — список dict {to, subject, html, ...}. on_result(item, ok, msg) вызывается на каждое письмо.
    Возвращает (отправлено, ошибок)."""
    c = _cfg()
    if c["dryrun"]:
        for it in items:
            if on_result:
                on_result(it, True, "dryrun (не отправлено)")
        return len(items), 0
    if not c["host"] or not c["from_addr"]:
        for it in items:
            if on_result:
                on_result(it, False, "SMTP не настроен")
        return 0, len(items)
    bc = _batch_cfg()
    sent = failed = since_pause = 0
    server = None
    try:
        for idx, it in enumerate(items):
            to = (it.get("to") or "").strip()
            if not to:
                failed += 1
                if on_result:
                    on_result(it, False, "нет адреса")
                continue
            msg = _build_msg(c, to, it["subject"], it["html"])
            ok, m = False, ""
            try:
                if server is None:
                    server = _connect(c)
                server.send_message(msg)
                ok, m = True, "отправлено"
            except Exception as e:
                try:  # одна попытка переподключения
                    if server is not None:
                        try:
                            server.quit()
                        except Exception:
                            pass
                    server = _connect(c)
                    server.send_message(msg)
                    ok, m = True, "отправлено (переподключение)"
                except Exception as e2:
                    ok, m, server = False, f"ошибка: {e2}", None
            sent += 1 if ok else 0
            failed += 0 if ok else 1
            if on_result:
                on_result(it, ok, m)
            # троттлинг между письмами
            if idx < len(items) - 1:
                since_pause += 1
                if bc["size"] and since_pause >= bc["size"]:
                    time.sleep(bc["pause"])
                    since_pause = 0
                elif bc["delay"]:
                    time.sleep(bc["delay"])
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return sent, failed
