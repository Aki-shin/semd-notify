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


def build_dept_report_html(depts, rep_period="", custom=""):
    """Сводный отчёт по подписанию СЭМД в разрезе подразделений — ответственному."""
    tot = {"sform": 0, "podp": 0, "nepodp": 0}
    body = []
    for i, d in enumerate(depts, 1):
        tot["sform"] += d["sform"]; tot["podp"] += d["podp"]; tot["nepodp"] += d["nepodp"]
        body.append(
            f"<tr><td>{i}</td><td>{d['podr']}</td>"
            f"<td style='text-align:right'>{d['sform']}</td>"
            f"<td style='text-align:right'>{d['podp']}</td>"
            f"<td style='text-align:right'>{d['pct']}%</td>"
            f"<td style='text-align:right'>{d['nepodp']}</td>"
            f"<td style='text-align:right'>{len(d['vrachi'])}</td></tr>")
    rows = "".join(body) or "<tr><td colspan='7'>нет данных</td></tr>"
    pct = round(100 * tot["podp"] / tot["sform"], 1) if tot["sform"] else 0
    total = ("<tr style='font-weight:bold;background:#eef'><td colspan='2'>ИТОГО</td>"
             f"<td style='text-align:right'>{tot['sform']}</td>"
             f"<td style='text-align:right'>{tot['podp']}</td>"
             f"<td style='text-align:right'>{pct}%</td>"
             f"<td style='text-align:right'>{tot['nepodp']}</td><td></td></tr>")
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Сводка по подписанию медицинских документов (СЭМД) в разрезе подразделений{per}.</p>
<p>Сформировано: <b>{tot['sform']}</b> · подписано: {tot['podp']} ({pct}%) ·
не подписано: <b>{tot['nepodp']}</b>.</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#1e3a5f;color:#fff"><th>№</th><th>Подразделение</th><th>Сформировано</th>
<th>Подписано</th><th>%</th><th>Не подписано</th><th>Врачей с неподп.</th></tr>
{rows}
{total}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД.
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


def build_emd_err_report_html(win_label, totals, errors, by_podr, by_vrach, custom=""):
    """Отчёт ответственному об ошибках регистрации ЭМД — из витрины первички.
    Без ПДн пациентов (политика: полные данные — только в менеджере); конкретные
    документы с пациентами — на странице «ЭМД → Аналитика»."""
    err_rows = "".join(
        f"<tr><td><code>{html.escape(e['err_code'])}</code></td>"
        f"<td>{html.escape(e['err_type'] or '—')}</td>"
        f"<td style='font-size:12px;color:#555'>{html.escape((e['sample'] or '')[:160])}</td>"
        f"<td style='text-align:right'><b>{e['n']}</b></td></tr>"
        for e in errors[:20]
    ) or "<tr><td colspan='4'>ошибок за период нет</td></tr>"
    podr_rows = "".join(
        f"<tr><td>{html.escape(p['podr'] or '—')}</td><td style='text-align:right'>{p['err']}</td></tr>"
        for p in by_podr if p.get("err")
    ) or "<tr><td colspan='2'>нет</td></tr>"
    vr_rows = "".join(
        f"<tr><td>{html.escape(v['v'])}</td><td style='text-align:right'>{v['n']}</td></tr>"
        for v in by_vrach
    ) or "<tr><td colspan='2'>нет</td></tr>"
    share = round(100 * totals["err"] / totals["n"], 1) if totals["n"] else 0
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>Сводка по <b>ошибкам регистрации ЭМД в РЭМД</b> за период <b>{win_label}</b>
(по витрине первички; конкретные документы и пациенты — в системе, страница «ЭМД → Аналитика»).</p>
<p>Документов, подписанных МО: <b>{totals['n']}</b> · зарегистрировано: {totals['reg']} ·
<b style="color:#b91c1c">с ошибкой: {totals['err']}</b> ({share}%) ·
в работе: {totals['ready'] + totals['sent']}.</p>
<h3>Ошибки по кодам</h3>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>Код</th><th>Тип</th><th>Пример сообщения</th><th>Док-в</th></tr>
{err_rows}
</table>
<h3>По подразделениям</h3>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>Подразделение</th><th>Ошибок</th></tr>
{podr_rows}
</table>
<h3>По врачам (топ)</h3>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#eef"><th>Врач</th><th>Ошибок</th></tr>
{vr_rows}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД (витрина первички РЭМД).</p>
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


