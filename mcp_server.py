# -*- coding: utf-8 -*-
"""
MCP-сервер Центра Цифровизации: доступ ИИ-ассистентов к витрине и действиям.

Шина контекста и действий: инструменты сгруппированы по видам —
  чтение (read)   : сводки, детализация, журналы;
  изменение (write): тексты писем, теги/комментарии, получатели, почты, планы;
  отправка (send) : сводные отчёты ответственным (по умолчанию ВЫКЛЮЧЕНЫ).
Будущие модули (бухгалтерия, ЭДО, админ-утилиты) добавляют свои инструменты в TOOLS —
клиент обнаруживает их сам через tools/list.

Набор доступных инструментов управляется со страницы «Настройки» (раздел
«MCP-инструменты»): выключенные не видны в tools/list и отклоняются в tools/call.
Конфиг читается из БД на каждый запрос — изменения применяются сразу.

Транспорт — stdio (JSON-RPC 2.0, MCP 2024-11-05), запускается клиентом:
    python mcp_server.py
Подключение (пример для Claude Code):
    claude mcp add centr -- python C:/путь/до/semd-notify/mcp_server.py

Маска ПДн: у пациентов всегда срезаются прямые идентификаторы — ФИО и СНИЛС
(PATIENT_MASK); врачи и сотрудники — служебный контекст, не маскируются.
Каждый вызов пишется в журнал операций («Журнал → Операции»).
"""
import datetime
import json
import re
import sys

import storage
import appconfig

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "centr-cifrovizacii", "version": "2.0"}
# Прямые идентификаторы пациента: всегда вырезаются из ответов инструментов.
PATIENT_MASK = ("patient", "snils")
# Ключ конфига: JSON-список выключенных инструментов (правится в «Настройках»).
CFG_OFF = "MCP_TOOLS_OFF"

# Тексты писем, которые разрешено редактировать ассистенту (ключ -> подпись).
LETTER_KEYS = {
    "CUSTOM_DEBT": "письмо врачу о неподписанных документах",
    "CUSTOM_DEPT": "сводка заведующему отделением",
    "CUSTOM_ERR": "отчёт ответственному за ошибки РЭМД",
    "CUSTOM_FAP": "отчёт по ФАП",
    "CUSTOM_KOIKI": "отчёты по стационарам",
    "CUSTOM_MAX": "отчёт MAX",
    "CUSTOM_XRAY": "отчёт по рентгену",
}
RESP_KINDS = ("dept", "err", "fap", "koiki", "max", "xray")


def _mask(rows):
    if isinstance(rows, list):
        return [_mask(r) for r in rows]
    if isinstance(rows, dict):
        return {k: v for k, v in rows.items() if k not in PATIENT_MASK}
    return rows


def _period_args(a):
    return (a.get("date_from") or "", a.get("date_to") or "")


# ================= ЧТЕНИЕ =================

def emd_summary(a):
    """Сводка ЭМД за период: итоги, по видам, ошибки по кодам, по подразделениям."""
    return storage.emd_summary(*_period_args(a))


def emd_error_docs(a):
    """Документы с ошибками за период (детально, без ФИО/СНИЛС пациентов)."""
    return _mask(storage.emd_error_docs(*_period_args(a), limit=int(a.get("limit") or 60)))


def emd_errors_by_doctor(a):
    """Топ врачей по числу документов с ошибками за период."""
    return storage.emd_err_by_vrach(*_period_args(a), limit=int(a.get("limit") or 15))


def emd_coverage(a):
    """Покрытие витрины по неделям (по дате создания) и дырки между ними."""
    return storage.emd_coverage()


