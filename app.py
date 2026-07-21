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
import zlib
from collections import Counter
from flask import (Flask, request, render_template, redirect, url_for,
                   flash, send_from_directory, send_file, has_request_context)


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
    "notrans": "Не переданы в РЭМД",
    "fap": "ФАП — работа в ЭМК",
    "vidy": "По видам документов",
    "docerr": "Ошибки по видам документов",
    "status": "Статусы документов",
    "koiki": "Стационары (занятость коек)",
    "max": "MAX — записи и ТМК через чат-бот",
    "xray": "Рентген — обработка исследований ИИ",
    "state": "Состояние по ЭМД (первичка)",
    "detail": "Детализация отправки ЭМД (первичка)",
    "vid_worker": "Количество ЭМД вид×работник (считается из витрины)",
    "unknown": "Не распознан",
}
LOADABLE = ("vrachi", "debts", "flk", "notrans",
            "fap", "vidy", "docerr", "status", "koiki", "max", "xray",
            "state", "detail")

# Справочник поддерживаемых отчётов: точное наименование (как в ЕИСЗ ПК) и что даёт в системе.
REPORTS_INFO = [
    {"key": "vrachi",
     "title": "Отчёт по отправке документов в РЭМД в разрезе врачей",
     "gives": "Рейтинг врачей: сформировано / подписано / % подписания / зарегистрировано. "
              "Формирует список «Врачи / долги» и сводку по отделениям.",
     "section": "Дашборд · Врачи/долги · Отделения",
     "note": "ОСТАЁТСЯ при переходе на первичку: единственный источник «подписано врачом» — "
             "без него не видна прослойка «подписан врачом, ждёт подписи МО» и рейтинг подписания."},
    {"key": "debts",
     "title": "Список пациентов с неподписанными документами, подлежащими регистрации в РЭМД",
     "gives": "Конкретные неподписанные документы каждого врача (пациент, № случая, вид, дата). "
              "Наполняет содержимое писем-долгов.",
     "section": "Врачи/долги (рассылка)"},
    {"key": "state",
     "title": "РЭМД. Состояние по ЭМД",
     "gives": "ПЕРВИЧКА (еженед.): все подписанные МО документы, созданные за период, — вид, врач, "
              "подразделение, статус, даты, дни до подписи/регистрации, попытки. Скелет витрины ЭМД: "
              "из неё считаются статусы, виды, SLA за любой период. ПДн пациентов не сохраняются.",
     "section": "ЭМД → Аналитика",
     "note": "Выгружать еженедельно (лимит выгрузки 50 000 строк). Фильтр — по дате документа."},
    {"key": "detail",
     "title": "Детализация статистики отправки ЭМД",
     "gives": "ПЕРВИЧКА (еженед.): события отправки/регистрации за период, включая документы прошлых "
              "недель (сама актуализирует витрину задним числом) + коды и полные тексты ошибок. "
              "Заменяет отчёты об ошибках ФЛК/по документам.",
     "section": "ЭМД → Аналитика",
     "note": "Выгружать еженедельно (лимит 50 000 строк). Фильтр — по дате события отправки."},
    {"key": "flk",
     "title": "РЭМД. Детализация по ошибкам ФЛК",
     "gives": "Коды и описания ошибок регистрации по сотрудникам (OBJECT_NOT_FOUND и др.) — "
              "диагностика, почему документы не доходят до РЭМД.",
     "section": "Ошибки",
     "note": "Заменяется «Детализацией отправки» (там ошибки с конкретными документами). "
             "После сверки переходного периода можно не выгружать."},
    {"key": "notrans",
     "title": "Отчёт по документам, не переданным в РЭМД",
     "gives": "Разбор «не в РЭМД»: не сформированы (клиническая сторона) vs сформированы, "
              "но не переданы (подпись МО / передача).",
     "section": "Дашборд (разбор причины)",
     "note": "ОСТАЁТСЯ при переходе на первичку: единственный источник «не сформированы» "
             "(клиническая сторона) — из витрины не выводится."},
    {"key": "vidy",
     "title": "РЭМД. Статистика отправки ЭМД в разрезе видов документов",
     "gives": "По каждому виду документа: зарегистрировано / отправлено / ошибки. "
              "Видно, какие виды документов проваливаются.",
     "section": "Дашборд (по видам)",
     "note": "Выводится из витрины (ЭМД → Аналитика). После сверки переходного периода "
             "можно не выгружать."},
    {"key": "docerr",
     "title": "РЭМД. Статистика по ошибкам документов",
     "gives": "Ошибки по видам документов и типам (не найдена запись справочника, валидация, должность). "
              "Дополняет ФЛК; идёт в отчёт ответственному.",
     "section": "Ошибки",
     "note": "Заменяется «Детализацией отправки». После сверки переходного периода "
             "можно не выгружать."},
    {"key": "status",
     "title": "Статистика по статусам документов в РЭМД",
     "gives": "Распределение по статусам: зарегистрировано / отправлено / готов / ошибка.",
     "section": "Дашборд (статусы)",
     "note": "Выводится из витрины (ЭМД → Аналитика). После сверки переходного периода "
             "можно не выгружать."},
    {"key": "fap",
     "title": "Отчёт по работе в ЭМК по фельдшерам ФАП",
     "gives": "По фельдшерам ФАП: посещения, % заполнения ЭМК, рецепты, ЭЛН, подключение к интернету. "
              "Видно «молчащие» ФАПы и точки без интернета.",
     "section": "Страница «ФАП»"},
    {"key": "koiki",
     "title": "Сводная ведомость движения пациентов и коечного фонда (стационар/дневной)",
     "gives": "Занятость коек по отделениям: койко-дни, занятость %, оборот, средняя длительность. "
              "Рассылка ответственным за отделения и сводный отчёт ответственному за коечный фонд.",
     "section": "Страница «Стационары»",
     "note": "В ЕИСЗ ПК (Промед) отчёт называется «Форма № 016/у Изменённая»."},
    {"key": "max",
     "title": "Отчёт о количестве записей и оказания услуг ТМК через чат-бот MAX",
     "gives": "Телемедицина через чат-бот MAX: записи на ТМК, проведённые консультации, отменённые "
              "и больничные — всего и через MAX, с долей MAX по врачам, должностям и целям. "
              "Показатель цифровизации (проникновение чат-бота MAX).",
     "section": "Страница «MAX»"},
    {"key": "xray",
     "title": "Отчёт по обработке лучевых исследований сервисом ИИ",
     "gives": "Обработка исследований (ФЛГ, ММГ, РГ, КТ, …) сервисом ИИ: всего / успешно обработано / "
              "с ошибкой по модальностям, разбивка ошибок (сторона МИ / МО / соединение) и среднее "
              "время обработки. Показатель цифровизации (внедрение ИИ в лучевую диагностику).",
     "section": "Страница «Рентген»",
     "note": "Файл .xlsx. В отчёте нет периода — берётся период текущей выгрузки."},
]

