# -*- coding: utf-8 -*-
"""
СЭМД-уведомления — интерфейс для статистов: загрузка отчётов РЭМД,
наглядная картина (дашборд) и рассылка «долгов» врачам по почте.

Работает за Host Manager: слушает HOST/PORT из env, корень '/',
читает X-Remote-User / X-Remote-Name (с фиксом кодировки Latin-1 -> UTF-8).
"""
import os
import threading
import time
from flask import (Flask, request, render_template, redirect, url_for,
                   flash, send_from_directory)

import parser as report_parser
import storage
import mailer
import ipa
import appconfig

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "data", "uploads")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "semd-notify-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 МБ на файл

RTYPE_RU = {
    "vrachi": "По врачам (подписание)",
    "debts": "Неподписанные (долги)",
    "flk": "Ошибки ФЛК / регистрации",
    "mo": "Воронка по МО (с подписью МО)",
    "tvsp": "По ТВСП",
    "notrans": "Не переданы в РЭМД",
    "fap": "ФАП — работа в ЭМК",
    "vidy": "По видам документов",
    "docerr": "Ошибки по видам документов",
    "fedkpi": "Выполнение фед. показателей",
    "status": "Статусы документов",
    "state": "Состояние по ЭМД",
    "unknown": "Не распознан",
}
LOADABLE = ("vrachi", "debts", "flk", "mo", "tvsp", "notrans",
            "fap", "vidy", "docerr", "fedkpi", "status")

# Справочник поддерживаемых отчётов: точное наименование (как в ЕИСЗ ПК) и что даёт в системе.
REPORTS_INFO = [
    {"key": "vrachi",
     "title": "Отчёт по отправке документов в РЭМД в разрезе врачей",
     "gives": "Рейтинг врачей: сформировано / подписано / % подписания / зарегистрировано. "
              "Формирует список «Врачи / долги» и сводку по отделениям.",
     "section": "Дашборд · Врачи/долги · Отделения"},
    {"key": "debts",
     "title": "Список пациентов с неподписанными документами, подлежащими регистрации в РЭМД",
     "gives": "Конкретные неподписанные документы каждого врача (пациент, № случая, вид, дата). "
              "Наполняет содержимое писем-долгов.",
     "section": "Врачи/долги (рассылка)"},
    {"key": "flk",
     "title": "РЭМД. Детализация по ошибкам ФЛК",
     "gives": "Коды и описания ошибок регистрации по сотрудникам (OBJECT_NOT_FOUND и др.) — "
              "диагностика, почему документы не доходят до РЭМД.",
     "section": "Ошибки"},
    {"key": "mo",
     "title": "Отчёт по отправке документов в РЭМД в разрезе МО",
     "gives": "Полная воронка с шагом «подпись МО»: сформировано → подписано врачом → подписано МО → в РЭМД. "
              "Показывает, сколько документов застряло на подписи МО.",
     "section": "Дашборд (воронка)"},
    {"key": "tvsp",
     "title": "РЭМД. Статистика ЭМД в разрезе ТВСП",
     "gives": "Загрузка в РЭМД по подразделениям/ТВСП — видно точки, которые не передают "
              "(например, подразделения Еловского филиала на 0%).",
     "section": "Дашборд (ТВСП)"},
    {"key": "notrans",
     "title": "Отчёт по документам, не переданным в РЭМД",
     "gives": "Разбор «не в РЭМД»: не сформированы (клиническая сторона) vs сформированы, "
              "но не переданы (подпись МО / передача).",
     "section": "Дашборд (разбор причины)"},
    {"key": "vidy",
     "title": "РЭМД. Статистика отправки ЭМД в разрезе видов документов",
     "gives": "По каждому виду документа: зарегистрировано / отправлено / ошибки. "
              "Видно, какие виды документов проваливаются.",
     "section": "Дашборд (по видам)"},
    {"key": "docerr",
     "title": "РЭМД. Статистика по ошибкам документов",
     "gives": "Ошибки по видам документов и типам (не найдена запись справочника, валидация, должность). "
              "Дополняет ФЛК; идёт в отчёт ответственному.",
     "section": "Ошибки"},
    {"key": "fedkpi",
     "title": "РЭМД. Выполнение Фед. Показателей ЕЦКЗ по МО",
     "gives": "План/факт ТВСП и % достижения индивидуального плана — KPI, по которым судят МО.",
     "section": "Дашборд (показатели)"},
    {"key": "status",
     "title": "Статистика по статусам документов в РЭМД",
     "gives": "Распределение по статусам: зарегистрировано / отправлено / готов / ошибка.",
     "section": "Дашборд (статусы)"},
    {"key": "fap",
     "title": "Отчёт по работе в ЭМК по фельдшерам ФАП",
     "gives": "По фельдшерам ФАП: посещения, % заполнения ЭМК, рецепты, ЭЛН, подключение к интернету. "
              "Видно «молчащие» ФАПы и точки без интернета.",
     "section": "Страница «ФАП»"},
]


