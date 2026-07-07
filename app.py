# -*- coding: utf-8 -*-
"""
СЭМД-уведомления — интерфейс для статистов: загрузка отчётов РЭМД,
наглядная картина (дашборд) и рассылка «долгов» врачам по почте.

Работает за Host Manager: слушает HOST/PORT из env, корень '/',
читает X-Remote-User / X-Remote-Name (с фиксом кодировки Latin-1 -> UTF-8).
"""
import os
import re
import threading
import time
from flask import (Flask, request, render_template, redirect, url_for,
                   flash, send_from_directory, send_file)


def _split_emails(raw):
    """Разбирает поле с несколькими адресами (запятая/точка с запятой/пробел)."""
    return [e for e in (x.strip() for x in re.split(r"[,;\s]+", raw or "")) if e]

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
    "koiki": "Койки — занятость (стационар)",
    "state": "Состояние по ЭМД",
    "unknown": "Не распознан",
}
LOADABLE = ("vrachi", "debts", "flk", "mo", "tvsp", "notrans",
            "fap", "vidy", "docerr", "fedkpi", "status", "koiki")

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
    {"key": "koiki",
     "title": "Сводная ведомость движения пациентов и коечного фонда (стационар/дневной)",
     "gives": "Занятость коек по отделениям: койко-дни, занятость %, оборот, средняя длительность. "
              "Рассылка ответственным за отделения и сводный отчёт ответственному за коечный фонд.",
     "section": "Страница «Койки»",
     "note": "В ЕИСЗ ПК (Промед) отчёт называется «Форма № 016/у Изменённая»."},
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
            "ipa_ok": ipa.available(), "periods": storage.periods_info(),
            "period_history": storage.periods_history(),
            "active_period": appconfig.get("active_period", "")}


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
        parsed = []   # (res, filename, raw_bytes)
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
                with open(path, "rb") as fh:
                    parsed.append((res, os.path.basename(f.filename), fh.read()))
            else:
                skipped.append(f"{f.filename}: тип «{RTYPE_RU.get(res['type'], res['type'])}» пока не загружается")
        if parsed:
            # идентичность периода — НЕДЕЛЯ начала (конец у ФЛК/статусов/ФАП может «дребезжать»)
            # период выгрузки = максимальный охват загруженных отчётов
            batch_period = report_parser.max_period([res["period"] for res, _, _ in parsed]) or "(без периода)"
            # Файлы просто грузятся в текущую выгрузку (тот же тип — замещается).
            # Период с ранее загруженными НЕ сравниваем: для нового периода жмите «Новая выгрузка».
            for res, fn, raw in parsed:
                storage.replace_report(res["type"], fn, res["period"], res["rows"], res["records"])
                storage.save_period_file(batch_period, res["type"], fn, raw)
                ok.append(f"{fn} → {RTYPE_RU[res['type']]} ({len(res['records'])} записей)")
            appconfig.set("active_period", batch_period)
        if ok:
            flash("Загружено: " + "; ".join(ok), "ok")
        if skipped:
            flash("Пропущено: " + "; ".join(skipped), "warn")
        # Предупреждение — только если в ОДНОЙ выгрузке отчёты за разные периоды
        pi = storage.periods_info()
        if not pi["consistent"]:
            parts = "; ".join(f"{p} ({', '.join(RTYPE_RU.get(t, t) for t in ts)})"
                              for p, ts in pi["by_period"].items())
            flash("⚠️ В выгрузке отчёты за РАЗНЫЕ периоды: " + parts +
                  ". Оставьте один период (удалите лишний отчёт) либо начните «Новую выгрузку» "
                  "и загрузите один период.", "warn")
        return redirect(url_for("upload"))
    active = appconfig.get("active_period", "")
    # статусы и «что загружено» — строго по набору отчётов активной выгрузки
    exports = {x["rtype"]: x["filename"] for x in storage.period_rtypes(active)} if active else {}
    meta = [m for m in storage.meta_all() if m["rtype"] in exports]
    loaded = set(exports)
    meta_by_rtype = {m["rtype"]: m for m in meta}
    return render_template("upload.html", meta=meta, reports=REPORTS_INFO, loaded=loaded,
                           meta_by_rtype=meta_by_rtype, exports=exports)


@app.route("/reset", methods=["POST"])
def reset():
    active = appconfig.get("active_period", "")
    if active:
        storage.delete_period(active)
        flash(f"Отчёты периода «{active}» сброшены (удалены, в т.ч. из истории). "
              "Почты врачей/зав. отделениями и настройки сохранены.", "ok")
    else:
        storage.reset_reports()
        flash("Рабочие данные очищены. Почты и настройки сохранены.", "ok")
    return redirect(url_for("upload"))


