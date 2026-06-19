# -*- coding: utf-8 -*-
"""Хранилище SQLite: загруженные данные отчётов, маппинг почт, журнал рассылки."""
import os
import sqlite3
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta(
            rtype TEXT PRIMARY KEY, filename TEXT, period TEXT, uploaded_at TEXT, rows INTEGER);
        CREATE TABLE IF NOT EXISTS vrachi(
            podrazdelenie TEXT, vrach TEXT, snils TEXT, doc_type TEXT,
            sform INTEGER, podp INTEGER, nepodp INTEGER, zareg INTEGER);
        CREATE TABLE IF NOT EXISTS debts(
            vrach TEXT, patient TEXT, birth TEXT, case_no TEXT,
            d_start TEXT, d_end TEXT, doc_type TEXT);
        CREATE TABLE IF NOT EXISTS errors(
            fio TEXT, snils TEXT, req_type TEXT, code TEXT, descr TEXT, extra TEXT);
        CREATE TABLE IF NOT EXISTS email_map(key TEXT PRIMARY KEY, email TEXT);
        CREATE TABLE IF NOT EXISTS send_log(
            ts TEXT, vrach TEXT, email TEXT, cnt INTEGER, status TEXT);
        CREATE TABLE IF NOT EXISTS mo_funnel(k TEXT PRIMARY KEY, v INTEGER);
        CREATE TABLE IF NOT EXISTS tvsp(tvsp TEXT, total INTEGER, loaded INTEGER);
        CREATE TABLE IF NOT EXISTS dept_map(podr TEXT PRIMARY KEY, email TEXT);
        CREATE TABLE IF NOT EXISTS notrans(k TEXT PRIMARY KEY, v INTEGER);
        CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT);
        """)


def cfg_get(key):
    init()
    with _conn() as c:
        r = c.execute("SELECT v FROM config WHERE k=?", (key,)).fetchone()
    return r["v"] if r else None


def cfg_set(key, val):
    init()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO config(k,v) VALUES(?,?)", (key, "" if val is None else str(val)))


def replace_report(rtype, filename, period, nrows, records):
    """Заменяет данные отчёта данного типа на свежие."""
    init()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?,?,?)",
                  (rtype, filename, period, datetime.datetime.now().isoformat(timespec="seconds"), nrows))
        if rtype == "vrachi":
            c.execute("DELETE FROM vrachi")
            c.executemany(
                "INSERT INTO vrachi VALUES(:podrazdelenie,:vrach,:snils,:doc_type,:sform,:podp,:nepodp,:zareg)",
                records)
        elif rtype == "debts":
            c.execute("DELETE FROM debts")
            c.executemany(
                "INSERT INTO debts VALUES(:vrach,:patient,:birth,:case_no,:d_start,:d_end,:doc_type)",
                records)
        elif rtype == "flk":
            c.execute("DELETE FROM errors")
            c.executemany(
                "INSERT INTO errors VALUES(:fio,:snils,:req_type,:code,:descr,:extra)",
                records)
        elif rtype == "mo":
            c.execute("DELETE FROM mo_funnel")
            if records:
                c.executemany("INSERT INTO mo_funnel VALUES(?,?)",
                              list(records[0].items()))
        elif rtype == "tvsp":
            c.execute("DELETE FROM tvsp")
            c.executemany("INSERT INTO tvsp VALUES(:tvsp,:total,:loaded)", records)
        elif rtype == "notrans":
            c.execute("DELETE FROM notrans")
            if records:
                c.executemany("INSERT INTO notrans VALUES(?,?)", list(records[0].items()))


def meta_all():
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM meta ORDER BY rtype")]


def funnel():
    """Воронка из отчёта по врачам: сформировано / подписано / зарегистрировано."""
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(sform),0) s, COALESCE(SUM(podp),0) p, "
                        "COALESCE(SUM(nepodp),0) n, COALESCE(SUM(zareg),0) z FROM vrachi").fetchone()
    s, p, n, z = row["s"], row["p"], row["n"], row["z"]
    pct = lambda a, b: round(100 * a / b, 1) if b else 0.0
    return {"sform": s, "podp": p, "nepodp": n, "zareg": z,
            "pct_podp": pct(p, s), "pct_zareg": pct(z, s)}


def doctors(order="nepodp"):
    """Сводка по врачам: агрегаты + долги + почта."""
    with _conn() as c:
        agg = c.execute("""
            SELECT vrach, MAX(snils) snils,
                   SUM(sform) sform, SUM(podp) podp, SUM(nepodp) nepodp, SUM(zareg) zareg
            FROM vrachi WHERE vrach<>'' GROUP BY vrach""").fetchall()
        debts = {r["vrach"]: r["c"] for r in c.execute(
            "SELECT vrach, COUNT(*) c FROM debts GROUP BY vrach")}
        emap = {r["key"]: r["email"] for r in c.execute("SELECT * FROM email_map")}
    out, seen = [], set()
    for r in agg:
        sform = r["sform"] or 0
        pct = round(100 * (r["podp"] or 0) / sform, 1) if sform else 0.0
        email = emap.get((r["snils"] or "").replace(" ", "")) or emap.get(_norm(r["vrach"])) or ""
        seen.add(r["vrach"])
        out.append({
            "vrach": r["vrach"], "snils": r["snils"] or "",
            "sform": sform, "podp": r["podp"] or 0, "nepodp": r["nepodp"] or 0,
            "zareg": r["zareg"] or 0, "pct": pct,
            "debts": debts.get(r["vrach"], 0), "email": email,
        })
    # врачи, которые есть только в отчёте долгов (нет в отчёте «в разрезе врачей»):
    # без них долги «висят» и их нельзя разослать
    for vrach, cnt in debts.items():
        if vrach in seen:
            continue
        out.append({
            "vrach": vrach, "snils": "",
            "sform": 0, "podp": 0, "nepodp": 0, "zareg": 0, "pct": 0.0,
            "debts": cnt, "email": emap.get(_norm(vrach)) or "",
        })
    key = {"nepodp": lambda x: (-x["nepodp"], -x["debts"]), "pct": lambda x: x["pct"],
           "vrach": lambda x: x["vrach"]}.get(order, lambda x: (-x["nepodp"], -x["debts"]))
    out.sort(key=key)
    return out


def mo_funnel():
    """Полная воронка из отчёта «в разрезе МО» (если загружен): включает шаг «подпись МО»."""
    with _conn() as c:
        d = {r["k"]: r["v"] for r in c.execute("SELECT * FROM mo_funnel")}
    if not d.get("sform"):
        return None
    s = d["sform"]
    pct = lambda a: round(100 * a / s, 1) if s else 0.0
    d["pct_podp_vrach"] = pct(d.get("podp_vrach", 0))
    d["pct_podp_mo"] = pct(d.get("podp_mo", 0))
    d["pct_zareg"] = pct(d.get("zareg", 0))
    # «застряли»: подписаны врачом, но не подписаны МО
    d["gap_vrach_mo"] = max(0, d.get("podp_vrach", 0) - d.get("podp_mo", 0))
    return d


def notrans_get():
    with _conn() as c:
        d = {r["k"]: r["v"] for r in c.execute("SELECT * FROM notrans")}
    return d if d.get("total") is not None else None


def tvsp_list():
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM tvsp")]
    for r in rows:
        r["pct"] = round(100 * (r["loaded"] or 0) / r["total"], 1) if r["total"] else 0.0
    rows.sort(key=lambda x: (x["pct"], -x["total"]))
    return rows


def dept_summary():
    """Сводка по подразделениям: агрегаты подписания + долги + врачи + почта зав. отделением."""
    with _conn() as c:
        rows = c.execute("""
            SELECT podrazdelenie podr, vrach,
                   SUM(sform) s, SUM(podp) p, SUM(nepodp) n
            FROM vrachi WHERE podrazdelenie<>'' AND vrach<>''
            GROUP BY podrazdelenie, vrach""").fetchall()
        debts = {r["vrach"]: r["c"] for r in c.execute(
            "SELECT vrach, COUNT(*) c FROM debts GROUP BY vrach")}
        dmap = {r["podr"]: r["email"] for r in c.execute("SELECT * FROM dept_map")}
    depts = {}
    for r in rows:
        d = depts.setdefault(r["podr"], {"podr": r["podr"], "sform": 0, "podp": 0,
                                         "nepodp": 0, "debts": 0, "vrachi": []})
        d["sform"] += r["s"] or 0
        d["podp"] += r["p"] or 0
        d["nepodp"] += r["n"] or 0
        dbt = debts.get(r["vrach"], 0)
        d["debts"] += dbt
        if (r["n"] or 0) or dbt:
            d["vrachi"].append({"vrach": r["vrach"], "nepodp": r["n"] or 0, "debts": dbt})
    out = []
    for d in depts.values():
        d["pct"] = round(100 * d["podp"] / d["sform"], 1) if d["sform"] else 0.0
        d["email"] = dmap.get(d["podr"], "")
        d["vrachi"].sort(key=lambda x: -x["nepodp"])
        out.append(d)
    out.sort(key=lambda x: -x["nepodp"])
    return out


def dept_vrachi(podr):
    for d in dept_summary():
        if d["podr"] == podr:
            return d
    return None


def set_dept_email(podr, email):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO dept_map(podr,email) VALUES(?,?)", (podr, email))


def doctor_debts(vrach):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM debts WHERE vrach=? ORDER BY d_start", (vrach,))]


def doctor_breakdown(vrach):
    """Разбивка врача по видам документов из отчёта «в разрезе врачей»
    (сформировано/подписано/не подписано/в РЭМД) + итоги и подразделение."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT doc_type, SUM(sform) sform, SUM(podp) podp, "
            "SUM(nepodp) nepodp, SUM(zareg) zareg FROM vrachi WHERE vrach=? "
            "GROUP BY doc_type ORDER BY nepodp DESC, sform DESC", (vrach,))]
        pod = c.execute("SELECT podrazdelenie p FROM vrachi WHERE vrach=? "
                        "AND podrazdelenie<>'' LIMIT 1", (vrach,)).fetchone()
    total = {k: sum(r[k] or 0 for r in rows) for k in ("sform", "podp", "nepodp", "zareg")}
    return {"rows": rows, "total": total, "podrazdelenie": pod["p"] if pod else ""}