def current_user():
    """Читает заголовки Host Manager с корректной перекодировкой кириллицы."""
    login = request.headers.get("X-Remote-User", "")
    raw = request.headers.get("X-Remote-Name", "")
    try:
        name = raw.encode("latin-1").decode("utf-8") if raw else ""
    except (UnicodeError, AttributeError):
        name = raw
    return {"login": login or "—", "name": name}


@app.context_processor
def inject():
    return {"user": current_user(), "rtype_ru": RTYPE_RU,
            "smtp_ok": mailer.configured(), "smtp_dry": mailer.is_dryrun(),
            "ipa_ok": ipa.available(), "periods": storage.periods_info()}


@app.route("/")
def index():
    storage.init()
    return render_template("dashboard.html",
                           funnel=storage.funnel(),
                           full=storage.full_funnel(),
                           mo=storage.mo_funnel(),
                           notrans=storage.notrans_get(),
                           tvsp=storage.tvsp_list(),
                           vidy=storage.vidy_list(),
                           status=storage.status_list(),
                           fedkpi=storage.fedkpi_get(),
                           meta=storage.meta_all(),
                           top=storage.doctors("nepodp")[:15],
                           errors=storage.errors_summary())


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        files = request.files.getlist("files")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ok, skipped = [], []
        for f in files:
            if not f or not f.filename:
                continue
            path = os.path.join(UPLOAD_DIR, os.path.basename(f.filename))
            f.save(path)
            try:
                res = report_parser.parse(path)
            except Exception as e:
                skipped.append(f"{f.filename}: ошибка разбора ({e})")
                continue
            if res["type"] in LOADABLE:
                storage.replace_report(res["type"], os.path.basename(f.filename),
                                       res["period"], res["rows"], res["records"])
                ok.append(f"{f.filename} → {RTYPE_RU[res['type']]} ({len(res['records'])} записей)")
            else:
                skipped.append(f"{f.filename}: тип «{RTYPE_RU.get(res['type'], res['type'])}» пока не загружается")
        if ok:
            flash("Загружено: " + "; ".join(ok), "ok")
        if skipped:
            flash("Пропущено: " + "; ".join(skipped), "warn")
        pi = storage.periods_info()
        if not pi["consistent"]:
            parts = "; ".join(f"{p} ({', '.join(RTYPE_RU.get(t, t) for t in ts)})"
                              for p, ts in pi["by_period"].items())
            flash("⚠️ Периоды отчётов НЕ совпадают: " + parts +
                  ". Дашборд сравнивает отчёты между собой — загрузите отчёты за один период "
                  "или нажмите «Сбросить отчёты».", "warn")
        return redirect(url_for("upload"))
    meta = storage.meta_all()
    loaded = {m["rtype"] for m in meta}
    return render_template("upload.html", meta=meta, reports=REPORTS_INFO, loaded=loaded)


@app.route("/reset", methods=["POST"])
def reset():
    storage.reset_reports()
    flash("Загруженные отчёты сброшены. Почты врачей/зав. отделениями и настройки "
          "сохранены — можно загрузить новый период.", "ok")
    return redirect(url_for("upload"))


@app.route("/doctors")
def doctors():
    order = request.args.get("order", "nepodp")
    docs = storage.doctors(order)
    return render_template("doctors.html", docs=docs, order=order,
                           log=storage.send_log(30))


def _dispatch_batch(items, label):
    """Фоновая пакетная рассылка с троттлингом (чтобы не висел запрос и не блокировали за спам)."""
    def run():
        mailer.send_batch(items, on_result=lambda it, ok, msg:
                          storage.log_send(it["log_vrach"], it.get("to", ""), it["cnt"], msg))
    threading.Thread(target=run, name=f"mail-batch-{label}", daemon=True).start()


