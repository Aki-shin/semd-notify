# -*- coding: utf-8 -*-
"""Рассылка долгов врачам через корпоративный SMTP.
Поддерживает режим DRYRUN (ничего не отправляет, только логирует)."""
import time
import html
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
import appconfig as cfg


def _custom_block(text):
    """Произвольный текст оператора (из Настроек), добавляемый в письмо.
    Экранируется как простой текст; переводы строк → <br>."""
    if not text or not str(text).strip():
        return ""
    safe = html.escape(str(text).strip()).replace("\n", "<br>")
    return ('<div style="margin:14px 0;padding:10px 12px;background:#f3f6fb;'
            'border-left:3px solid #1e3a5f;font-size:13px;color:#333">' + safe + "</div>")


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


def build_debt_html(vrach, debts, rep_period="", custom=""):
    # ПДн-минимизация: в письмо НЕ включаем ФИО пациента.
    # Документ идентифицируется по № случая и дате рождения — врач находит его в ЕИСЗ ПК.
    body = []
    for i, d in enumerate(debts, 1):
        start, end = d.get("d_start", ""), d.get("d_end", "")
        # Период случая — как в исходном отчёте: всегда «начало – окончание»,
        # в т. ч. для случая одного дня (15.06.2026 – 15.06.2026). Одна дата — только если конца нет.
        cp = f"{start} – {end}" if end else start
        body.append(
            f"<tr><td>{i}</td><td>{d.get('case_no','')}</td>"
            f"<td>{d.get('birth','')}</td>"
            f"<td>{d.get('doc_type','')}</td><td>{cp}</td></tr>"
        )
    rows = "".join(body)
    per = f" за период <b>{rep_period}</b> —" if rep_period else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Уважаемый(ая) {vrach}!</p>
<p>У вас{per} <b>{len(debts)}</b> неподписанных медицинских документов, подлежащих регистрации в РЭМД.
Просьба подписать их в ЕИСЗ ПК в ближайшее время — без подписи документы не передаются в федеральный реестр,
что влияет на показатели учреждения.</p>
<p style="color:#444">Документы перечислены <b>по номеру случая и дате рождения пациента</b> (без указания ФИО).
Откройте случай в ЕИСЗ ПК по указанному номеру, чтобы найти и подписать документ.</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>№</th><th>№ случая</th><th>Дата рождения</th><th>Вид документа</th><th>Период случая</th></tr>
{rows}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Письмо сформировано автоматически системой мониторинга СЭМД.
Отдел информационных технологий.</p>
</body></html>"""


def build_dept_html(podr, vrachi, total_nepodp, rep_period="", custom=""):
    rows = "".join(
        f"<tr><td>{i}</td><td>{v['vrach']}</td><td style='text-align:right'>{v['nepodp']}</td></tr>"
        for i, v in enumerate(vrachi, 1)
    )
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    intro = (f"По вашему подразделению{per} в ЕИСЗ ПК <b>{total_nepodp}</b> неподписанных медицинских документов, "
             "подлежащих регистрации в РЭМД. Просьба организовать подписание силами врачей подразделения — "
             "неподписанные документы не передаются в федеральный реестр и снижают показатели учреждения.")
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Уважаемый(ая) заведующий(ая) подразделением «{podr}»!</p>
<p>{intro}</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>№</th><th>Врач</th><th>Не подписано</th></tr>
{rows}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Письмо сформировано автоматически системой мониторинга СЭМД.
Отдел информационных технологий.</p>
</body></html>"""


def build_report_html(data, custom=""):
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
    per = data.get("period", "")
    per_line = f"<p>Период отчёта: <b>{per}</b>.</p>" if per else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Отчёт по проблемам передачи документов в РЭМД, <b>требующим вмешательства</b>
(не привязаны к конкретному врачу — рассылкой врачам не закрываются).</p>
{per_line}
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
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД.</p>
</body></html>"""


def build_fap_report_html(s, rows, rep_period="", custom=""):
    """Полная статистика работы фельдшеров ФАП в ЭМК — ответственному за ФАП."""
    def td(v):
        return f"<td style='text-align:right'>{v}</td>"
    body = "".join(
        f"<tr><td>{r['fap']}</td><td>{r['fio']}</td>"
        + td(r['visits']) + td(r['visits_doc']) + td(str(r['pct']) + '%')
        + td(r['naprav']) + td(r['recipes']) + td(r['naznach'])
        + td(r['eln']) + td(r.get('telemed', 0)) + td(r.get('er', 0)) + "</tr>"
        for r in rows
    ) or "<tr><td colspan='11'>нет данных</td></tr>"
    total = (f"<tr style='font-weight:bold;background:#eef'><td colspan='2'>ИТОГО ({s.get('n',0)})</td>"
             + td(s.get('visits', 0)) + td(s.get('visits_doc', 0)) + td(str(s.get('pct', 0)) + '%')
             + td(s.get('naprav', 0)) + td(s.get('recipes', 0)) + td(s.get('naznach', 0))
             + td(s.get('eln', 0)) + td(s.get('telemed', 0)) + td(s.get('er', 0)) + "</tr>")
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:13px;color:#222">
<p>Статистика работы фельдшеров ФАП в электронной медицинской карте (ЭМК){per}.</p>
<p>Фельдшеров: <b>{s.get('n',0)}</b> · посещений: <b>{s.get('visits',0)}</b> ·
с документами: <b>{s.get('visits_doc',0)}</b> ({s.get('pct',0)}% заполнения ЭМК).</p>
<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:12px">
<tr style="background:#1e3a5f;color:#fff">
<th>ФАП</th><th>Фельдшер</th><th>Посещ.</th><th>С док.</th><th>% ЭМК</th>
<th>Направл.</th><th>Рецепты</th><th>Назнач.</th><th>ЭЛН</th><th>Телемед</th><th>Записи ЭР</th></tr>
{body}
{total}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД.</p>
</body></html>"""