def emd_signing_gap(a):
    """Подписной контур: сформировано/подписано врачом (отчёт «в разрезе врачей»)
    против подписано МО (витрина) — оценка прослойки «ждёт подписи МО»."""
    per = storage.report_period("vrachi")
    m = re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", per or "")
    if len(m) < 2:
        return {"available": False, "reason": "отчёт «в разрезе врачей» не загружен"}
    f1 = f"{m[0][2]}-{m[0][1]}-{m[0][0]}"
    t1 = f"{m[1][2]}-{m[1][1]}-{m[1][0]}"
    vt = storage.vrachi_totals()
    mo = storage.emd_summary(f1, t1)["totals"]["n"]
    return dict(vt, available=True, period=per, signed_mo=mo,
                waiting_mo=max(0, vt["podp"] - mo))


def emd_doctors_debts(a):
    """Врачи с неподписанными документами (долги): рейтинг с почтами и числом долгов."""
    docs = storage.doctors(a.get("order") or "nepodp")
    return docs[: int(a.get("limit") or 30)]


def koiki_summary(a):
    """Стационары: занятость коечного фонда — итоги и отделения (с группами)."""
    return {"totals": storage.koiki_totals(), "wards": storage.koiki_list(),
            "period": storage.report_period("koiki")}


def fap_summary(a):
    """ФАП: сводка работы фельдшеров в ЭМК + строки по ФАПам."""
    return {"summary": storage.fap_summary(), "rows": storage.fap_list(),
            "period": storage.report_period("fap")}


def max_summary(a):
    """Телемедицина через чат-бот MAX: итоги и разрезы."""
    return {"totals": storage.max_totals(), "by_purpose": storage.max_by_purpose(),
            "period": storage.report_period("max")}


def xray_summary(a):
    """Обработка лучевых исследований ИИ-сервисом: итоги по модальностям."""
    return {"totals": storage.xray_totals(), "rows": storage.xray_list(),
            "period": storage.report_period("xray")}


def staff_stats(a):
    """Сотрудники: статистика FreeIPA-учёток и сверка с выгрузкой ЕИСЗ ПК."""
    rec = storage.eisz_reconcile()
    return {"ipa": storage.ipa_users_stats(),
            "eisz": {k: len(v) if isinstance(v, list) else v for k, v in rec.items()}}


def reports_status(a):
    """Свежесть данных: какой отчёт каким периодом сейчас в работе, файлы, ожидающие
    указания периода, и история загрузок. Накопительная модель: каждый тип живёт своим
    последним файлом, витрина ЭМД сквозная."""
    import json as _json
    try:
        pend = _json.loads(storage.cfg_get("PENDING_PERIOD") or "[]")
    except ValueError:
        pend = []
    return {"loaded": storage.meta_all(),
            "pending_period": pend,
            "history": storage.history_files()[: int(a.get("limit") or 40)]}


def reporting_period_get(a):
    """Текущий отчётный период: гранулярность (день/неделя/месяц/квартал/полугодие/год/всё)
    и вычисленный диапазон дат. Управляет накопительными витринами (ЭМД)."""
    import app
    return app.reporting_range()


def upload_coverage(a):
    """Карта загрузок по ISO-неделям: какие недели покрыты файлами каждого типа отчёта
    и каких недель не хватает (то же, что «Карта загрузок» на странице Загрузка)."""
    import app
    cov = app._upload_coverage(storage.history_files())
    if not cov:
        return {"weeks": [], "rows": [], "note": "загрузок с датами в периоде нет"}
    return {"weeks": [w["iso"] for w in cov["weeks"]],
            "rows": [{"type": r["rtype"], "title": r["title"],
                      "loaded_weeks": sum(1 for c in r["cells"] if c["st"] == "ok"),
                      "missing_weeks": r["gaps"]} for r in cov["rows"]]}


def send_log_recent(a):
    """Журнал рассылок: последние записи (кому, что, статус, кто запустил)."""
    return storage.send_log(int(a.get("limit") or 30))


def ops_log_recent(a):
    """Журнал операций менеджера: кто и что делал (загрузки, настройки, рассылки)."""
    return storage.ops_log_list(int(a.get("limit") or 30))


def letter_text_get(a):
    """Текущий произвольный текст письма по ключу (см. letter_text_set)."""
    key = (a.get("key") or "").strip()
    if key not in LETTER_KEYS:
        raise ValueError(f"неизвестный ключ; допустимые: {', '.join(LETTER_KEYS)}")
    return {"key": key, "purpose": LETTER_KEYS[key], "text": appconfig.get(key, "")}