@app.route("/doctors/send", methods=["POST"])
def doctors_send():
    selected = request.form.getlist("vrach")
    if not selected:
        flash("Не выбрано ни одного врача.", "warn")
        return redirect(url_for("doctors"))
    items, noaddr = [], 0
    for vrach in selected:
        debts = storage.doctor_debts(vrach)
        if not debts:
            continue
        email = (request.form.get(f"email__{vrach}") or "").strip()
        if not email:
            noaddr += 1
            storage.log_send(vrach, "", len(debts), "нет адреса")
            continue
        items.append({"to": email, "log_vrach": vrach, "cnt": len(debts),
                      "subject": f"Неподписанные документы РЭМД: {len(debts)} шт.",
                      "html": mailer.build_debt_html(vrach, debts)})
    if items:
        _dispatch_batch(items, "doctors")
    dry = " (режим DRYRUN — реально не слалось)" if mailer.is_dryrun() else ""
    flash(f"Запущена пакетная рассылка: {len(items)} писем в фоне (с задержкой против спама)."
          + (f" Без адреса: {noaddr}." if noaddr else "") + " Результат — в журнале ниже." + dry,
          "ok" if items else "warn")
    return redirect(url_for("doctors"))


@app.route("/doctor/<path:vrach>")
def doctor_detail(vrach):
    return render_template("doctor_detail.html", vrach=vrach,
                           debts=storage.doctor_debts(vrach),
                           bd=storage.doctor_breakdown(vrach))


@app.route("/departments")
def departments():
    return render_template("departments.html",
                           depts=storage.dept_summary(), log=storage.send_log(30))


@app.route("/departments/send", methods=["POST"])
def departments_send():
    selected = request.form.getlist("podr")
    if not selected:
        flash("Не выбрано ни одного подразделения.", "warn")
        return redirect(url_for("departments"))
    items, noaddr = [], 0
    for podr in selected:
        d = storage.dept_vrachi(podr)
        if not d or not d["vrachi"]:
            continue
        cnt = d["nepodp"] or d["debts"]
        email = (request.form.get(f"email__{podr}") or "").strip()
        if not email:
            noaddr += 1
            storage.log_send(f"[отд.] {podr}", "", cnt, "нет адреса")
            continue
        items.append({"to": email, "log_vrach": f"[отд.] {podr}", "cnt": cnt,
                      "subject": f"Неподписанные документы по подразделению: {cnt} шт.",
                      "html": mailer.build_dept_html(podr, d["vrachi"], d["nepodp"], d["debts"], d.get("from_debts"))})
    if items:
        _dispatch_batch(items, "depts")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Запущена пакетная рассылка: {len(items)} писем в фоне."
          + (f" Без адреса: {noaddr}." if noaddr else "") + " Результат — в журнале." + dry,
          "ok" if items else "warn")
    return redirect(url_for("departments"))