def build_max_report_html(totals, by_doctor, by_purpose, rep_period="", custom=""):
    """Сводный отчёт по ТМК через чат-бот MAX — ответственному за цифровизацию/ТМК.
    Доля через MAX — показатель цифровизации; цвет: синий ≥80 %, красный <50 %."""
    t = totals or {}
    def pct_td(v):
        if v is None:
            return "<td style='text-align:right;color:#999'>—</td>"
        col = "#2563eb" if v >= 80 else ("#c0392b" if v < 50 else "#222")
        return f"<td style='text-align:right;color:{col}'>{v}%</td>"
    def td(v):
        return f"<td style='text-align:right'>{v}</td>"
    drows = "".join(
        f"<tr><td>{d['doctor']}</td><td style='font-size:11px;color:#555'>{d.get('position','')}</td>"
        + td(d['zap']) + td(d['zap_max']) + pct_td(d['zap_pct'])
        + td(d['prov']) + td(d['prov_max']) + pct_td(d['prov_pct'])
        + td(d['otm']) + td(d['bl_max']) + "</tr>"
        for d in by_doctor
    ) or "<tr><td colspan='10'>нет данных</td></tr>"
    dtot = ("<tr style='font-weight:bold;background:#eef'><td colspan='2'>ИТОГО</td>"
            + td(t.get('zap', 0)) + td(t.get('zap_max', 0)) + pct_td(t.get('zap_pct'))
            + td(t.get('prov', 0)) + td(t.get('prov_max', 0)) + pct_td(t.get('prov_pct'))
            + td(t.get('otm', 0)) + td(t.get('bl_max', 0)) + "</tr>")
    prows = "".join(
        f"<tr><td>{c['purpose']}</td>"
        + td(c['zap']) + td(c['zap_max']) + pct_td(c['zap_pct'])
        + td(c['prov']) + pct_td(c['prov_pct']) + td(c['bl_max']) + "</tr>"
        for c in by_purpose
    ) or "<tr><td colspan='7'>нет данных</td></tr>"
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:13px;color:#222">
<p>Сводный отчёт по <b>телемедицинским консультациям (ТМК)</b> и использованию <b>чат-бота MAX</b>{per}.</p>
<p>Записи на ТМК: всего <b>{t.get('zap',0)}</b>, через MAX <b>{t.get('zap_max',0)}</b> ({t.get('zap_pct')}%) ·
проведено ТМК: <b>{t.get('prov',0)}</b>, через MAX <b>{t.get('prov_max',0)}</b> ({t.get('prov_pct')}%) ·
отменено: <b>{t.get('otm',0)}</b> · больничных закрыто через MAX: <b>{t.get('bl_max',0)}</b> ·
врачей ведут ТМК: <b>{t.get('n_doctors',0)}</b>.</p>
<p style="color:#555">Доля через MAX — показатель цифровизации (проникновение чат-бота):
<b style="color:#2563eb">синим</b> ≥ 80 %, <b style="color:#c0392b">красным</b> — ниже 50 %.</p>

<h3 style="margin:14px 0 4px">По врачам</h3>
<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:12px">
<tr style="background:#1e3a5f;color:#fff"><th>Врач</th><th>Должность</th>
<th>Зап.</th><th>&rarr;MAX</th><th>%</th><th>Пров.</th><th>&rarr;MAX</th><th>%</th><th>Отмен.</th><th>Больн.MAX</th></tr>
{drows}
{dtot}
</table>