# ================= ИЗМЕНЕНИЕ =================

def letter_text_set(a):
    """Задать произвольный текст письма (блок оператора в рассылке). Ключи:
    CUSTOM_DEBT (долги врачу), CUSTOM_DEPT (заведующему), CUSTOM_ERR (ошибки РЭМД),
    CUSTOM_FAP, CUSTOM_KOIKI, CUSTOM_MAX, CUSTOM_XRAY. Пустой text очищает блок."""
    key = (a.get("key") or "").strip()
    if key not in LETTER_KEYS:
        raise ValueError(f"неизвестный ключ; допустимые: {', '.join(LETTER_KEYS)}")
    text = (a.get("text") or "").strip()
    appconfig.set(key, text)
    return {"ok": True, "key": key, "purpose": LETTER_KEYS[key], "length": len(text)}


def reporting_period_set(a):
    """Задать отчётный период. gran: day|week|month|quarter|half|year|all|custom;
    anchor (ГГГГ-ММ-ДД) — опорная дата; для custom — date_from и date_to."""
    gran = (a.get("gran") or "month").strip()
    storage.cfg_set("RPERIOD_GRAN", gran)
    if a.get("anchor"):
        storage.cfg_set("RPERIOD_ANCHOR", str(a["anchor"]).strip())
    if gran == "custom":
        storage.cfg_set("RPERIOD_FROM", (a.get("date_from") or "").strip())
        storage.cfg_set("RPERIOD_TO", (a.get("date_to") or "").strip())
    import app
    return {"ok": True, "range": app.reporting_range()}


def report_comment_set(a):
    """Комментарий к отчёту на странице «Загрузка» (rtype — ключ отчёта)."""
    rtype = (a.get("rtype") or "").strip()
    storage.set_report_comment(rtype, a.get("comment") or "")
    return {"ok": True, "rtype": rtype}


def report_tag_add(a):
    """Добавить тег отчёту (классификация на «Загрузке»)."""
    storage.report_tag_add((a.get("rtype") or "").strip(), a.get("tag") or "")
    return {"ok": True}


def report_tag_remove(a):
    """Убрать тег у отчёта."""
    storage.report_tag_remove((a.get("rtype") or "").strip(), (a.get("tag") or "").strip())
    return {"ok": True}


def responsible_add(a):
    """Добавить получателя сводного отчёта. report: dept | err | fap | koiki | max | xray."""
    report = (a.get("report") or "").strip()
    if report not in RESP_KINDS:
        raise ValueError(f"report должен быть одним из: {', '.join(RESP_KINDS)}")
    email = (a.get("email") or "").strip()
    if not email or "@" not in email:
        raise ValueError("нужен корректный email")
    storage.resp_add(report, email, (a.get("name") or "").strip())
    return {"ok": True, "recipients": storage.resp_list(report)}


def responsible_remove(a):
    """Убрать получателя сводного отчёта."""
    report = (a.get("report") or "").strip()
    if report not in RESP_KINDS:
        raise ValueError(f"report должен быть одним из: {', '.join(RESP_KINDS)}")
    storage.resp_remove(report, (a.get("email") or "").strip())
    return {"ok": True, "recipients": storage.resp_list(report)}


def doctor_email_set(a):
    """Сохранить почту врача (ФИО как в отчётах)."""
    vrach = (a.get("vrach") or "").strip()
    email = (a.get("email") or "").strip()
    if not vrach:
        raise ValueError("нужно ФИО врача (vrach)")
    n = storage.bulk_set_doctor_emails([(vrach, email)])
    return {"ok": True, "saved": n}


def dept_email_set(a):
    """Сохранить почту заведующего отделением (podr — подразделение как в отчётах)."""
    podr = (a.get("podr") or "").strip()
    if not podr:
        raise ValueError("нужно подразделение (podr)")
    storage.set_dept_email(podr, (a.get("email") or "").strip())
    return {"ok": True}