@app.route("/period/new", methods=["POST"])
def period_new():
    storage.new_period()
    flash("Начата новая выгрузка — загрузите отчёты нового периода. "
          "Прежняя выгрузка остаётся в истории (можно вернуться).", "ok")
    return redirect(url_for("upload"))


@app.route("/period/delete_report", methods=["POST"])
def period_delete_report():
    rtype = (request.form.get("rtype") or "").strip()
    active = appconfig.get("active_period", "")
    storage.delete_report(active, rtype)
    flash(f"Отчёт «{RTYPE_RU.get(rtype, rtype)}» удалён из периода.", "ok")
    return redirect(url_for("upload"))


@app.route("/period/switch", methods=["POST"])
def period_switch():
    period = (request.form.get("period") or "").strip()
    n = storage.switch_period(period)
    if n:
        flash(f"Переключено на период «{period}» (загружено отчётов: {n}).", "ok")
    else:
        flash(f"Для периода «{period}» нет сохранённых отчётов.", "warn")
    return redirect(request.referrer or url_for("upload"))


@app.route("/period/delete", methods=["POST"])
def period_delete():
    period = (request.form.get("period") or "").strip()
    if period:
        storage.delete_period(period)
        flash(f"Выгрузка «{period}» удалена из истории.", "ok")
    return redirect(request.referrer or url_for("upload"))


@app.route("/reprocess", methods=["POST"])
def reprocess():
    """Заново разбирает сохранённые файлы активного периода (после обновления парсера)."""
    period = appconfig.get("active_period", "")
    if not period:
        flash("Нет активного периода. Сначала загрузите отчёты.", "warn")
        return redirect(url_for("upload"))
    n = storage.switch_period(period)
    flash(f"Переобработано отчётов за «{period}»: {n} (сохранённые файлы разобраны заново).",
          "ok" if n else "warn")
    return redirect(url_for("upload"))


@app.route("/export/<rtype>")
def export(rtype):
    period = appconfig.get("active_period", "")
    fn, data = storage.period_file(period, rtype)
    if not data:
        flash("Файл не найден для текущего периода.", "warn")
        return redirect(url_for("upload"))
    from io import BytesIO
    return send_file(BytesIO(data), as_attachment=True, download_name=fn or f"{rtype}.xls",
                     mimetype="application/vnd.ms-excel")


@app.route("/doctors")
def doctors():
    order = request.args.get("order", "nepodp")
    docs = storage.doctors(order)
    return render_template("doctors.html", docs=docs, order=order,
                           log=storage.send_log(30))


# --- Единая очередь фоновой рассылки: задания идут по очереди (один воркер),
#     чтобы троттлинг соблюдался и рассылки не шли параллельно через один SMTP ---
import queue as _queue

_send_q = _queue.Queue()
_send_lock = threading.Lock()
_send_status = {"active": False, "label": "", "done": 0, "total": 0,
                "ok": 0, "failed": 0, "queued": 0, "ts": ""}
_send_worker_started = False
_send_cancel = threading.Event()


def _send_worker():
    while True:
        job = _send_q.get()
        items, label = job["items"], job["label"]
        with _send_lock:
            _send_status.update({"active": True, "label": label, "done": 0, "total": len(items),
                                 "ok": 0, "failed": 0, "queued": _send_q.qsize(),
                                 "ts": time.strftime("%H:%M:%S")})

        def on_result(it, ok, msg):
            storage.log_send(it["log_vrach"], it.get("to", ""), it["cnt"], msg)
            with _send_lock:
                _send_status["done"] += 1
                _send_status["ok" if ok else "failed"] += 1
        try:
            mailer.send_batch(items, on_result=on_result, cancel=lambda: _send_cancel.is_set())
        except Exception:
            pass
        if _send_cancel.is_set():
            _send_cancel.clear()
        with _send_lock:
            _send_status["active"] = _send_q.qsize() > 0
            _send_status["queued"] = _send_q.qsize()
        _send_q.task_done()


def _start_send_worker():
    global _send_worker_started
    if _send_worker_started:
        return
    _send_worker_started = True
    threading.Thread(target=_send_worker, name="send-worker", daemon=True).start()


def _dispatch_batch(items, label):
    """Ставит пачку в очередь фоновой рассылки (один воркер обрабатывает по очереди)."""
    _start_send_worker()
    _send_q.put({"items": items, "label": label})
    with _send_lock:
        _send_status["queued"] = _send_q.qsize() + (1 if _send_status["active"] else 0)


@app.route("/send/status")
def send_status():
    with _send_lock:
        st = dict(_send_status)
    st["log"] = storage.send_log(20)
    return st