# Направление отчёта. В UI отдельной колонки больше нет: направления разово
# мигрируют в теги (см. _seed_report_tags), здесь остаются для порядка сортировки.
REPORT_GROUP = {
    "vrachi": "ЭМД", "debts": "ЭМД", "notrans": "ЭМД", "vidy": "ЭМД", "status": "ЭМД",
    "flk": "Ошибки", "docerr": "Ошибки",
    "fap": "ФАП", "koiki": "Стационары", "max": "Телемед", "xray": "Рентген",
    "state": "ЭМД", "detail": "ЭМД",
}
# Прежняя жёсткая классификация «необходимые/дополнительные». Оставлена только как
# источник разовой миграции в стартовые теги (см. _seed_report_tags) — дальше
# классификацию ведёт пользователь своими тегами на странице «Загрузка».
REPORT_REQUIRED = {"vrachi", "debts", "flk", "fap", "koiki"}
REPORT_GROUP_ORDER = {"ЭМД": 0, "Ошибки": 1, "Стационары": 2, "ФАП": 3, "Телемед": 4, "Рентген": 5}
# Кому уходит рассылка по отчёту (если уходит). Отчётов здесь нет → рассылки нет, только визуализация.
REPORT_MAILING = {
    "debts": "Врачам — их неподписанные документы",
    "vrachi": "Зав. отделениями (сводки) + ответственному (сводный по подразделениям)",
    "flk": "Ответственному за ошибки РЭМД",
    "docerr": "Ответственному за ошибки РЭМД",
    "fap": "Ответственному за ФАП",
    "koiki": "Ответственным за отделения + за коечный фонд",
    "max": "Ответственному за цифровизацию / ТМК",
    "xray": "Ответственному за цифровизацию / лучевую диагностику",
}

# Ключи произвольного текста писем (редактируются на страницах соответствующих отчётов)
CUSTOM_KEYS = ("CUSTOM_DEBT", "CUSTOM_DEPT", "CUSTOM_ERR", "CUSTOM_FAP",
               "CUSTOM_KOIKI", "CUSTOM_MAX", "CUSTOM_XRAY")