def koiki_responsible_set(a):
    """Стационары: ответственный и почта по отделению (otdelenie как на «Стационарах»)."""
    od = (a.get("otdelenie") or "").strip()
    if not od:
        raise ValueError("нужно отделение (otdelenie)")
    storage.set_koiki_resp(od, (a.get("resp") or "").strip(), (a.get("email") or "").strip())
    return {"ok": True}


def koiki_plan_set(a):
    """Стационары: годовой план госпитализаций отделению или группе."""
    od = (a.get("otdelenie") or "").strip()
    if not od:
        raise ValueError("нужно отделение или группа (otdelenie)")
    plan = int(a.get("plan") or 0)
    if od in set(storage.koiki_groups()):
        storage.set_koiki_group_plan(od, plan)
    else:
        storage.set_koiki_plan(od, plan)
    return {"ok": True, "otdelenie": od, "plan": plan}


# ================= ОТПРАВКА =================

def send_summary_report(a):
    """Отправить сводный отчёт ответственным (получатели — из настроек страниц).
    kind: err (ошибки РЭМД, из витрины) | dept | fap | koiki | max | xray.
    Письма не содержат ПДн пациентов. Запись — в журнал рассылок."""
    import mailer
    kind = (a.get("kind") or "").strip()
    if kind not in RESP_KINDS:
        raise ValueError(f"kind должен быть одним из: {', '.join(RESP_KINDS)}")
    resp = storage.resp_list(kind)
    if not resp:
        raise ValueError(f"не заданы получатели отчёта «{kind}» — добавьте на странице или через responsible_add")
    to = ", ".join(r["email"] for r in resp)

    if kind == "err":
        import app as _app
        letter = _app._emd_err_letter()
        if not letter:
            raise ValueError("витрина пуста — загрузите первички «Состояние» и «Детализация»")
        subj, html, cnt, label = letter
    elif kind == "dept":
        depts = storage.dept_summary()
        if not depts:
            raise ValueError("отчёт «в разрезе врачей» не загружен")
        rep = storage.report_period("vrachi")
        html = mailer.build_dept_report_html(depts, rep, appconfig.get("CUSTOM_DEPT", ""))
        subj = "Сводный отчёт по подписанию СЭМД в разрезе подразделений" + (f" — период {rep}" if rep else "")
        cnt, label = len(depts), rep
    elif kind == "fap":
        s = storage.fap_summary()
        if not s:
            raise ValueError("отчёт по ФАП не загружен")
        rep = storage.report_period("fap")
        html = mailer.build_fap_report_html(s, storage.fap_list(), rep, appconfig.get("CUSTOM_FAP", ""))
        subj = "Отчёт по работе ФАП в ЭМК" + (f" — период {rep}" if rep else "")
        cnt, label = s.get("n", 0), rep
    elif kind == "koiki":
        wards = storage.koiki_list()
        if not wards:
            raise ValueError("отчёт по койкам не загружен")
        rep = storage.report_period("koiki")
        html = mailer.build_koiki_overall_html(wards, storage.koiki_totals(), rep,
                                               appconfig.get("CUSTOM_KOIKI", ""), storage.koiki_cumulative())
        subj = "Сводный отчёт: занятость коечного фонда" + (f" — период {rep}" if rep else "")
        cnt, label = len(wards), rep
    elif kind == "max":
        totals = storage.max_totals()
        if not totals:
            raise ValueError("отчёт MAX не загружен")
        rep = storage.report_period("max")
        html = mailer.build_max_report_html(totals, storage.max_by_doctor(),
                                            storage.max_by_purpose(), rep, appconfig.get("CUSTOM_MAX", ""))
        subj = "Сводный отчёт: ТМК через чат-бот MAX" + (f" — период {rep}" if rep else "")
        cnt, label = totals.get("n_doctors", 0), rep
    else:  # xray
        totals = storage.xray_totals()
        if not totals:
            raise ValueError("отчёт по рентгену не загружен")
        rep = storage.report_period("xray")
        html = mailer.build_xray_report_html(totals, storage.xray_list(), rep, appconfig.get("CUSTOM_XRAY", ""))
        subj = "Сводный отчёт: обработка лучевых исследований ИИ" + (f" — период {rep}" if rep else "")
        cnt, label = totals.get("total", 0), rep

    ok, msg = mailer.send(to, subj, html)
    storage.log_send(f"[MCP] сводный отчёт: {kind}", to, cnt, msg,
                     kind={"err": "Ошибки РЭМД", "dept": "Свод по подразделениям",
                           "fap": "Отчёт по ФАП", "koiki": "Свод по стационарам",
                           "max": "Отчёт MAX", "xray": "Отчёт по рентгену"}[kind],
                     subject=subj, period=label or "", by_user="MCP-ассистент")
    return {"ok": ok, "status": msg, "to": to, "subject": subj,
            "dryrun": mailer.is_dryrun()}