@app.route("/send/cancel", methods=["POST"])
def send_cancel():
    with _send_lock:
        active = _send_status["active"]
    drained = 0
    try:
        while True:
            _send_q.get_nowait()
            _send_q.task_done()
            drained += 1
    except _queue.Empty:
        pass
    if active:
        _send_cancel.set()  # прервать текущую пачку (воркер снимет флаг после)
    if active or drained:
        flash(f"Рассылка отменяется. Снято из очереди: {drained}."
              + (" Текущая пачка остановится." if active else ""), "warn")
    else:
        flash("Активной рассылки нет.", "warn")
    return redirect(request.referrer or url_for("doctors"))


@app.route("/doctors/send", methods=["POST"])
def doctors_send():
    selected = request.form.getlist("vrach")
    if not selected:
        flash("Не выбрано ни одного врача.", "warn")
        return redirect(url_for("doctors"))
    rep = storage.report_period("debts")
    cust = appconfig.get("CUSTOM_DEBT", "")
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
        subj = f"Неподписанные документы РЭМД: {len(debts)} шт." + (f" (период {rep})" if rep else "")
        items.append({"to": email, "log_vrach": vrach, "cnt": len(debts), "subject": subj,
                      "html": mailer.build_debt_html(vrach, debts, rep, cust)})
    if items:
        _dispatch_batch(items, "doctors")
    dry = " (режим DRYRUN — реально не слалось)" if mailer.is_dryrun() else ""
    flash(f"Запущена пакетная рассылка: {len(items)} писем в фоне (с задержкой против спама)."
          + (f" Без адреса: {noaddr}." if noaddr else "") + " Результат — в журнале ниже." + dry,
          "ok" if items else "warn")
    return redirect(url_for("doctors"))


@app.route("/doctors/save_emails", methods=["POST"])
def doctors_save_emails():
    items = [(k[len("email__"):], v) for k, v in request.form.items() if k.startswith("email__")]
    n = storage.bulk_set_doctor_emails(items) if items else 0
    flash(f"Сохранены почты врачей ({n}).", "ok" if n else "warn")
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
    rep = storage.report_period("vrachi")
    cust = appconfig.get("CUSTOM_DEPT", "")
    items, noaddr = [], 0
    for podr in selected:
        d = storage.dept_vrachi(podr)
        if not d or not d["vrachi"]:
            continue
        cnt = d["nepodp"] or d["debts"]
        emails = _split_emails(request.form.get(f"email__{podr}"))
        if not emails:
            noaddr += 1
            storage.log_send(f"[отд.] {podr}", "", cnt, "нет адреса")
            continue
        subj = f"Неподписанные документы по подразделению: {cnt} шт." + (f" (период {rep})" if rep else "")
        items.append({"to": ", ".join(emails), "log_vrach": f"[отд.] {podr}", "cnt": cnt, "subject": subj,
                      "html": mailer.build_dept_html(podr, d["vrachi"], d["nepodp"], d["debts"],
                                                     d.get("from_debts"), rep_period=rep, custom=cust)})
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
    rep = storage.report_period("vrachi")
    data = {"funnel": storage.funnel(),
            "errors": storage.errors_summary()["by_code"],
            "unassigned": storage.unassigned_summary(),
            "docerr": storage.docerr_list(),
            "mo_gap": (storage.mo_funnel() or {}).get("gap_vrach_mo"),
            "period": rep}
    html = mailer.build_report_html(data, appconfig.get("CUSTOM_ERR", ""))
    subj = "Отчёт по проблемам РЭМД (ответственному за исправление)" + (f" — период {rep}" if rep else "")
    ok, msg = mailer.send(email, subj, html)
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
    resp_name, resp_email = mailer.fap_recipient()
    return render_template("fap.html", rows=storage.fap_list(), s=storage.fap_summary(),
                           resp_name=resp_name, resp_email=resp_email)


@app.route("/fap/report/send", methods=["POST"])
def fap_report_send():
    name, email = mailer.fap_recipient()
    if not email:
        flash("Не задан e-mail ответственного — укажите в Настройках.", "warn")
        return redirect(url_for("fap"))
    s = storage.fap_summary()
    if not s:
        flash("Отчёт по ФАП не загружен.", "warn")
        return redirect(url_for("fap"))
    rep = storage.report_period("fap")
    html = mailer.build_fap_report_html(s, storage.fap_list(), rep, appconfig.get("CUSTOM_FAP", ""))
    subj = "Отчёт по работе ФАП в ЭМК (ответственному)" + (f" — период {rep}" if rep else "")
    ok, msg = mailer.send(email, subj, html)
    storage.log_send(f"[ФАП-отчёт] {name or email}", email, s.get("n", 0), msg)
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Отчёт по ФАП ответственному ({email}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("fap"))


@app.route("/koiki")
def koiki():
    kname, kemail = mailer.koiki_recipient()
    return render_template("koiki.html",
                           wards=storage.koiki_list(),
                           totals=storage.koiki_totals(),
                           groups=storage.koiki_groups(),
                           resp_name=kname, resp_email=kemail,
                           period=storage.report_period("koiki"),
                           log=storage.send_log(30))