def _koiki_rows_html(wards, show_resp=False):
    def c(v):
        return "—" if v is None else v
    out = []
    for w in wards:
        zan = w.get("zan")
        if w.get("no_beds"):
            note = "коек нет в справочнике"
        elif w.get("overload") and not w.get("day"):
            note = "проверить коечный фонд"
        else:
            note = ""
        zt = "—" if zan is None else f"{zan}%"
        # синий — план перевыполнен (>100%); красный — ниже 80%; иначе обычный
        if zan is None:
            color = "#222"
        elif zan > 100:
            color = "#2563eb"
        elif zan < 80:
            color = "#c0392b"
        else:
            color = "#222"
        resp_td = f"<td style='font-size:12px'>{w.get('resp','') or '—'}</td>" if show_resp else ""
        out.append(
            f"<tr><td>{w['otdelenie']}</td>{resp_td}"
            f"<td style='text-align:right'>{w['koek']}</td>"
            f"<td style='text-align:right'>{w['kd']}</td>"
            f"<td style='text-align:right;color:{color}'><b>{zt}</b></td>"
            f"<td style='text-align:right'>{c(w.get('oborot'))}</td>"
            f"<td style='text-align:right'>{c(w.get('dlit'))}</td>"
            f"<td style='font-size:12px;color:#777'>{note}</td></tr>")
    return "".join(out) or "<tr><td colspan='8'>нет данных</td></tr>"


_KOIKI_NOTE = ("Занятость = койко-дни ÷ (число коек × дни периода). "
               "<b style='color:#2563eb'>Синим</b> — занятость выше 100 % (план по койко-дням перевыполнен; "
               "для круглосуточных коек это также может означать, что коек в справочнике Промед меньше "
               "фактически развёрнутых — стоит проверить). "
               "<b style='color:#c0392b'>Красным</b> — ниже 80 % (недозагрузка коек). "
               "Оборот койки = выписано ÷ коек; средняя длительность = койко-дни ÷ выписано.")


def build_koiki_resp_html(resp, wards, days, rep_period="", custom=""):
    """Занятость коек по отделениям конкретного ответственного (заведующего)."""
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    who = f"Уважаемый(ая) {resp}!" if resp else "Уважаемый коллега!"
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>{who}</p>
<p>Показатели <b>занятости коек</b> по закреплённым за вами отделениям{per} (в периоде {days} дн.).</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#1e3a5f;color:#fff"><th>Отделение</th><th>Коек</th><th>Койко-дни</th>
<th>Занятость</th><th>Оборот</th><th>Ср. длит.</th><th>Примечание</th></tr>
{_koiki_rows_html(wards)}
</table>
<p style="color:#555;font-size:12px">{_KOIKI_NOTE}</p>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически. Отдел информационных технологий.</p>
</body></html>"""


def build_koiki_overall_html(wards, totals, rep_period="", custom=""):
    """Общий сводный отчёт по занятости коек — ответственному за коечный фонд."""
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    t = totals
    def line(lbl, a):
        z = "—" if a["zan"] is None else f"{a['zan']}%"
        return f"<tr><td>{lbl}</td><td style='text-align:right'>{a['koek']}</td><td style='text-align:right'>{a['kd']}</td><td style='text-align:right'><b>{z}</b></td></tr>"
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Сводный отчёт по <b>занятости коечного фонда</b>{per} (в периоде {t['days']} дн.).</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px;margin-bottom:10px">
<tr style="background:#eef"><th>Категория</th><th>Коек</th><th>Койко-дни</th><th>Занятость</th></tr>
{line('Всего по учреждению', t['all'])}
{line('Круглосуточные койки', t['kruglo'])}
{line('Дневные стационары (места)', t['day'])}
</table>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#1e3a5f;color:#fff"><th>Отделение</th><th>Ответственный</th><th>Коек</th><th>Койко-дни</th>
<th>Занятость</th><th>Оборот</th><th>Ср. длит.</th><th>Примечание</th></tr>
{_koiki_rows_html(wards, show_resp=True)}
</table>
<p style="color:#555;font-size:12px">{_KOIKI_NOTE}</p>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически. Отдел информационных технологий.</p>
</body></html>"""


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


def send_batch(items, on_result=None, cancel=None):
    """Пакетная рассылка: одно SMTP-соединение + троттлинг (защита от спам-блокировки).
    items — список dict {to, subject, html, ...}. on_result(item, ok, msg) вызывается на каждое письмо.
    cancel — функция без аргументов; если вернёт True, рассылка прерывается.
    Возвращает (отправлено, ошибок)."""
    c = _cfg()
    if c["dryrun"]:
        n = 0
        for it in items:
            if cancel and cancel():
                break
            if on_result:
                on_result(it, True, "dryrun (не отправлено)")
            n += 1
        return n, 0
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
            if cancel and cancel():
                break
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