# ================= РЕЕСТР =================

_PERIOD_PROPS = {
    "date_from": {"type": "string", "description": "начало периода ГГГГ-ММ-ДД (пусто = без ограничения)"},
    "date_to": {"type": "string", "description": "конец периода ГГГГ-ММ-ДД (пусто = без ограничения)"},
}
_LIMIT_PROP = {"limit": {"type": "integer", "description": "максимум строк"}}


def _schema(props=None, required=None):
    s = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


# (имя, функция, схема, вид: read | write | send)
TOOLS = [
    ("emd_summary", emd_summary, _schema(_PERIOD_PROPS), "read"),
    ("emd_error_docs", emd_error_docs, _schema({**_PERIOD_PROPS, **_LIMIT_PROP}), "read"),
    ("emd_errors_by_doctor", emd_errors_by_doctor, _schema({**_PERIOD_PROPS, **_LIMIT_PROP}), "read"),
    ("emd_coverage", emd_coverage, _schema(), "read"),
    ("emd_signing_gap", emd_signing_gap, _schema(), "read"),
    ("emd_doctors_debts", emd_doctors_debts, _schema(
        {"order": {"type": "string", "enum": ["nepodp", "pct", "vrach"]}, **_LIMIT_PROP}), "read"),
    ("koiki_summary", koiki_summary, _schema(), "read"),
    ("fap_summary", fap_summary, _schema(), "read"),
    ("max_summary", max_summary, _schema(), "read"),
    ("xray_summary", xray_summary, _schema(), "read"),
    ("staff_stats", staff_stats, _schema(), "read"),
    ("reports_status", reports_status, _schema(), "read"),
    ("upload_coverage", upload_coverage, _schema(), "read"),
    ("reporting_period_get", reporting_period_get, _schema(), "read"),
    ("send_log_recent", send_log_recent, _schema(_LIMIT_PROP), "read"),
    ("ops_log_recent", ops_log_recent, _schema(_LIMIT_PROP), "read"),
    ("letter_text_get", letter_text_get, _schema(
        {"key": {"type": "string", "enum": list(LETTER_KEYS)}}, ["key"]), "read"),
    ("letter_text_set", letter_text_set, _schema(
        {"key": {"type": "string", "enum": list(LETTER_KEYS)},
         "text": {"type": "string", "description": "текст блока оператора (пусто — очистить)"}},
        ["key", "text"]), "write"),
    ("reporting_period_set", reporting_period_set, _schema(
        {"gran": {"type": "string",
                  "enum": ["day", "week", "month", "quarter", "half", "year", "all", "custom"]},
         "anchor": {"type": "string", "description": "опорная дата ГГГГ-ММ-ДД"},
         "date_from": {"type": "string"}, "date_to": {"type": "string"}}, ["gran"]), "write"),
    ("report_comment_set", report_comment_set, _schema(
        {"rtype": {"type": "string"}, "comment": {"type": "string"}}, ["rtype"]), "write"),
    ("report_tag_add", report_tag_add, _schema(
        {"rtype": {"type": "string"}, "tag": {"type": "string"}}, ["rtype", "tag"]), "write"),
    ("report_tag_remove", report_tag_remove, _schema(
        {"rtype": {"type": "string"}, "tag": {"type": "string"}}, ["rtype", "tag"]), "write"),
    ("responsible_add", responsible_add, _schema(
        {"report": {"type": "string", "enum": list(RESP_KINDS)},
         "email": {"type": "string"}, "name": {"type": "string"}}, ["report", "email"]), "write"),
    ("responsible_remove", responsible_remove, _schema(
        {"report": {"type": "string", "enum": list(RESP_KINDS)},
         "email": {"type": "string"}}, ["report", "email"]), "write"),
    ("doctor_email_set", doctor_email_set, _schema(
        {"vrach": {"type": "string", "description": "ФИО врача как в отчётах"},
         "email": {"type": "string"}}, ["vrach", "email"]), "write"),
    ("dept_email_set", dept_email_set, _schema(
        {"podr": {"type": "string"}, "email": {"type": "string"}}, ["podr", "email"]), "write"),
    ("koiki_responsible_set", koiki_responsible_set, _schema(
        {"otdelenie": {"type": "string"}, "resp": {"type": "string"},
         "email": {"type": "string"}}, ["otdelenie"]), "write"),
    ("koiki_plan_set", koiki_plan_set, _schema(
        {"otdelenie": {"type": "string"}, "plan": {"type": "integer"}},
        ["otdelenie", "plan"]), "write"),
    ("send_summary_report", send_summary_report, _schema(
        {"kind": {"type": "string", "enum": list(RESP_KINDS)}}, ["kind"]), "send"),
]
_BY_NAME = {n: (fn, kind) for n, fn, _, kind in TOOLS}
KIND_RU = {"read": "чтение", "write": "изменение", "send": "отправка"}