@app.route("/koiki/save_map", methods=["POST"])
def koiki_save_map():
    resp_items = {k[len("resp__"):]: v for k, v in request.form.items() if k.startswith("resp__")}
    email_items = {k[len("email__"):]: v for k, v in request.form.items() if k.startswith("email__")}
    ods = set(resp_items) | set(email_items)
    for od in ods:
        storage.set_koiki_resp(od, resp_items.get(od, ""), email_items.get(od, ""))
    flash(f"Сохранено сопоставление отделений ({len(ods)}).", "ok" if ods else "warn")
    return redirect(url_for("koiki"))


@app.route("/koiki/send", methods=["POST"])
def koiki_send():
    selected = {e.lower() for e in request.form.getlist("grp")}
    if not selected:
        flash("Не выбрано ни одного ответственного.", "warn")
        return redirect(url_for("koiki"))
    rep = storage.report_period("koiki")
    days = storage.koiki_totals()["days"]
    cust = appconfig.get("CUSTOM_KOIKI", "")
    items = []
    for g in storage.koiki_groups():
        if g["email"].lower() not in selected:
            continue
        subj = "Занятость коек по отделениям" + (f" (период {rep})" if rep else "")
        items.append({"to": g["email"], "log_vrach": f"[койки] {g['resp'] or g['email']}",
                      "cnt": len(g["wards"]), "subject": subj,
                      "html": mailer.build_koiki_resp_html(g["resp"], g["wards"], days, rep, cust)})
    if items:
        _dispatch_batch(items, "koiki")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Запущена рассылка ответственным за отделения: {len(items)} писем.{dry}",
          "ok" if items else "warn")
    return redirect(url_for("koiki"))


@app.route("/koiki/report/send", methods=["POST"])
def koiki_report_send():
    name, email = mailer.koiki_recipient()
    if not email:
        flash("Не задан e-mail ответственного за коечный фонд — укажите в Настройках.", "warn")
        return redirect(url_for("koiki"))
    wards = storage.koiki_list()
    if not wards:
        flash("Отчёт по койкам не загружен.", "warn")
        return redirect(url_for("koiki"))
    rep = storage.report_period("koiki")
    html = mailer.build_koiki_overall_html(wards, storage.koiki_totals(), rep,
                                           appconfig.get("CUSTOM_KOIKI", ""))
    subj = "Сводный отчёт: занятость коечного фонда" + (f" — период {rep}" if rep else "")
    ok, msg = mailer.send(email, subj, html)
    storage.log_send(f"[койки-свод] {name or email}", email, len(wards), msg)
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Сводный отчёт по койкам ({email}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("koiki"))


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
            for k in ("RESP_NAME", "RESP_EMAIL", "RESP_FAP_NAME", "RESP_FAP_EMAIL",
                      "RESP_KOIKI_NAME", "RESP_KOIKI_EMAIL"):
                appconfig.set(k, (request.form.get(k) or "").strip())
            flash("Ответственные сохранены.", "ok")
        elif action == "save_custom":
            for k in ("CUSTOM_DEBT", "CUSTOM_DEPT", "CUSTOM_ERR", "CUSTOM_FAP", "CUSTOM_KOIKI"):
                appconfig.set(k, (request.form.get(k) or "").strip())
            flash("Дополнительный текст писем сохранён.", "ok")
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
    resp = {k: appconfig.get(k, "") for k in
            ("RESP_NAME", "RESP_EMAIL", "RESP_FAP_NAME", "RESP_FAP_EMAIL",
             "RESP_KOIKI_NAME", "RESP_KOIKI_EMAIL")}
    custom = {k: appconfig.get(k, "") for k in
              ("CUSTOM_DEBT", "CUSTOM_DEPT", "CUSTOM_ERR", "CUSTOM_FAP", "CUSTOM_KOIKI")}
    ipacfg = {k: appconfig.get(k, "") for k in ("IPA_LDAP_URI", "IPA_BASE_DN", "IPA_BIND_DN")}
    ipacfg["IPA_AUTOSYNC"] = appconfig.get_bool("IPA_AUTOSYNC", False)
    ipacfg["IPA_SYNC_HOURS"] = appconfig.get("IPA_SYNC_HOURS", "24")
    ipacfg["pass_set"] = appconfig.is_set("IPA_BIND_PW")
    last_ts, last_res = ipa.last_sync_info()
    return render_template("settings.html", smtp=smtp, ipacfg=ipacfg, resp=resp, custom=custom,
                           ipa_last=(last_ts, last_res),
                           docs=storage.doctors(), depts=storage.dept_summary())


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/favicon.ico")
def favicon():
    # Браузеры неявно запрашивают /favicon.ico — отдаём наш SVG-значок.
    return redirect(url_for("static", filename="favicon.svg"))


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
