# -*- coding: utf-8 -*-
"""
MCP-сервер Центра Цифровизации: доступ ИИ-ассистентов к витрине и сводкам.

Шина контекста для нейронки: инструменты сгруппированы по доменам (emd_*, koiki_*,
staff_* …); будущие модули (бухгалтерия, ЭДО, админ-утилиты) добавляют свои
инструменты в TOOLS — клиент обнаруживает их сам через tools/list.

Транспорт — stdio (JSON-RPC 2.0, MCP 2024-11-05), запускается клиентом:
    python mcp_server.py
Подключение (пример для Claude Code):
    claude mcp add centr -- python C:/путь/до/semd-notify/mcp_server.py

Данные: read-only, каждый вызов пишется в журнал операций (страница «Журнал →
Операции»). Маска ПДн: у пациентов срезаются прямые идентификаторы — ФИО и СНИЛС
(PATIENT_MASK); остальное отдаётся полностью. Врачи и сотрудники — служебный
контекст, не маскируются.
"""
import datetime
import json
import re
import sys

import storage

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "centr-cifrovizacii", "version": "1.0"}
# Прямые идентификаторы пациента: всегда вырезаются из ответов инструментов.
PATIENT_MASK = ("patient", "snils")


def _mask(rows):
    if isinstance(rows, list):
        return [_mask(r) for r in rows]
    if isinstance(rows, dict):
        return {k: v for k, v in rows.items() if k not in PATIENT_MASK}
    return rows


def _period_args(a):
    return (a.get("date_from") or "", a.get("date_to") or "")


# ---------- инструменты: ЭМД (витрина первички) ----------

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


# ---------- инструменты: остальные домены ----------

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
    """Что загружено: активная выгрузка, отчёты в ней, история выгрузок."""
    return {"active_period": storage.cfg_get("active_period") or "",
            "loaded": storage.meta_all(),
            "history": storage.periods_history()}


def send_log_recent(a):
    """Журнал рассылок: последние записи (кому, что, статус, кто запустил)."""
    return storage.send_log(int(a.get("limit") or 30))


def ops_log_recent(a):
    """Журнал операций менеджера: кто и что делал (загрузки, настройки, рассылки)."""
    return storage.ops_log_list(int(a.get("limit") or 30))


_PERIOD_PROPS = {
    "date_from": {"type": "string", "description": "начало периода ГГГГ-ММ-ДД (пусто = без ограничения)"},
    "date_to": {"type": "string", "description": "конец периода ГГГГ-ММ-ДД (пусто = без ограничения)"},
}
_LIMIT_PROP = {"limit": {"type": "integer", "description": "максимум строк"}}


def _schema(props=None):
    return {"type": "object", "properties": props or {}, "additionalProperties": False}


TOOLS = [
    ("emd_summary", emd_summary, _schema(_PERIOD_PROPS)),
    ("emd_error_docs", emd_error_docs, _schema({**_PERIOD_PROPS, **_LIMIT_PROP})),
    ("emd_errors_by_doctor", emd_errors_by_doctor, _schema({**_PERIOD_PROPS, **_LIMIT_PROP})),
    ("emd_coverage", emd_coverage, _schema()),
    ("emd_signing_gap", emd_signing_gap, _schema()),
    ("emd_doctors_debts", emd_doctors_debts, _schema(
        {"order": {"type": "string", "enum": ["nepodp", "pct", "vrach"]}, **_LIMIT_PROP})),
    ("koiki_summary", koiki_summary, _schema()),
    ("fap_summary", fap_summary, _schema()),
    ("max_summary", max_summary, _schema()),
    ("xray_summary", xray_summary, _schema()),
    ("staff_stats", staff_stats, _schema()),
    ("reports_status", reports_status, _schema()),
    ("send_log_recent", send_log_recent, _schema(_LIMIT_PROP)),
    ("ops_log_recent", ops_log_recent, _schema(_LIMIT_PROP)),
]
_BY_NAME = {n: fn for n, fn, _ in TOOLS}


def _tools_payload():
    return [{"name": n, "description": (fn.__doc__ or "").strip(),
             "inputSchema": schema} for n, fn, schema in TOOLS]


def _audit(tool, args):
    try:
        storage.log_op("MCP-ассистент", "MCP: вызов инструмента",
                       f"{tool}({json.dumps(args, ensure_ascii=False)})")
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
                           "capabilities": {"tools": {}},
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
        fn = _BY_NAME.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"неизвестный инструмент: {name}"}}
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