def tools_meta():
    """Метаданные для страницы «Настройки»: имя, вид, описание, выключен ли."""
    off = disabled_set()
    return [{"name": n, "kind": kind, "descr": (fn.__doc__ or "").strip().split("\n")[0],
             "enabled": n not in off}
            for n, fn, _, kind in TOOLS]


def disabled_set():
    """Выключенные инструменты (из настроек; по умолчанию выключена вся отправка)."""
    raw = storage.cfg_get(CFG_OFF)
    if raw is None:
        return {n for n, _, _, kind in TOOLS if kind == "send"}
    try:
        return set(json.loads(raw))
    except ValueError:
        return set()


def _tools_payload():
    off = disabled_set()
    out = []
    for n, fn, schema, kind in TOOLS:
        if n in off:
            continue
        out.append({"name": n, "description": (fn.__doc__ or "").strip(),
                    "inputSchema": schema,
                    "annotations": {"readOnlyHint": kind == "read",
                                    "destructiveHint": False}})
    return out


def _audit(tool, args, note=""):
    try:
        storage.log_op("MCP-ассистент", "MCP: вызов инструмента",
                       f"{tool}({json.dumps(args, ensure_ascii=False)})" + (f" — {note}" if note else ""))
    except Exception:
        pass


def _json_default(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    return str(o)


def handle(req):
    method = req.get("method", "")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": PROTOCOL,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": SERVER_INFO}}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tools_payload()}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        entry = _BY_NAME.get(name)
        if not entry:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"неизвестный инструмент: {name}"}}
        if name in disabled_set():
            _audit(name, args, "ОТКЛОНЕНО: выключен в настройках")
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text",
                               "text": f"инструмент «{name}» выключен в настройках Центра "
                                       f"(страница «Настройки» → MCP-инструменты)"}],
                               "isError": True}}
        fn, kind = entry
        _audit(name, args)
        try:
            data = fn(args)
            text = json.dumps(data, ensure_ascii=False, default=_json_default)
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": f"ошибка: {e}"}],
                               "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"метод не поддерживается: {method}"}}
    return None


def main():
    storage.init()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        resp = handle(req)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


if __name__ == "__main__":
    main()