def current_user():
    """Читает заголовки Host Manager с корректной перекодировкой кириллицы."""
    login = request.headers.get("X-Remote-User", "")
    raw = request.headers.get("X-Remote-Name", "")
    try:
        name = raw.encode("latin-1").decode("utf-8") if raw else ""
    except (UnicodeError, AttributeError):
        name = raw
    words = [w for w in (name or login).replace("_", " ").replace(".", " ").split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "•"
    return {"login": login or "—", "name": name, "initials": initials}


def _acting_user():
    """Кто выполняет действие (для журналов). Вне запроса (фоновый воркер) — пусто."""
    if has_request_context():
        u = current_user()
        return u["name"] or (u["login"] if u["login"] != "—" else "")
    return ""


def audit(action, details=""):
    """Журнал операций менеджера: кто и что сделал. Никогда не роняет обработчик."""
    try:
        storage.log_op(_acting_user(), action, details)
    except Exception:
        pass


@app.context_processor
def inject():
    return {"user": current_user(), "rtype_ru": RTYPE_RU,
            "smtp_ok": mailer.configured(), "smtp_dry": mailer.is_dryrun(),
            "ipa_ok": ipa.available(), "periods": storage.periods_info(),
            "period_history": storage.periods_history(),
            "active_period": appconfig.get("active_period", ""),
            "custom_texts": {k: appconfig.get(k, "") for k in CUSTOM_KEYS}}


@app.route("/")
def index():
    storage.init()
    return render_template("dashboard.html",
                           funnel=storage.funnel(),
                           status=storage.status_list(),
                           notrans=storage.notrans_get(),
                           vidy=storage.vidy_list(),
                           top=storage.doctors("nepodp")[:15],
                           errors=storage.errors_summary(),
                           has_data=bool(storage.meta_all()))


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
            batch_period = report_parser.max_period([res["period"] for res, _, _ in parsed])
            if not batch_period:
                # Отчёты без собственного периода (напр. Рентген) привязываем к УЖЕ
                # открытой выгрузке, а не создаём отдельную «(без периода)». Отдельный
                # бакет — только если активной выгрузки нет вовсе.
                batch_period = appconfig.get("active_period", "") or "(без периода)"
            # Файлы просто грузятся в текущую выгрузку (тот же тип — замещается).
            # Период с ранее загруженными НЕ сравниваем: для нового периода жмите «Новая выгрузка».
            for res, fn, raw in parsed:
                info = storage.replace_report(res["type"], fn, res["period"], res["rows"], res["records"])
                storage.save_period_file(batch_period, res["type"], fn, raw)
                if info:
                    ok.append(f"{fn} → {RTYPE_RU[res['type']]}: в витрину +{info['ins']} новых, "
                              f"{info['upd']} обновлено")
                else:
                    ok.append(f"{fn} → {RTYPE_RU[res['type']]} ({len(res['records'])} записей)")
            appconfig.set("active_period", batch_period)
            audit("Загрузка отчётов", f"период «{batch_period}»; файлов: {len(parsed)} — "
                  + ", ".join(fn for _, fn, _ in parsed))
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
    meta_by_rtype = {m["rtype"]: m for m in meta}
    rcfg = storage.report_cfg_all()  # пользовательские комментарии к отчётам
    _seed_report_tags()
    tags_by = storage.report_tags_all()
    allr = []
    for r in REPORTS_INFO:
        rc = rcfg.get(r["key"], {})
        allr.append(dict(r, mailing=REPORT_MAILING.get(r["key"], ""),
                         comment=rc.get("comment", ""),
                         tags=tags_by.get(r["key"], [])))
    allr.sort(key=lambda r: (REPORT_GROUP_ORDER.get(REPORT_GROUP.get(r["key"], ""), 9), r["title"]))
    cnt = Counter(t for ts in tags_by.values() for t in ts)
    tag_counts = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    tag_hues = {t: zlib.crc32(t.encode("utf-8")) % 360 for t in cnt}
    untagged = sum(1 for r in allr if not r["tags"])
    return render_template("upload.html", meta=meta, meta_by_rtype=meta_by_rtype, exports=exports,
                           reports=allr, tag_counts=tag_counts, tag_hues=tag_hues, untagged=untagged)


def _seed_report_tags():
    """Разовые миграции прежней классификации в теги. Дальше пользователь ведёт теги сам.
    v1: «основные/дополнительные» -> теги. v2: направления (ЭМД/Ошибки/…) -> теги."""
    if not appconfig.get("REPORT_TAGS_SEEDED"):
        rcfg = storage.report_cfg_all()
        for r in REPORTS_INFO:
            k = r["key"]
            rc = rcfg.get(k, {})
            req = bool(rc["required"]) if rc.get("required") is not None else (k in REPORT_REQUIRED)
            storage.report_tag_add(k, "Основные" if req else "Дополнительные")
        appconfig.set("REPORT_TAGS_SEEDED", "1")
    if not appconfig.get("REPORT_TAGS_SEEDED_GROUPS"):
        for r in REPORTS_INFO:
            g = REPORT_GROUP.get(r["key"])
            if g:
                storage.report_tag_add(r["key"], g)
        appconfig.set("REPORT_TAGS_SEEDED_GROUPS", "1")


@app.route("/reports/config", methods=["POST"])
def reports_config():
    """Комментарии к отчётам (классификация — тегами, см. reports_tag_*)."""
    saved = 0
    for r in REPORTS_INFO:
        comment = request.form.get(f"comment__{r['key']}")
        if comment is None:
            continue
        storage.set_report_comment(r["key"], comment)
        saved += 1
    flash(f"Комментарии отчётов сохранены ({saved}).", "ok")
    audit("Комментарии отчётов", f"обновлено: {saved}")
    return redirect(url_for("upload"))


@app.route("/reports/tag/add", methods=["POST"])
def reports_tag_add():
    rtype = (request.form.get("rtype") or "").strip()
    tag = (request.form.get("tag") or "").strip()
    storage.report_tag_add(rtype, tag)
    if rtype and tag:
        audit("Теги отчётов", f"+ «{tag}» → {RTYPE_RU.get(rtype, rtype)}")
    return redirect(url_for("upload"))


@app.route("/reports/tag/remove", methods=["POST"])
def reports_tag_remove():
    rtype = (request.form.get("rtype") or "").strip()
    tag = (request.form.get("tag") or "").strip()
    storage.report_tag_remove(rtype, tag)
    if rtype and tag:
        audit("Теги отчётов", f"− «{tag}» из {RTYPE_RU.get(rtype, rtype)}")
    return redirect(url_for("upload"))


@app.route("/reset", methods=["POST"])
def reset():
    active = appconfig.get("active_period", "")
    if active:
        storage.delete_period(active)
        flash(f"Отчёты периода «{active}» сброшены (удалены, в т.ч. из истории). "
              "Почты врачей/зав. отделениями и настройки сохранены.", "ok")
        audit("Сброс отчётов", f"период «{active}» удалён (в т.ч. из истории)")
    else:
        storage.reset_reports()
        flash("Рабочие данные очищены. Почты и настройки сохранены.", "ok")
        audit("Сброс отчётов", "рабочие данные очищены")
    return redirect(url_for("upload"))


@app.route("/period/new", methods=["POST"])
def period_new():
    storage.new_period()
    flash("Начата новая выгрузка — загрузите отчёты нового периода. "
          "Прежняя выгрузка остаётся в истории (можно вернуться).", "ok")
    audit("Новая выгрузка", "рабочие данные очищены, прежняя выгрузка в истории")
    return redirect(url_for("upload"))


@app.route("/period/delete_report", methods=["POST"])
def period_delete_report():
    rtype = (request.form.get("rtype") or "").strip()
    active = appconfig.get("active_period", "")
    storage.delete_report(active, rtype)
    flash(f"Отчёт «{RTYPE_RU.get(rtype, rtype)}» удалён из периода.", "ok")
    audit("Удаление отчёта", f"«{RTYPE_RU.get(rtype, rtype)}» из выгрузки «{active}»")
    return redirect(url_for("upload"))


@app.route("/period/switch", methods=["POST"])
def period_switch():
    period = (request.form.get("period") or "").strip()
    n = storage.switch_period(period)
    if n:
        flash(f"Переключено на период «{period}» (загружено отчётов: {n}).", "ok")
        audit("Переключение выгрузки", f"→ «{period}» ({n} отч.)")
    else:
        flash(f"Для периода «{period}» нет сохранённых отчётов.", "warn")
    return redirect(request.referrer or url_for("upload"))


@app.route("/period/delete", methods=["POST"])
def period_delete():
    period = (request.form.get("period") or "").strip()
    if period:
        storage.delete_period(period)
        flash(f"Выгрузка «{period}» удалена из истории.", "ok")
        audit("Удаление выгрузки", f"«{period}» (из истории)")
    return redirect(request.referrer or url_for("upload"))


@app.route("/reprocess", methods=["POST"])
def reprocess():
    """Заново разбирает сохранённые файлы активного периода (после обновления парсера)."""
    period = appconfig.get("active_period", "")
    if not period:
        flash("Нет активного периода. Сначала загрузите отчёты.", "warn")
        audit("Переобработка", f"выгрузка «{period}»: {n} отч.")
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
    return render_template("doctors.html", docs=docs, order=order)


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
            storage.log_send(it["log_vrach"], it.get("to", ""), it["cnt"], msg,
                             kind=it.get("kind", ""), subject=it.get("subject", ""),
                             period=it.get("period", ""), by_user=job.get("by", ""))
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
    _send_q.put({"items": items, "label": label, "by": _acting_user()})
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
        audit("Отмена рассылки", f"снято из очереди: {drained}")
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
            storage.log_send(vrach, "", len(debts), "нет адреса",
                             kind="Долги врачам", period=rep, by_user=_acting_user())
            continue
        subj = f"Неподписанные документы РЭМД: {len(debts)} шт." + (f" (период {rep})" if rep else "")
        items.append({"to": email, "log_vrach": vrach, "cnt": len(debts), "subject": subj,
                      "kind": "Долги врачам", "period": rep,
                      "html": mailer.build_debt_html(vrach, debts, rep, cust)})
    if items:
        _dispatch_batch(items, "doctors")
        audit("Запуск рассылки", f"Долги врачам: {len(items)} писем")
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
    audit("Почты врачей", f"сохранено: {n}")
    return redirect(url_for("doctors"))


@app.route("/doctor/<path:vrach>")
def doctor_detail(vrach):
    return render_template("doctor_detail.html", vrach=vrach,
                           debts=storage.doctor_debts(vrach),
                           bd=storage.doctor_breakdown(vrach))


@app.route("/emd")
def emd_analytics():
    """Аналитика ЭМД поверх витрины первички: любой период без новых выгрузок."""
    import datetime as _dt
    bounds = storage.emd_bounds()
    presets = []
    if bounds["hi"]:
        hi = _dt.date.fromisoformat(bounds["hi"])
        week_start = hi - _dt.timedelta(days=hi.isoweekday() - 1)
        q_start = _dt.date(hi.year, 3 * ((hi.month - 1) // 3) + 1, 1)
        presets = [
            ("Всё", "", ""),
            ("Год", f"{hi.year}-01-01", bounds["hi"]),
            ("Квартал", q_start.isoformat(), bounds["hi"]),
            ("Месяц", hi.replace(day=1).isoformat(), bounds["hi"]),
            ("Неделя", week_start.isoformat(), bounds["hi"]),
        ]
    dfrom = (request.args.get("from") or "").strip()
    dto = (request.args.get("to") or "").strip()
    return render_template("emd.html",
                           s=storage.emd_summary(dfrom, dto),
                           cov=storage.emd_coverage(),
                           bounds=bounds, presets=presets,
                           dfrom=dfrom, dto=dto)


@app.route("/departments")
def departments():
    return render_template("departments.html",
                           depts=storage.dept_summary(),
                           resp=storage.resp_list("dept"))


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
        cnt = d["nepodp"]
        emails = _split_emails(request.form.get(f"email__{podr}"))
        if not emails:
            noaddr += 1
            storage.log_send(f"[отд.] {podr}", "", cnt, "нет адреса",
                             kind="Сводки заведующим", period=rep, by_user=_acting_user())
            continue
        subj = f"Неподписанные документы по подразделению: {cnt} шт." + (f" (период {rep})" if rep else "")
        items.append({"to": ", ".join(emails), "log_vrach": f"[отд.] {podr}", "cnt": cnt, "subject": subj,
                      "kind": "Сводки заведующим", "period": rep,
                      "html": mailer.build_dept_html(podr, d["vrachi"], d["nepodp"], rep_period=rep, custom=cust)})
    if items:
        _dispatch_batch(items, "depts")
        audit("Запуск рассылки", f"Сводки заведующим: {len(items)} писем")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Запущена пакетная рассылка: {len(items)} писем в фоне."
          + (f" Без адреса: {noaddr}." if noaddr else "") + " Результат — в журнале." + dry,
          "ok" if items else "warn")
    return redirect(url_for("departments"))


@app.route("/departments/report/send", methods=["POST"])
def departments_report_send():
    resp = storage.resp_list("dept")
    if not resp:
        flash("Не заданы получатели сводного отчёта — добавьте на странице «Отделения».", "warn")
        return redirect(url_for("departments"))
    depts = storage.dept_summary()
    if not depts:
        flash("Отчёт «в разрезе врачей» не загружен.", "warn")
        return redirect(url_for("departments"))
    rep = storage.report_period("vrachi")
    html = mailer.build_dept_report_html(depts, rep, appconfig.get("CUSTOM_DEPT", ""))
    subj = "Сводный отчёт по подписанию СЭМД в разрезе подразделений" + (f" — период {rep}" if rep else "")
    to = ", ".join(r["email"] for r in resp)
    ok, msg = mailer.send(to, subj, html)
    storage.log_send("[отделения-свод]", to, len(depts), msg, kind="Свод по подразделениям",
                     subject=subj, period=rep, by_user=_acting_user())
    audit("Отправка отчёта", f"Свод по подразделениям → {to}: {msg}")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Сводный отчёт по подразделениям ({to}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("departments"))


@app.route("/report/send", methods=["POST"])
def report_send():
    resp = storage.resp_list("err")
    if not resp:
        flash("Не заданы получатели отчёта об ошибках — добавьте на странице «Ошибки».", "warn")
        return redirect(request.referrer or url_for("errors"))
    rep = storage.report_period("vrachi")
    data = {"funnel": storage.funnel(),
            "errors": storage.errors_summary()["by_code"],
            "unassigned": storage.unassigned_summary(),
            "docerr": storage.docerr_list(),
            "period": rep}
    html = mailer.build_report_html(data, appconfig.get("CUSTOM_ERR", ""))
    subj = "Отчёт по проблемам РЭМД (ответственному за исправление)" + (f" — период {rep}" if rep else "")
    to = ", ".join(r["email"] for r in resp)
    ok, msg = mailer.send(to, subj, html)
    storage.log_send("[отчёт] ошибки РЭМД", to, len(data["unassigned"]), msg, kind="Ошибки РЭМД",
                     subject=subj, period=rep, by_user=_acting_user())
    audit("Отправка отчёта", f"Ошибки РЭМД → {to}: {msg}")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Отчёт об ошибках ({to}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(request.referrer or url_for("errors"))


@app.route("/errors")
def errors():
    return render_template("errors.html", e=storage.errors_summary(),
                           unassigned=storage.unassigned_summary(),
                           docerr=storage.docerr_list(),
                           resp=storage.resp_list("err"))


@app.route("/fap")
def fap():
    return render_template("fap.html", rows=storage.fap_list(), s=storage.fap_summary(),
                           resp=storage.resp_list("fap"))


@app.route("/fap/report/send", methods=["POST"])
def fap_report_send():
    resp = storage.resp_list("fap")
    if not resp:
        flash("Не заданы получатели отчёта по ФАП — добавьте на странице «ФАП».", "warn")
        return redirect(url_for("fap"))
    s = storage.fap_summary()
    if not s:
        flash("Отчёт по ФАП не загружен.", "warn")
        return redirect(url_for("fap"))
    rep = storage.report_period("fap")
    html = mailer.build_fap_report_html(s, storage.fap_list(), rep, appconfig.get("CUSTOM_FAP", ""))
    subj = "Отчёт по работе ФАП в ЭМК" + (f" — период {rep}" if rep else "")
    to = ", ".join(r["email"] for r in resp)
    ok, msg = mailer.send(to, subj, html)
    storage.log_send("[ФАП-отчёт]", to, s.get("n", 0), msg, kind="Отчёт по ФАП",
                     subject=subj, period=rep, by_user=_acting_user())
    audit("Отправка отчёта", f"Отчёт по ФАП → {to}: {msg}")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Отчёт по ФАП ({to}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("fap"))


@app.route("/max")
def max_page():
    return render_template("max.html",
                           totals=storage.max_totals(),
                           by_doctor=storage.max_by_doctor(),
                           by_position=storage.max_by_position(),
                           by_purpose=storage.max_by_purpose(),
                           resp=storage.resp_list("max"),
                           period=storage.report_period("max"))


@app.route("/max/report/send", methods=["POST"])
def max_report_send():
    resp = storage.resp_list("max")
    if not resp:
        flash("Не заданы получатели отчёта MAX — добавьте на странице «MAX».", "warn")
        return redirect(url_for("max_page"))
    totals = storage.max_totals()
    if not totals:
        flash("Отчёт MAX не загружен.", "warn")
        return redirect(url_for("max_page"))
    rep = storage.report_period("max")
    html = mailer.build_max_report_html(totals, storage.max_by_doctor(),
                                        storage.max_by_purpose(), rep,
                                        appconfig.get("CUSTOM_MAX", ""))
    subj = "Сводный отчёт: ТМК через чат-бот MAX" + (f" — период {rep}" if rep else "")
    to = ", ".join(r["email"] for r in resp)
    ok, msg = mailer.send(to, subj, html)
    storage.log_send("[MAX-отчёт]", to, totals.get("n_doctors", 0), msg, kind="Отчёт MAX",
                     subject=subj, period=rep, by_user=_acting_user())
    audit("Отправка отчёта", f"Отчёт MAX → {to}: {msg}")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Отчёт MAX ({to}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("max_page"))


@app.route("/xray")
def xray():
    return render_template("xray.html",
                           totals=storage.xray_totals(),
                           rows=storage.xray_list(),
                           resp=storage.resp_list("xray"),
                           period=storage.report_period("xray"))


@app.route("/xray/report/send", methods=["POST"])
def xray_report_send():
    resp = storage.resp_list("xray")
    if not resp:
        flash("Не заданы получатели отчёта по рентгену — добавьте на странице «Рентген».", "warn")
        return redirect(url_for("xray"))
    totals = storage.xray_totals()
    if not totals:
        flash("Отчёт по обработке лучевых исследований ИИ не загружен.", "warn")
        return redirect(url_for("xray"))
    rep = storage.report_period("xray")
    html = mailer.build_xray_report_html(totals, storage.xray_list(), rep,
                                         appconfig.get("CUSTOM_XRAY", ""))
    subj = "Сводный отчёт: обработка лучевых исследований ИИ" + (f" — период {rep}" if rep else "")
    to = ", ".join(r["email"] for r in resp)
    ok, msg = mailer.send(to, subj, html)
    storage.log_send("[Рентген-отчёт]", to, totals.get("total", 0), msg, kind="Отчёт по рентгену",
                     subject=subj, period=rep, by_user=_acting_user())
    audit("Отправка отчёта", f"Отчёт по рентгену → {to}: {msg}")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Отчёт по рентгену ({to}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("xray"))


@app.route("/koiki")
def koiki():
    cum = storage.koiki_cumulative()
    return render_template("koiki.html",
                           wards=storage.koiki_list(),
                           totals=storage.koiki_totals(),
                           resp=storage.resp_list("koiki"),
                           period=storage.report_period("koiki"),
                           cum=cum,
                           cum_vyp={r["otdelenie"]: r["vypoln"] for r in cum["rows"]})


@app.route("/koiki/plan")
def koiki_plan():
    cum = storage.koiki_cumulative()
    return render_template("koiki_plan.html",
                           plans=storage.koiki_plan_list([r["otdelenie"] for r in cum["rows"]]),
                           cum=cum)


@app.route("/koiki/plan/save", methods=["POST"])
def koiki_plan_save():
    groups = set(storage.koiki_groups())
    saved = 0
    for k, v in request.form.items():
        if k.startswith("year__"):
            name = k[len("year__"):]
            if name in groups:
                storage.set_koiki_group_plan(name, v or 0)
            else:
                storage.set_koiki_plan(name, v or 0)
            saved += 1
    flash(f"Годовой план госпитализаций сохранён ({saved} строк).", "ok")
    audit("План госпитализаций", f"строк: {saved}")
    return redirect(url_for("koiki_plan"))


@app.route("/koiki/group/create", methods=["POST"])
def koiki_group_create():
    name = (request.form.get("grp_name") or "").strip()
    picks = [p for p in request.form.getlist("pick") if (p or "").strip()]
    if not name or len(picks) < 2:
        flash("Укажите название группы и отметьте не менее двух отделений.", "warn")
        return redirect(url_for("koiki_plan"))
    storage.koiki_group_create(name, picks)
    flash(f"Отделения объединены в группу «{name}» ({len(picks)}). План теперь считается по группе.", "ok")
    audit("Группа отделений", f"создана «{name}»: {len(picks)} отделений")
    return redirect(url_for("koiki_plan"))


@app.route("/koiki/group/disband", methods=["POST"])
def koiki_group_disband():
    name = (request.form.get("grp") or "").strip()
    if name:
        storage.koiki_group_disband(name)
        audit("Группа отделений", f"расформирована «{name}»")
        flash(f"Группа «{name}» разъединена — отделения снова считаются по отдельности.", "ok")
    return redirect(url_for("koiki_plan"))


@app.route("/koiki/save_map", methods=["POST"])
def koiki_save_map():
    resp_items = {k[len("resp__"):]: v for k, v in request.form.items() if k.startswith("resp__")}
    email_items = {k[len("email__"):]: v for k, v in request.form.items() if k.startswith("email__")}
    plan_items = {k[len("plan__"):]: v for k, v in request.form.items() if k.startswith("plan__")}
    ods = set(resp_items) | set(email_items) | set(plan_items)
    for od in ods:
        storage.set_koiki_resp(od, resp_items.get(od, ""), email_items.get(od, ""))
        if od in plan_items:
            storage.set_koiki_plan(od, plan_items.get(od, 0))
    flash(f"Сохранено по отделениям: {len(ods)} (ответственные, почта, план).", "ok" if ods else "warn")
    audit("Стационары: карта отделений", f"сохранено: {len(ods)} (ответственные, почта, план)")
    return redirect(url_for("koiki"))


@app.route("/koiki/send", methods=["POST"])
def koiki_send():
    picked = request.form.getlist("pick")
    if not picked:
        flash("Не выбрано ни одного отделения.", "warn")
        return redirect(url_for("koiki"))
    rep = storage.report_period("koiki")
    days = storage.koiki_totals()["days"]
    cust = appconfig.get("CUSTOM_KOIKI", "")
    _cum = storage.koiki_cumulative()
    cum_vyp = ({r["otdelenie"]: r.get("vypoln") for r in _cum["rows"]}
               if _cum.get("rows") else None)
    wmap = {w["otdelenie"]: w for w in storage.koiki_list()}
    # группируем выбранные отделения по e-mail (инлайн из формы) — одному ответственному одно письмо
    groups, noaddr = {}, 0
    for od in picked:
        w = wmap.get(od)
        if not w:
            continue
        email = (request.form.get(f"email__{od}") or "").strip()
        if not email:
            noaddr += 1
            continue
        resp = (request.form.get(f"resp__{od}") or "").strip()
        g = groups.setdefault(email.lower(), {"email": email, "resp": resp, "wards": []})
        g["wards"].append(w)
    items = []
    for g in groups.values():
        subj = "Занятость коек по отделениям" + (f" (период {rep})" if rep else "")
        items.append({"to": g["email"], "log_vrach": f"[стационары] {g['resp'] or g['email']}",
                      "cnt": len(g["wards"]), "subject": subj,
                      "kind": "Стационары ответственным", "period": rep,
                      "html": mailer.build_koiki_resp_html(g["resp"], g["wards"], days, rep, cust, cum_vyp)})
    if items:
        _dispatch_batch(items, "koiki")
        audit("Запуск рассылки", f"Стационары ответственным: {len(items)} писем")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Запущена рассылка: {len(items)} писем ({len(picked)} отд.)."
          + (f" Без почты пропущено: {noaddr}." if noaddr else "") + dry,
          "ok" if items else "warn")
    return redirect(url_for("koiki"))


@app.route("/koiki/report/send", methods=["POST"])
def koiki_report_send():
    resp = storage.resp_list("koiki")
    if not resp:
        flash("Не заданы получатели сводного отчёта — добавьте на странице «Стационары».", "warn")
        return redirect(url_for("koiki"))
    wards = storage.koiki_list()
    if not wards:
        flash("Отчёт по койкам не загружен.", "warn")
        return redirect(url_for("koiki"))
    rep = storage.report_period("koiki")
    html = mailer.build_koiki_overall_html(wards, storage.koiki_totals(), rep,
                                           appconfig.get("CUSTOM_KOIKI", ""), storage.koiki_cumulative())
    subj = "Сводный отчёт: занятость коечного фонда" + (f" — период {rep}" if rep else "")
    to = ", ".join(r["email"] for r in resp)
    ok, msg = mailer.send(to, subj, html)
    storage.log_send("[стационары-свод]", to, len(wards), msg, kind="Свод по стационарам",
                     subject=subj, period=rep, by_user=_acting_user())
    audit("Отправка отчёта", f"Свод по стационарам → {to}: {msg}")
    dry = " (DRYRUN)" if mailer.is_dryrun() else ""
    flash(f"Сводный отчёт по стационарам ({to}): {msg}.{dry}", "ok" if ok else "warn")
    return redirect(url_for("koiki"))


@app.route("/send/all", methods=["POST"])
def send_all():
    """Одна кнопка: ставит в очередь ВСЕ рассылки по загруженным отчётам активной
    выгрузки — по сохранённым адресам (врачи, зав. отделениями, ответственные).
    Всё уходит через общую очередь с троттлингом; прогресс — чип в шапке."""
    if not mailer.configured():
        flash("SMTP не настроен — откройте «Настройки».", "warn")
        return redirect(url_for("upload"))
    loaded = {m["rtype"] for m in storage.meta_all()}
    if not loaded:
        flash("Нет загруженных отчётов — рассылать нечего.", "warn")
        return redirect(url_for("upload"))
    queued, skipped = [], []

    def q(items, label, title):
        if items:
            _dispatch_batch(items, label)
            queued.append(f"{title} — {len(items)}")

    def report_to(kind, title, cnt, subject, html, period=""):
        """Сводный отчёт ответственным (одним письмом) через общую очередь."""
        resp = storage.resp_list(kind)
        if not resp:
            skipped.append(f"{title}: нет получателей")
            return
        to = ", ".join(r["email"] for r in resp)
        q([{"to": to, "log_vrach": f"[{title}]", "cnt": cnt, "subject": subject,
            "kind": title, "period": period, "html": html}],
          kind, title)

    # Долги врачам — каждому его список неподписанных (по сохранённым почтам)
    if "debts" in loaded:
        rep = storage.report_period("debts")
        cust = appconfig.get("CUSTOM_DEBT", "")
        items, noaddr = [], 0
        for d in storage.doctors("nepodp"):
            debts = storage.doctor_debts(d["vrach"])
            if not debts:
                continue
            if not d.get("email"):
                noaddr += 1
                continue
            subj = f"Неподписанные документы РЭМД: {len(debts)} шт." + (f" (период {rep})" if rep else "")
            items.append({"to": d["email"], "log_vrach": d["vrach"], "cnt": len(debts),
                          "subject": subj, "kind": "Долги врачам", "period": rep,
                          "html": mailer.build_debt_html(d["vrach"], debts, rep, cust)})
        q(items, "doctors", "Долги врачам")
        if noaddr:
            skipped.append(f"врачи без почты: {noaddr}")

    # Сводки заведующим отделениями + сводный отчёт по подразделениям
    if "vrachi" in loaded:
        rep = storage.report_period("vrachi")
        cust = appconfig.get("CUSTOM_DEPT", "")
        depts = storage.dept_summary()
        items, noaddr = [], 0
        for dpt in depts:
            if not dpt["vrachi"]:
                continue
            emails = _split_emails(dpt.get("email"))
            if not emails:
                noaddr += 1
                continue
            subj = (f"Неподписанные документы по подразделению: {dpt['nepodp']} шт."
                    + (f" (период {rep})" if rep else ""))
            items.append({"to": ", ".join(emails), "log_vrach": f"[отд.] {dpt['podr']}",
                          "cnt": dpt["nepodp"], "subject": subj,
                          "kind": "Сводки заведующим", "period": rep,
                          "html": mailer.build_dept_html(dpt["podr"], dpt["vrachi"], dpt["nepodp"],
                                                         rep_period=rep, custom=cust)})
        q(items, "depts", "Сводки заведующим")
        if noaddr:
            skipped.append(f"отделения без почты: {noaddr}")
        if depts:
            report_to("dept", "Свод по подразделениям", len(depts),
                      "Сводный отчёт по подписанию СЭМД в разрезе подразделений"
                      + (f" — период {rep}" if rep else ""),
                      mailer.build_dept_report_html(depts, rep, cust), period=rep)

    # Ошибки РЭМД — ответственному за исправление
    if {"flk", "docerr"} & loaded:
        rep = storage.report_period("vrachi")
        data = {"funnel": storage.funnel(), "errors": storage.errors_summary()["by_code"],
                "unassigned": storage.unassigned_summary(), "docerr": storage.docerr_list(),
                "period": rep}
        report_to("err", "Ошибки РЭМД", len(data["unassigned"]),
                  "Отчёт по проблемам РЭМД (ответственному за исправление)"
                  + (f" — период {rep}" if rep else ""),
                  mailer.build_report_html(data, appconfig.get("CUSTOM_ERR", "")), period=rep)

    # ФАП — ответственному
    if "fap" in loaded:
        s = storage.fap_summary()
        if s:
            rep = storage.report_period("fap")
            report_to("fap", "Отчёт по ФАП", s.get("n", 0),
                      "Отчёт по работе ФАП в ЭМК" + (f" — период {rep}" if rep else ""),
                      mailer.build_fap_report_html(s, storage.fap_list(), rep,
                                                   appconfig.get("CUSTOM_FAP", "")), period=rep)

    # Стационары: ответственным по отделениям (по сохранённым почтам) + сводный
    if "koiki" in loaded:
        rep = storage.report_period("koiki")
        cust = appconfig.get("CUSTOM_KOIKI", "")
        wards = storage.koiki_list()
        days = storage.koiki_totals()["days"]
        _cum = storage.koiki_cumulative()
        cum_vyp = ({r["otdelenie"]: r.get("vypoln") for r in _cum["rows"]}
                   if _cum.get("rows") else None)
        by_mail, noaddr = {}, 0
        for w in wards:
            email = (w.get("email") or "").strip()
            if not email:
                noaddr += 1
                continue
            g = by_mail.setdefault(email.lower(), {"email": email, "resp": w.get("resp") or "", "wards": []})
            g["wards"].append(w)
        subj = "Занятость коек по отделениям" + (f" (период {rep})" if rep else "")
        items = [{"to": g["email"], "log_vrach": f"[стационары] {g['resp'] or g['email']}",
                  "cnt": len(g["wards"]), "subject": subj,
                  "kind": "Стационары ответственным", "period": rep,
                  "html": mailer.build_koiki_resp_html(g["resp"], g["wards"], days, rep, cust, cum_vyp)}
                 for g in by_mail.values()]
        q(items, "koiki", "Стационары ответственным")
        if noaddr:
            skipped.append(f"отделения (койки) без почты: {noaddr}")
        if wards:
            report_to("koiki", "Свод по стационарам", len(wards),
                      "Сводный отчёт: занятость коечного фонда" + (f" — период {rep}" if rep else ""),
                      mailer.build_koiki_overall_html(wards, storage.koiki_totals(), rep, cust, _cum),
                      period=rep)

    # MAX — ответственному
    if "max" in loaded:
        totals = storage.max_totals()
        if totals:
            rep = storage.report_period("max")
            report_to("max", "Отчёт MAX", totals.get("n_doctors", 0),
                      "Сводный отчёт: ТМК через чат-бот MAX" + (f" — период {rep}" if rep else ""),
                      mailer.build_max_report_html(totals, storage.max_by_doctor(),
                                                   storage.max_by_purpose(), rep,
                                                   appconfig.get("CUSTOM_MAX", "")), period=rep)

    # Рентген — ответственному
    if "xray" in loaded:
        totals = storage.xray_totals()
        if totals:
            rep = storage.report_period("xray")
            report_to("xray", "Отчёт по рентгену", totals.get("total", 0),
                      "Сводный отчёт: обработка лучевых исследований ИИ"
                      + (f" — период {rep}" if rep else ""),
                      mailer.build_xray_report_html(totals, storage.xray_list(), rep,
                                                    appconfig.get("CUSTOM_XRAY", "")), period=rep)

    dry = " (DRYRUN — письма не отправляются)" if mailer.is_dryrun() else ""
    if queued:
        flash("Поставлено в очередь: " + "; ".join(queued) + " (писем). "
              "Прогресс — чип в шапке, результат — в «Журнале»." + dry, "ok")
    else:
        flash("Ничего не отправлено: по загруженным отчётам нет адресатов/получателей." + dry, "warn")
    if skipped:
        flash("Пропущено: " + "; ".join(skipped) + ".", "warn")
    audit("Запуск «Разослать всё»",
          ("в очередь: " + "; ".join(queued)) if queued else "ничего не поставлено")
    return redirect(url_for("upload"))


@app.route("/resp/add", methods=["POST"])
def resp_add():
    report = (request.form.get("report") or "").strip()
    name = (request.form.get("name") or "").strip()
    added = 0
    for email in _split_emails(request.form.get("email")):
        storage.resp_add(report, email, name)
        added += 1
    if added:
        audit("Получатели отчёта", f"{report}: добавлено {added}")
    flash(f"Добавлено получателей: {added}." if added else "Укажите e-mail получателя.",
          "ok" if added else "warn")
    return redirect(request.referrer or url_for("index"))


@app.route("/resp/remove", methods=["POST"])
def resp_remove():
    report = (request.form.get("report") or "").strip()
    email = (request.form.get("email") or "").strip()
    if report and email:
        storage.resp_remove(report, email)
        flash("Получатель удалён.", "ok")
        audit("Получатели отчёта", f"{report}: удалён {email}")
    return redirect(request.referrer or url_for("index"))


@app.route("/log")
def send_log_page():
    rows = storage.send_log(200)
    kinds = sorted({r["kind"] for r in rows if r.get("kind")})
    return render_template("log.html", log=rows, kinds=kinds, stats=storage.send_log_stats())


@app.route("/log/ops")
def ops_log_page():
    return render_template("log_ops.html", rows=storage.ops_log_list(300))


@app.route("/users")
def users_page():
    return render_template("users.html",
                           users=storage.ipa_users_list(),
                           stats=storage.ipa_users_stats(),
                           ipa_ready=ipa.available(),
                           ipa_group=appconfig.get("IPA_GROUP", ""),
                           ipa_last=ipa.last_sync_info())


@app.route("/users/integration")
def users_integration():
    ipacfg = {k: appconfig.get(k, "")
              for k in ("IPA_LDAP_URI", "IPA_BASE_DN", "IPA_BIND_DN", "IPA_GROUP")}
    ipacfg["IPA_AUTOSYNC"] = appconfig.get_bool("IPA_AUTOSYNC", False)
    ipacfg["IPA_SYNC_HOURS"] = appconfig.get("IPA_SYNC_HOURS", "24")
    ipacfg["pass_set"] = appconfig.is_set("IPA_BIND_PW")
    return render_template("users_integration.html",
                           ipacfg=ipacfg,
                           stats=storage.ipa_users_stats(),
                           ipa_ready=ipa.available(),
                           ipa_last=ipa.last_sync_info())


@app.route("/users/eisz")
def users_eisz():
    return render_template("users_eisz.html",
                           rec=storage.eisz_reconcile(),
                           eisz=storage.eisz_list())


@app.route("/users/eisz/upload", methods=["POST"])
def users_eisz_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Файл выгрузки ЕИСЗ не выбран.", "warn")
        return redirect(url_for("users_eisz"))
    try:
        text = f.read().decode("utf-8", errors="replace")
        recs = report_parser.parse_birt_eisz(text)
        if not recs:
            flash("В файле не найдено записей сотрудников — это точно выгрузка ЕИСЗ (BIRT)?", "warn")
            return redirect(url_for("users_eisz"))
        storage.set_eisz_users(recs)
        flash(f"Загружена выгрузка ЕИСЗ ПК: {len(recs)} записей (рабочих мест).", "ok")
        audit("Загрузка выгрузки ЕИСЗ", f"записей: {len(recs)}")
    except Exception as e:
        flash(f"Ошибка разбора выгрузки: {e}", "warn")
    return redirect(url_for("users_eisz"))


def _write_xlsx(headers, rows, sheet="Лист1"):
    """Минимальная запись .xlsx на stdlib (inline strings), без openpyxl."""
    import io, zipfile, re as _re
    from xml.sax.saxutils import escape
    def colref(n):
        s = ""
        while n > 0:
            n, r = divmod(n - 1, 26); s = chr(65 + r) + s
        return s
    def rowxml(ri, vals):
        cells = "".join(
            f'<c r="{colref(ci+1)}{ri}" t="inlineStr"><is><t xml:space="preserve">'
            f'{escape("" if v is None else str(v))}</t></is></c>' for ci, v in enumerate(vals))
        return f'<row r="{ri}">{cells}</row>'
    body = "".join(rowxml(i + 1, v) for i, v in enumerate([headers] + list(rows)))
    safe = _re.sub(r'[:\\/?*\[\]]', " ", str(sheet))[:31] or "Лист1"
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 f'<sheetData>{body}</sheetData></worksheet>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{escape(safe)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
              '</Relationships>')
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    bio.seek(0)
    return bio


@app.route("/users/eisz/export/<block>")
def users_eisz_export(block):
    """Выгрузка выбранного блока сверки ЕИСЗ в .xlsx."""
    rec = storage.eisz_reconcile()
    if block == "eisz":
        head = ["ФИО", "СНИЛС", "Подразделение", "Должность", "Ставка", "Начало работы", "Окончание работы", "ФРМО"]
        rows = [[e["fio"], e["snils"], e["podr"], e["position"], e["stavka"], e["start"], e["endwork"], e["frmo"]]
                for e in storage.eisz_list()]
        title, fn = "ЕИСЗ — все записи", "eisz_vse_zapisi.xlsx"
    elif block == "access":
        head = ["ФИО", "СНИЛС", "Должность(и)", "Подразделение(я)"]
        rows = [[p["fio"], p["snils"], p["positions"], p["podrs"]] for p in rec["has_access"]]
        title, fn = "Есть доступ ЕИСЗ", "eisz_est_dostup.xlsx"
    elif block == "delete":
        head = ["ФИО", "СНИЛС", "Должность(и)", "Подразделение(я)", "Причина"]
        rows = []
        for p in rec["to_delete"]:
            reason = (["нет в штате"] if not p["in_staff"] else []) + \
                     (["уволен" + (f" ({p['end']})" if p["end"] else "")] if p["terminated"] else [])
            rows.append([p["fio"], p["snils"], p["positions"], p["podrs"], "; ".join(reason)])
        title, fn = "Профили на удаление", "eisz_na_udalenie.xlsx"
    elif block == "noaccess":
        head = ["ФИО", "Логин", "Должность", "Подразделение", "Почта"]
        rows = [[s["cn"], s["uid"], s["title"], s["ou"], s["mail"]] for s in rec["no_access"]]
        title, fn = "Штат без доступа ЕИСЗ", "eisz_bez_dostupa.xlsx"
    else:
        flash("Неизвестный блок выгрузки.", "warn")
        return redirect(url_for("users_eisz"))
    return send_file(_write_xlsx(head, rows, title), as_attachment=True, download_name=fn,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/users/sync", methods=["POST"])
def users_sync():
    if not ipa.available():
        flash("FreeIPA не настроен — задайте параметры на вкладке «Интеграция».", "warn")
        return redirect(url_for("users_integration"))
    try:
        loaded, matched = ipa.run_sync_and_record()
        grp = appconfig.get("IPA_GROUP", "")
        src = f" (из группы «{grp}»)" if grp else ""
        flash(f"FreeIPA{src}: загружено {loaded} учёток, сопоставлено врачам {matched}.", "ok")
        audit("Синхронизация FreeIPA", f"загружено {loaded}, сопоставлено {matched}")
    except Exception as e:
        flash(f"FreeIPA ошибка: {e}", "warn")
    return redirect(url_for("users_page"))


@app.route("/users/save_ipa", methods=["POST"])
def users_save_ipa():
    """Настройки интеграции FreeIPA (вкладка «Интеграция» страницы «Пользователи»)."""
    for k in ("IPA_LDAP_URI", "IPA_BASE_DN", "IPA_BIND_DN", "IPA_GROUP"):
        appconfig.set(k, (request.form.get(k) or "").strip())
    appconfig.set("IPA_AUTOSYNC", "1" if request.form.get("IPA_AUTOSYNC") else "0")
    appconfig.set("IPA_SYNC_HOURS", (request.form.get("IPA_SYNC_HOURS") or "24").strip())
    pw = request.form.get("IPA_BIND_PW") or ""
    if pw:
        appconfig.set("IPA_BIND_PW", pw)
    flash("Настройки интеграции FreeIPA сохранены.", "ok")
    audit("Настройки FreeIPA", "обновлены")
    return redirect(url_for("users_integration"))


@app.route("/custom/save", methods=["POST"])
def custom_save():
    """Сохраняет произвольный текст письма одного типа (со страницы отчёта)."""
    key = (request.form.get("key") or "").strip()
    if key in CUSTOM_KEYS:
        appconfig.set(key, (request.form.get("value") or "").strip())
        flash("Текст письма сохранён.", "ok")
        audit("Текст письма", key)
    else:
        flash("Неизвестный тип письма.", "warn")
    return redirect(request.form.get("back") or request.referrer or url_for("index"))


@app.route("/departments/save_emails", methods=["POST"])
def departments_save_emails():
    """Сохраняет почты заведующих отделениями (перенесено из Настроек)."""
    saved = 0
    for k, v in request.form.items():
        if k.startswith("email__"):
            storage.set_dept_email(k[len("email__"):], (v or "").strip())
            saved += 1
    flash(f"Сохранены почты отделений ({saved}).", "ok" if saved else "warn")
    audit("Почты заведующих", f"сохранено: {saved}")
    return redirect(url_for("departments"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        if request.form.get("action") == "save_smtp":
            for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_FROM", "SMTP_FROM_NAME",
                      "SMTP_BATCH_DELAY", "SMTP_BATCH_SIZE", "SMTP_BATCH_PAUSE"):
                appconfig.set(k, (request.form.get(k) or "").strip())
            appconfig.set("SMTP_TLS", "1" if request.form.get("SMTP_TLS") else "0")
            appconfig.set("SMTP_DRYRUN", "1" if request.form.get("SMTP_DRYRUN") else "0")
            pw = request.form.get("SMTP_PASS") or ""
            if pw:  # пустое поле — не перезаписываем сохранённый пароль
                appconfig.set("SMTP_PASS", pw)
            flash("Настройки почты сохранены.", "ok")
            audit("Настройки SMTP", "обновлены")
        return redirect(url_for("settings"))

    smtp = {k: appconfig.get(k, "") for k in
            ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_FROM", "SMTP_FROM_NAME")}
    smtp["SMTP_TLS"] = appconfig.get_bool("SMTP_TLS", True)
    smtp["SMTP_DRYRUN"] = appconfig.get_bool("SMTP_DRYRUN", False)
    smtp["pass_set"] = appconfig.is_set("SMTP_PASS")
    smtp["SMTP_BATCH_DELAY"] = appconfig.get("SMTP_BATCH_DELAY", "2")
    smtp["SMTP_BATCH_SIZE"] = appconfig.get("SMTP_BATCH_SIZE", "25")
    smtp["SMTP_BATCH_PAUSE"] = appconfig.get("SMTP_BATCH_PAUSE", "30")
    return render_template("settings.html", smtp=smtp)


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/favicon.ico")
def favicon():
    # Браузеры неявно запрашивают /favicon.ico — отдаём наш SVG-значок.
    return redirect(url_for("static", filename="favicon.svg"))


def _migrate_resp():
    """Разовый перенос одиночных ответственных (RESP_*) в таблицу получателей report_resp."""
    try:
        storage.init()
        if appconfig.get("resp_migrated", ""):
            return
        for rep, ek, nk in (("err", "RESP_EMAIL", "RESP_NAME"),
                            ("fap", "RESP_FAP_EMAIL", "RESP_FAP_NAME"),
                            ("koiki", "RESP_KOIKI_EMAIL", "RESP_KOIKI_NAME")):
            name = appconfig.get(nk, "")
            for email in _split_emails(appconfig.get(ek, "")):
                storage.resp_add(rep, email, name)
        appconfig.set("resp_migrated", "1")
    except Exception:
        pass


_migrate_resp()


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