@app.route("/report/send", methods=["POST"])
def report_send():
    name, email = mailer.report_recipient()
    if not email:
        flash("Не задан e-mail ответственного за исправление — укажите в Настройках.", "warn")
        return redirect(request.referrer or url_for("errors"))
    data = {"funnel": storage.funnel(),
            "errors": storage.errors_summary()["by_code"],
            "unassigned": storage.unassigned_summary(),
            "docerr": storage.docerr_list(),
            "mo_gap": (storage.mo_funnel() or {}).get("gap_vrach_mo")}
    html = mailer.build_report_html(data)
    ok, msg = mailer.send(email, "Отчёт по проблемам РЭМД (ответственному за исправление)", html)
    storage.log_send(f"[отчёт] {name or email}", email, len(data["unassigned"]), msg)
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Отчёт ответственному ({email}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(request.referrer or url_for("errors"))


@app.route("/errors")
def errors():
    resp_name, resp_email = mailer.report_recipient()
    return render_template("errors.html", e=storage.errors_summary(),
                           unassigned=storage.unassigned_summary(),
                           docerr=storage.docerr_list(),
                           resp_name=resp_name, resp_email=resp_email)


@app.route("/fap")
def fap():
    return render_template("fap.html", rows=storage.fap_list(), s=storage.fap_summary())


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "ipa_sync":
            try:
                loaded, matched = ipa.run_sync_and_record()
                flash(f"FreeIPA: загружено {loaded} учёток, сопоставлено врачам {matched}.", "ok")
            except Exception as e:
                flash(f"FreeIPA ошибка: {e}", "warn")
        elif action == "set_email":
            key = (request.form.get("key") or "").strip()
            email = (request.form.get("email") or "").strip()
            if key and email:
                storage.set_email(key, email)
                flash("Почта сохранена.", "ok")
        elif action == "set_dept":
            podr = (request.form.get("podr") or "").strip()
            email = (request.form.get("email") or "").strip()
            if podr and email:
                storage.set_dept_email(podr, email)
                flash("Почта подразделения сохранена.", "ok")
        elif action == "set_dept_bulk":
            saved = 0
            for key, val in request.form.items():
                if key.startswith("dept_email__"):
                    storage.set_dept_email(key[len("dept_email__"):], (val or "").strip())
                    saved += 1
            flash(f"Сохранены почты отделений ({saved}).", "ok")
        elif action == "save_smtp":
            for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_FROM", "SMTP_FROM_NAME",
                      "SMTP_BATCH_DELAY", "SMTP_BATCH_SIZE", "SMTP_BATCH_PAUSE"):
                appconfig.set(k, (request.form.get(k) or "").strip())
            appconfig.set("SMTP_TLS", "1" if request.form.get("SMTP_TLS") else "0")
            appconfig.set("SMTP_DRYRUN", "1" if request.form.get("SMTP_DRYRUN") else "0")
            pw = request.form.get("SMTP_PASS") or ""
            if pw:  # пустое поле — не перезаписываем сохранённый пароль
                appconfig.set("SMTP_PASS", pw)
            flash("Настройки почты сохранены.", "ok")
        elif action == "save_resp":
            appconfig.set("RESP_NAME", (request.form.get("RESP_NAME") or "").strip())
            appconfig.set("RESP_EMAIL", (request.form.get("RESP_EMAIL") or "").strip())
            flash("Ответственный за исправление сохранён.", "ok")
        elif action == "save_ipa":
            for k in ("IPA_LDAP_URI", "IPA_BASE_DN", "IPA_BIND_DN"):
                appconfig.set(k, (request.form.get(k) or "").strip())
            appconfig.set("IPA_AUTOSYNC", "1" if request.form.get("IPA_AUTOSYNC") else "0")
            appconfig.set("IPA_SYNC_HOURS", (request.form.get("IPA_SYNC_HOURS") or "24").strip())
            pw = request.form.get("IPA_BIND_PW") or ""
            if pw:
                appconfig.set("IPA_BIND_PW", pw)
            flash("Настройки FreeIPA сохранены.", "ok")
        return redirect(url_for("settings"))

    smtp = {k: appconfig.get(k, "") for k in
            ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_FROM", "SMTP_FROM_NAME")}
    smtp["SMTP_TLS"] = appconfig.get_bool("SMTP_TLS", True)
    smtp["SMTP_DRYRUN"] = appconfig.get_bool("SMTP_DRYRUN", False)
    smtp["pass_set"] = appconfig.is_set("SMTP_PASS")
    smtp["SMTP_BATCH_DELAY"] = appconfig.get("SMTP_BATCH_DELAY", "2")
    smtp["SMTP_BATCH_SIZE"] = appconfig.get("SMTP_BATCH_SIZE", "25")
    smtp["SMTP_BATCH_PAUSE"] = appconfig.get("SMTP_BATCH_PAUSE", "30")
    resp = {"RESP_NAME": appconfig.get("RESP_NAME", ""), "RESP_EMAIL": appconfig.get("RESP_EMAIL", "")}
    ipacfg = {k: appconfig.get(k, "") for k in ("IPA_LDAP_URI", "IPA_BASE_DN", "IPA_BIND_DN")}
    ipacfg["IPA_AUTOSYNC"] = appconfig.get_bool("IPA_AUTOSYNC", False)
    ipacfg["IPA_SYNC_HOURS"] = appconfig.get("IPA_SYNC_HOURS", "24")
    ipacfg["pass_set"] = appconfig.is_set("IPA_BIND_PW")
    last_ts, last_res = ipa.last_sync_info()
    return render_template("settings.html", smtp=smtp, ipacfg=ipacfg, resp=resp,
                           ipa_last=(last_ts, last_res),
                           docs=storage.doctors(), depts=storage.dept_summary())


@app.route("/healthz")
def healthz():
    return {"ok": True}


_scheduler_started = False


def _scheduler_loop():
    """Фоновая автосинхронизация FreeIPA: проверка раз в час, запуск раз в сутки (настраивается)."""
    while True:
        try:
            if ipa.due():
                ipa.run_sync_and_record()
        except Exception as e:
            try:
                storage.cfg_set("ipa_last_result", f"ошибка автосинхронизации: {e}")
            except Exception:
                pass
        time.sleep(3600)  # проверять раз в час


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, name="ipa-autosync", daemon=True)
    t.start()


if __name__ == "__main__":
    storage.init()
    start_scheduler()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        use_reloader=False,
    )