<h3 style="margin:16px 0 4px">По целям консультации</h3>
<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:12px">
<tr style="background:#1e3a5f;color:#fff"><th>Цель консультации</th>
<th>Зап.</th><th>&rarr;MAX</th><th>%</th><th>Пров.</th><th>%</th><th>Больн.MAX</th></tr>
{prows}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД.</p>
</body></html>"""


def build_xray_report_html(totals, rows, rep_period="", custom=""):
    """Сводный отчёт по обработке лучевых исследований сервисом ИИ — ответственному
    за цифровизацию/лучевую диагностику. Успешность: синий ≥90 %, красный <70 %."""
    t = totals or {}
    def td(v):
        return f"<td style='text-align:right'>{v}</td>"
    def bad_td(v):
        col = ";color:#c0392b;font-weight:bold" if v else ""
        return f"<td style='text-align:right{col}'>{v}</td>"
    def succ_td(v):
        if v is None:
            return "<td style='text-align:right;color:#999'>—</td>"
        col = "#2563eb" if v >= 90 else ("#c0392b" if v < 70 else "#222")
        return f"<td style='text-align:right;color:{col};font-weight:bold'>{v}%</td>"
    def err_td(v):
        if v is None:
            return "<td style='text-align:right;color:#999'>—</td>"
        col = ";color:#c0392b" if v >= 10 else ""
        return f"<td style='text-align:right{col}'>{v}%</td>"
    body = "".join(
        f"<tr><td><b>{r['modality']}</b></td>" + td(r['total']) + td(r['success'])
        + succ_td(r['pct_success']) + bad_td(r['err']) + err_td(r['pct_err'])
        + bad_td(r['err_mi']) + bad_td(r['err_mo']) + bad_td(r['err_conn'])
        + td(r['avg_time']) + "</tr>"
        for r in rows
    ) or "<tr><td colspan='10'>нет данных</td></tr>"
    tot = ("<tr style='font-weight:bold;background:#eef'><td>ИТОГО</td>"
           + td(t.get('total', 0)) + td(t.get('success', 0))
           + td((str(t.get('pct_success')) + '%') if t.get('pct_success') is not None else '—')
           + td(t.get('err', 0))
           + td((str(t.get('pct_err')) + '%') if t.get('pct_err') is not None else '—')
           + td(t.get('err_mi', 0)) + td(t.get('err_mo', 0)) + td(t.get('err_conn', 0))
           + td(t.get('avg_time') if t.get('avg_time') is not None else '—') + "</tr>")
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:13px;color:#222">
<p>Сводный отчёт по <b>обработке лучевых исследований сервисом ИИ</b>{per}.</p>
<p>Всего исследований: <b>{t.get('total',0)}</b> · успешно обработано:
<b>{t.get('success',0)}</b> ({t.get('pct_success')}%) · с ошибкой: <b>{t.get('err',0)}</b>
({t.get('pct_err')}%) · среднее время обработки: <b>{t.get('avg_time')}</b> сек.</p>
<p style="color:#555">Ошибки по стороне: медизделие/ИИ-сервис (МИ) — <b>{t.get('err_mi',0)}</b> ·
медорганизация (МО) — <b>{t.get('err_mo',0)}</b> · соединение — <b>{t.get('err_conn',0)}</b>.
Успешность: <b style="color:#2563eb">синим</b> ≥ 90 %, <b style="color:#c0392b">красным</b> — ниже 70 %.</p>
<table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-size:12px">
<tr style="background:#1e3a5f;color:#fff"><th>Модальность</th><th>Всего</th><th>Успешно</th><th>%</th>
<th>Ошибки</th><th>%</th><th>МИ</th><th>МО</th><th>Соед.</th><th>Ср. время, с</th></tr>
{body}
{tot}
</table>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически системой мониторинга СЭМД.</p>
</body></html>"""


def _koiki_rows_html(wards, show_resp=False, cum_vyp=None):
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
        pv = w.get("vypoln")
        vcol = "#c0392b" if (pv is not None and pv < 100) else ("#2563eb" if pv is not None else "#222")
        # выполнение за весь обработанный период (все загруженные недели) — по каждому отделению
        cum_td = ""
        if cum_vyp is not None:
            cvv = cum_vyp.get(w["otdelenie"])
            ccol = "#c0392b" if (cvv is not None and cvv < 100) else ("#2563eb" if cvv is not None else "#222")
            cum_td = (f"<td style='text-align:right;color:{ccol}'>"
                      f"{(str(cvv)+'%') if cvv is not None else '—'}</td>")
        out.append(
            f"<tr><td>{w['otdelenie']}</td>{resp_td}"
            f"<td style='text-align:right'>{w['koek']}</td>"
            f"<td style='text-align:right'>{w['kd']}</td>"
            f"<td style='text-align:right;color:{color}'><b>{zt}</b></td>"
            f"<td style='text-align:right'>{w.get('postup',0)}</td>"
            f"<td style='text-align:right'>{w.get('plan') or '—'}</td>"
            f"<td style='text-align:right;color:{vcol}'>{(str(pv)+'%') if pv is not None else '—'}</td>"
            f"{cum_td}"
            f"<td style='text-align:right'>{w.get('vyp',0)}</td>"
            f"<td style='text-align:right'>{w.get('pered',0)}</td>"
            f"<td style='text-align:right'>{w.get('umer',0)}</td>"
            f"<td style='text-align:right'>{c(w.get('oborot'))}</td>"
            f"<td style='text-align:right'>{c(w.get('dlit'))}</td>"
            f"<td style='font-size:12px;color:#777'>{note}</td></tr>")
    span = 13 + (1 if show_resp else 0) + (1 if cum_vyp is not None else 0)
    return "".join(out) or f"<tr><td colspan='{span}'>нет данных</td></tr>"