def errors_summary():
    with _conn() as c:
        by_code = [dict(r) for r in c.execute(
            "SELECT code, COUNT(*) c FROM errors GROUP BY code ORDER BY c DESC")]
        by_person = [dict(r) for r in c.execute(
            "SELECT fio, snils, COUNT(*) c FROM errors GROUP BY fio ORDER BY c DESC LIMIT 50")]
        samples = [dict(r) for r in c.execute(
            "SELECT code, descr, COUNT(*) c FROM errors GROUP BY code, descr ORDER BY c DESC LIMIT 20")]
    return {"by_code": by_code, "by_person": by_person, "samples": samples}


def set_email(key, email):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO email_map(key,email) VALUES(?,?)", (key, email))


def bulk_set_emails(pairs):
    with _conn() as c:
        c.executemany("INSERT OR REPLACE INTO email_map(key,email) VALUES(?,?)", pairs)


def log_send(vrach, email, cnt, status):
    with _conn() as c:
        c.execute("INSERT INTO send_log VALUES(?,?,?,?,?)",
                  (datetime.datetime.now().isoformat(timespec="seconds"), vrach, email, cnt, status))


def send_log(limit=100):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM send_log ORDER BY ts DESC LIMIT ?", (limit,))]


def _norm(fio):
    return " ".join((fio or "").upper().split())