_KOIKI_NOTE = ("Занятость = койко-дни ÷ (число коек × дни периода). "
               "<b style='color:#2563eb'>Синим</b> — занятость выше 100 % (план по койко-дням перевыполнен; "
               "для круглосуточных коек это также может означать, что коек в справочнике Промед меньше "
               "фактически развёрнутых — стоит проверить). "
               "<b style='color:#c0392b'>Красным</b> — ниже 80 % (недозагрузка коек). "
               "Оборот койки = выписано ÷ коек; средняя длительность = койко-дни ÷ выписано.")


def build_koiki_resp_html(resp, wards, days, rep_period="", custom="", cum_vyp=None):
    """Занятость коек по отделениям конкретного ответственного (заведующего)."""
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    who = f"Уважаемый(ая) {resp}!" if resp else "Уважаемый коллега!"
    vyp_hdr = ("<th>Вып. (период)</th><th>Вып. (весь)</th>"
               if cum_vyp is not None else "<th>Вып.</th>")
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<p>{who}</p>
<p>Показатели <b>занятости коек</b> по закреплённым за вами отделениям{per} (в периоде {days} дн.).</p>
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#1e3a5f;color:#fff"><th>Отделение</th><th>Коек</th><th>Койко-дни</th>
<th>Занятость</th><th>Поступ.</th><th>План</th>{vyp_hdr}<th>Выпис.</th><th>Перев.</th><th>Умер.</th><th>Оборот</th><th>Ср. длит.</th><th>Примечание</th></tr>
{_koiki_rows_html(wards, cum_vyp=cum_vyp)}
</table>
<p style="color:#555;font-size:12px">{_KOIKI_NOTE}</p>
{_custom_block(custom)}
<p style="color:#666;font-size:12px">Сформировано автоматически. Отдел информационных технологий.</p>
</body></html>"""


def build_koiki_overall_html(wards, totals, rep_period="", custom="", cum=None):
    """Общий сводный отчёт по занятости коек — ответственному за коечный фонд."""
    per = f" за период <b>{rep_period}</b>" if rep_period else ""
    t = totals
    m = t.get("mov", {})
    plan_line = ""
    if t.get("plan"):
        pv = t.get("plan_vypoln")
        pcol = "#2563eb" if (pv is not None and pv >= 100) else "#c0392b"
        plan_line = (f"<p>Выполнение плана госпитализаций — <b>за отчётный период</b>: план <b>{t['plan']}</b>, "
                     f"факт <b>{t.get('plan_fact', 0)}</b>, выполнение <b style='color:{pcol}'>{pv}%</b>.")
        if cum and cum.get("total_vypoln") is not None:
            cv = cum["total_vypoln"]; ccol = "#2563eb" if cv >= 100 else "#c0392b"
            plan_line += (f" <b>За весь обработанный период</b> ({cum.get('covered', 0)} дн.): "
                          f"план <b>{cum.get('tot_plan', 0)}</b>, факт <b>{cum.get('tot_fact', 0)}</b>, "
                          f"выполнение <b style='color:{ccol}'>{cv}%</b>.")
        plan_line += "</p>"
    # выполнение за весь обработанный период по каждому отделению (для колонки в таблице)
    cum_vyp = ({r["otdelenie"]: r.get("vypoln") for r in cum["rows"]}
               if cum and cum.get("rows") else None)
    vyp_hdr = ("<th>Вып. (период)</th><th>Вып. (весь)</th>"
               if cum_vyp is not None else "<th>Вып.</th>")
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
<p>Движение пациентов за период: поступило <b>{m.get('postup',0)}</b> · выписано <b>{m.get('vyp',0)}</b> ·
переведено в другие отделения <b>{m.get('pered',0)}</b> · умерло <b>{m.get('umer',0)}</b>.</p>
{plan_line}
<table border="1" cellspacing="0" cellpadding="5" style="border-collapse:collapse;font-size:13px">
<tr style="background:#1e3a5f;color:#fff"><th>Отделение</th><th>Ответственный</th><th>Коек</th><th>Койко-дни</th>
<th>Занятость</th><th>Поступ.</th><th>План</th>{vyp_hdr}<th>Выпис.</th><th>Перев.</th><th>Умер.</th><th>Оборот</th><th>Ср. длит.</th><th>Примечание</th></tr>
{_koiki_rows_html(wards, show_resp=True, cum_vyp=cum_vyp)}
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
