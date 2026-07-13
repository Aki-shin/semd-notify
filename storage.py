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
            d_start TEXT, d_end TEXT, doc_type TEXT, otdelenie TEXT);
        CREATE TABLE IF NOT EXISTS errors(
            fio TEXT, snils TEXT, req_type TEXT, code TEXT, descr TEXT, extra TEXT);
        CREATE TABLE IF NOT EXISTS email_map(key TEXT PRIMARY KEY, email TEXT);
        CREATE TABLE IF NOT EXISTS send_log(
            ts TEXT, vrach TEXT, email TEXT, cnt INTEGER, status TEXT);
        CREATE TABLE IF NOT EXISTS dept_map(podr TEXT PRIMARY KEY, email TEXT);
        CREATE TABLE IF NOT EXISTS notrans(k TEXT PRIMARY KEY, v INTEGER);
        CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS fap(
            fap TEXT, internet TEXT, fio TEXT, visits INTEGER, visits_doc INTEGER,
            pct INTEGER, naprav INTEGER, recipes INTEGER, naznach INTEGER, eln INTEGER,
            telemed INTEGER, er INTEGER);
        CREATE TABLE IF NOT EXISTS vidy(
            doc_type TEXT, zareg INTEGER, sent INTEGER, err_sync INTEGER, err_reg INTEGER, total INTEGER);
        CREATE TABLE IF NOT EXISTS docerr(
            doc_type TEXT, not_found INTEGER, validation INTEGER, position INTEGER, total INTEGER);
        CREATE TABLE IF NOT EXISTS status(status TEXT, count INTEGER);
        CREATE TABLE IF NOT EXISTS koiki(
            otdelenie TEXT PRIMARY KEY, koek INTEGER, kd INTEGER, nach INTEGER,
            postup INTEGER, vyp INTEGER, pered INTEGER, umer INTEGER, kon INTEGER, day INTEGER);
        CREATE TABLE IF NOT EXISTS koiki_map(otdelenie TEXT PRIMARY KEY, resp TEXT, email TEXT);
        CREATE TABLE IF NOT EXISTS koiki_plan(otdelenie TEXT PRIMARY KEY, plan INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS report_resp(report TEXT, email TEXT, name TEXT DEFAULT '', PRIMARY KEY(report, email));
        CREATE TABLE IF NOT EXISTS report_cfg(rtype TEXT PRIMARY KEY, required INTEGER, comment TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS period_files(
            period TEXT, rtype TEXT, filename TEXT, uploaded_at TEXT, data BLOB,
            PRIMARY KEY(period, rtype));
        """)
        # миграция старых БД: колонка отделения в таблице долгов
        cols = {r["name"] for r in c.execute("PRAGMA table_info(debts)")}
        if "otdelenie" not in cols:
            c.execute("ALTER TABLE debts ADD COLUMN otdelenie TEXT DEFAULT ''")
        fcols = {r["name"] for r in c.execute("PRAGMA table_info(fap)")}
        for col in ("telemed", "er"):
            if col not in fcols:
                c.execute(f"ALTER TABLE fap ADD COLUMN {col} INTEGER DEFAULT 0")
        kcols = {r["name"] for r in c.execute("PRAGMA table_info(koiki)")}
        if "pered" not in kcols:
            c.execute("ALTER TABLE koiki ADD COLUMN pered INTEGER DEFAULT 0")


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
                "INSERT INTO debts(vrach,patient,birth,case_no,d_start,d_end,doc_type,otdelenie) "
                "VALUES(:vrach,:patient,:birth,:case_no,:d_start,:d_end,:doc_type,:otdelenie)",
                records)
        elif rtype == "flk":
            c.execute("DELETE FROM errors")
            c.executemany(
                "INSERT INTO errors VALUES(:fio,:snils,:req_type,:code,:descr,:extra)",
                records)
        elif rtype == "notrans":
            c.execute("DELETE FROM notrans")
            if records:
                c.executemany("INSERT INTO notrans VALUES(?,?)", list(records[0].items()))
        elif rtype == "fap":
            c.execute("DELETE FROM fap")
            c.executemany(
                "INSERT INTO fap(fap,internet,fio,visits,visits_doc,pct,naprav,recipes,"
                "naznach,eln,telemed,er) VALUES(:fap,:internet,:fio,:visits,:visits_doc,"
                ":pct,:naprav,:recipes,:naznach,:eln,:telemed,:er)", records)
        elif rtype == "vidy":
            c.execute("DELETE FROM vidy")
            c.executemany("INSERT INTO vidy VALUES(:doc_type,:zareg,:sent,:err_sync,:err_reg,:total)", records)
        elif rtype == "docerr":
            c.execute("DELETE FROM docerr")
            c.executemany("INSERT INTO docerr VALUES(:doc_type,:not_found,:validation,:position,:total)", records)
        elif rtype == "status":
            c.execute("DELETE FROM status")
            c.executemany("INSERT INTO status VALUES(:status,:count)", records)
        elif rtype == "koiki":
            c.execute("DELETE FROM koiki")
            c.executemany(
                "INSERT OR REPLACE INTO koiki(otdelenie,koek,kd,nach,postup,vyp,pered,umer,kon,day) "
                "VALUES(:otdelenie,:koek,:kd,:nach,:postup,:vyp,:pered,:umer,:kon,:day)", records)


def meta_all():
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM meta ORDER BY rtype")]


def report_period(rtype):
    """Нормализованный период отчёта данного типа (для указания в письмах).
    Если типа нет — общий активный период."""
    import parser
    init()
    with _conn() as c:
        r = c.execute("SELECT period FROM meta WHERE rtype=?", (rtype,)).fetchone()
    if r and r["period"]:
        return parser.norm_period(r["period"]) or r["period"]
    return cfg_get("active_period") or ""


def reset_reports():
    """Удаляет данные всех загруженных отчётов (для загрузки нового периода).
    СОХРАНЯЕТ справочные данные: почты врачей и зав. отделениями, журнал рассылки,
    настройки SMTP/FreeIPA."""
    init()
    with _conn() as c:
        for t in ("meta", "vrachi", "debts", "errors", "notrans",
                  "fap", "vidy", "docerr", "status", "koiki"):
            c.execute(f"DELETE FROM {t}")


# --- История периодов: храним сырые файлы по периодам, можно вернуться и выгрузить ---

def save_period_file(period, rtype, filename, data):
    """Сохраняет сырой загруженный файл в историю (по периоду и типу)."""
    init()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO period_files(period,rtype,filename,uploaded_at,data) "
                  "VALUES(?,?,?,?,?)",
                  (period, rtype, filename,
                   datetime.datetime.now().isoformat(timespec="seconds"), sqlite3.Binary(data)))


def periods_history():
    """Список сохранённых периодов (для переключения), новые сверху, активный помечен."""
    init()
    active = cfg_get("active_period") or ""
    with _conn() as c:
        rows = c.execute("SELECT period, COUNT(*) n, MAX(uploaded_at) ts "
                         "FROM period_files GROUP BY period ORDER BY ts DESC").fetchall()
    return [{"period": r["period"], "n": r["n"], "ts": r["ts"], "active": r["period"] == active}
            for r in rows]


def period_rtypes(period):
    """Типы отчётов, сохранённые для периода (для ссылок выгрузки)."""
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT rtype, filename FROM period_files WHERE period=? ORDER BY rtype", (period,))]


def period_file(period, rtype):
    """(имя файла, байты) сохранённого отчёта — для выгрузки обратно."""
    init()
    with _conn() as c:
        r = c.execute("SELECT filename, data FROM period_files WHERE period=? AND rtype=?",
                      (period, rtype)).fetchone()
    return (r["filename"], bytes(r["data"])) if r else (None, None)


def switch_period(period):
    """Переключает рабочие данные на сохранённый период: чистит таблицы и
    заново разбирает сырые файлы этого периода. Справочники (почты) не трогает."""
    import parser, tempfile, os as _os
    init()
    with _conn() as c:
        files = [(r["rtype"], r["filename"], bytes(r["data"]))
                 for r in c.execute("SELECT rtype, filename, data FROM period_files WHERE period=?", (period,))]
    if not files:
        return 0
    reset_reports()
    n = 0
    for rtype, fn, data in files:
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".xls")
        tf.write(data); tf.close()
        try:
            res = parser.parse(tf.name)
            replace_report(rtype, fn, res["period"], res["rows"], res["records"])
            n += 1
        except Exception:
            pass
        finally:
            try:
                _os.unlink(tf.name)
            except OSError:
                pass
    cfg_set("active_period", period)
    return n


# rtype → рабочая таблица данных (для точечного удаления одного отчёта)
RTYPE_TABLE = {
    "vrachi": "vrachi", "debts": "debts", "flk": "errors",
    "notrans": "notrans", "fap": "fap", "vidy": "vidy",
    "docerr": "docerr", "status": "status", "koiki": "koiki",
}


def clear_report(rtype):
    """Удаляет один тип отчёта из рабочих таблиц (meta + таблица данных)."""
    init()
    tbl = RTYPE_TABLE.get(rtype)
    with _conn() as c:
        c.execute("DELETE FROM meta WHERE rtype=?", (rtype,))
        if tbl:
            c.execute(f"DELETE FROM {tbl}")


def delete_report(period, rtype):
    """Удаляет отчёт данного типа: из истории периода и (если период активный) из рабочих таблиц."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM period_files WHERE period=? AND rtype=?", (period, rtype))
    if (cfg_get("active_period") or "") == period:
        clear_report(rtype)


def delete_period(period):
    """Полностью удаляет период: из истории; если активный — чистит рабочие таблицы
    и сбрасывает active_period. Другие периоды в истории не трогает."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM period_files WHERE period=?", (period,))
    if (cfg_get("active_period") or "") == period:
        reset_reports()
        cfg_set("active_period", "")


def new_period():
    """Начинает новый период: чистит рабочие таблицы и сбрасывает active_period.
    История периодов (period_files) сохраняется — на неё можно вернуться."""
    init()
    reset_reports()
    cfg_set("active_period", "")


def periods_info():
    """Сводка по периодам загруженных отчётов: общий период и согласованность."""
    import parser
    init()
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT rtype, period FROM meta")]
    by_period = {}
    for r in rows:
        np = parser.norm_period(r["period"])
        if np:
            by_period.setdefault(np, []).append(r["rtype"])
    periods = list(by_period)
    return {
        "by_period": by_period,
        "consistent": len(periods) <= 1,
        "period": periods[0] if len(periods) == 1 else "",
        "n_reports": len(rows),
    }


def funnel():
    """Воронка из отчёта по врачам: сформировано / подписано / зарегистрировано."""
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(sform),0) s, COALESCE(SUM(podp),0) p, "
                        "COALESCE(SUM(nepodp),0) n, COALESCE(SUM(zareg),0) z FROM vrachi").fetchone()
    s, p, n, z = row["s"], row["p"], row["n"], row["z"]
    pct = lambda a, b: round(100 * a / b, 1) if b else 0.0
    return {"sform": s, "podp": p, "nepodp": n, "zareg": z,
            "pct_podp": pct(p, s), "pct_zareg": pct(z, s)}


def _is_unassigned(vrach):
    """Документы без конкретного врача («Не указан», пусто) — слать врачу нельзя."""
    v = (vrach or "").strip().lower()
    return v == "" or v.startswith("не указан")


def unassigned_summary():
    """Неподписанные документы без указанного врача (для отчёта ответственному):
    разбивка по виду документа из отчёта «в разрезе врачей»."""
    with _conn() as c:
        rows = c.execute(
            "SELECT vrach, doc_type, SUM(nepodp) nepodp FROM vrachi "
            "GROUP BY vrach, doc_type HAVING SUM(nepodp)>0").fetchall()
    out = [{"vrach": r["vrach"] or "(пусто)", "doc_type": r["doc_type"], "nepodp": r["nepodp"]}
           for r in rows if _is_unassigned(r["vrach"])]
    out.sort(key=lambda x: -x["nepodp"])
    return out


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
            "unassigned": _is_unassigned(r["vrach"]),
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
            "unassigned": _is_unassigned(vrach),
        })
    key = {"nepodp": lambda x: (-x["nepodp"], -x["debts"]), "pct": lambda x: x["pct"],
           "vrach": lambda x: x["vrach"]}.get(order, lambda x: (-x["nepodp"], -x["debts"]))
    out.sort(key=key)
    return out


def notrans_get():
    with _conn() as c:
        d = {r["k"]: r["v"] for r in c.execute("SELECT * FROM notrans")}
    return d if d.get("total") is not None else None


def vidy_list():
    """Статистика по видам документов (что зарегистрировано/упало), по убыванию объёма."""
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM vidy ORDER BY total DESC")]


def docerr_list():
    """Ошибки по видам документов (для страницы «Ошибки» и отчёта ответственному)."""
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM docerr ORDER BY total DESC")]


def status_list():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM status ORDER BY count DESC")]


def fap_list():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM fap ORDER BY pct ASC, visits DESC")]


def fap_summary():
    """Агрегаты по ФАП: всего фельдшеров, без интернета, средний % заполнения ЭМК."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM fap")]
    if not rows:
        return None
    no_net = sum(1 for r in rows if (r["internet"] or "").strip().lower() in ("нет", "no"))
    def tot(k):
        return sum(r[k] or 0 for r in rows)
    visits, visits_doc = tot("visits"), tot("visits_doc")
    return {"n": len(rows), "no_internet": no_net,
            "visits": visits, "visits_doc": visits_doc,
            "pct": round(100 * visits_doc / visits, 1) if visits else 0.0,
            "naprav": tot("naprav"), "recipes": tot("recipes"), "naznach": tot("naznach"),
            "eln": tot("eln"), "telemed": tot("telemed"), "er": tot("er"),
            "low": [r for r in rows if (r["pct"] or 0) < 100]}


# --- Коечный фонд (занятость коек в стационарах) ---

def _koiki_days():
    import parser
    return parser.period_days(report_period("koiki") or "")


def koiki_list():
    """Отделения стационара с рассчитанными показателями:
      zan     — занятость, % = койко-дни / (коек × дни периода) × 100;
      oborot  — оборот койки = выписано / коек;
      dlit    — средняя длительность = койко-дни / выписано;
      overload— койко-дни или пациенты превышают коечный фонд (коек в справочнике занижено);
      no_beds — коек в отчёте 0, а движение есть (койки не заведены в справочнике).
    Плюс ответственный (resp/email) из koiki_map. Сортировка по убыванию занятости."""
    days = _koiki_days()
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM koiki")]
        rmap = {r["otdelenie"]: dict(r) for r in c.execute("SELECT * FROM koiki_map")}
        pmap = {r["otdelenie"]: r["plan"] for r in c.execute("SELECT * FROM koiki_plan")}
    for r in rows:
        koek, kd, vyp = r["koek"], r["kd"], r["vyp"]
        r["zan"] = round(kd / (koek * days) * 100, 1) if koek else None
        r["oborot"] = round(vyp / koek, 1) if koek else None
        r["dlit"] = round(kd / vyp, 1) if vyp else None
        r["overload"] = bool(koek and (kd > koek * days or r["kon"] > koek))
        r["no_beds"] = koek == 0
        m = rmap.get(r["otdelenie"], {})
        r["resp"], r["email"] = m.get("resp", ""), m.get("email", "")
        r["plan_year"] = pmap.get(r["otdelenie"], 0) or 0
        r["plan"] = round(r["plan_year"] / 365 * days) if r["plan_year"] else 0  # план за текущий период
        r["vypoln"] = round(r["postup"] / r["plan"] * 100, 1) if r["plan"] else None
    rows.sort(key=lambda x: (x["zan"] is None, -(x["zan"] or 0)))
    return rows


def koiki_totals():
    """Итоги по учреждению: всего / круглосуточные / дневные (места считаем отдельно),
    плюс счётчики отделений с перевыполнением (>100%) и недозагрузкой (<80%)."""
    days = _koiki_days()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT otdelenie, koek, kd, day, postup, vyp, pered, umer FROM koiki")]
        pmap = {r["otdelenie"]: (r["plan"] or 0) for r in c.execute("SELECT * FROM koiki_plan")}

    def agg(sel):
        k = sum(r["koek"] for r in rows if sel(r))
        d = sum(r["kd"] for r in rows if sel(r))
        return {"koek": k, "kd": d, "zan": round(d / (k * days) * 100, 1) if k else None}
    over = low = 0
    for r in rows:
        if not r["koek"]:
            continue
        z = r["kd"] / (r["koek"] * days) * 100
        if z > 100:
            over += 1
        elif z < 80:
            low += 1
    # итоги движения пациентов по учреждению
    mov = {k: sum(r[k] or 0 for r in rows) for k in ("postup", "vyp", "pered", "umer")}
    # выполнение плана за текущий период: годовой план пропорционально дням; факт — только по отд. с планом
    plan_period = sum(round((pmap.get(r["otdelenie"], 0) or 0) / 365 * days)
                      for r in rows if pmap.get(r["otdelenie"], 0))
    plan_fact = sum(r["postup"] or 0 for r in rows if pmap.get(r["otdelenie"], 0))
    plan_vypoln = round(plan_fact / plan_period * 100, 1) if plan_period else None
    return {"days": days, "n": len(rows), "over": over, "low": low, "mov": mov,
            "plan": plan_period, "plan_fact": plan_fact, "plan_vypoln": plan_vypoln,
            "all": agg(lambda r: True),
            "kruglo": agg(lambda r: not r["day"]),
            "day": agg(lambda r: r["day"])}


def set_koiki_resp(otdelenie, resp, email):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO koiki_map(otdelenie,resp,email) VALUES(?,?,?)",
                  (otdelenie, (resp or "").strip(), (email or "").strip()))


# --- Получатели сводных отчётов (несколько на отчёт). report: 'err' | 'fap' | 'koiki' ---

def resp_list(report):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT email, name FROM report_resp WHERE report=? ORDER BY name, email", (report,))]


def resp_add(report, email, name=""):
    email = (email or "").strip()
    if not email:
        return
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO report_resp(report,email,name) VALUES(?,?,?)",
                  (report, email, (name or "").strip()))


def resp_remove(report, email):
    with _conn() as c:
        c.execute("DELETE FROM report_resp WHERE report=? AND email=?", (report, (email or "").strip()))


# --- Пользовательские настройки отчётов на «Загрузке»: тип (основной/доп.) + комментарий ---

def report_cfg_all():
    with _conn() as c:
        return {r["rtype"]: {"required": r["required"], "comment": r["comment"] or ""}
                for r in c.execute("SELECT * FROM report_cfg")}


def set_report_cfg(rtype, required, comment):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO report_cfg(rtype,required,comment) VALUES(?,?,?)",
                  (rtype, 1 if required else 0, (comment or "").strip()))


# --- План госпитализаций по отделениям (стационары) ---

def set_koiki_plan(otdelenie, plan):
    try:
        p = int(float(str(plan).replace(",", ".")))
    except (ValueError, TypeError):
        p = 0
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO koiki_plan(otdelenie,plan) VALUES(?,?)", (otdelenie, p))


def koiki_plan_list():
    """Отделения с годовым планом госпитализаций и производными (месяц = год/12, неделя = год/52).
    Отделения берём из текущей загрузки коек + из справочника планов."""
    with _conn() as c:
        ods = [r["otdelenie"] for r in c.execute("SELECT otdelenie FROM koiki ORDER BY otdelenie")]
        pmap = {r["otdelenie"]: (r["plan"] or 0) for r in c.execute("SELECT * FROM koiki_plan")}
    for od in pmap:
        if od not in ods:
            ods.append(od)
    return [{"otdelenie": od, "year": pmap.get(od, 0),
             "month": round(pmap.get(od, 0) / 12) if pmap.get(od) else 0,
             "week": round(pmap.get(od, 0) / 52) if pmap.get(od) else 0} for od in ods]


def koiki_cumulative():
    """Сводное выполнение плана за ВСЕ загруженные периоды коек.
    Пересекающиеся периоды де-дублируются (жадно: приоритет раньше начавшимся и более длинным),
    пробелы между периодами показываются явно. План годовой, пропорционально числу покрытых дней."""
    import parser, tempfile, os as _os
    with _conn() as c:
        blobs = [(r["period"], bytes(r["data"])) for r in
                 c.execute("SELECT period, data FROM period_files WHERE rtype='koiki'")]
        pmap = {r["otdelenie"]: (r["plan"] or 0) for r in c.execute("SELECT * FROM koiki_plan")}
    periods = []
    for per, data in blobs:
        fd, tmp = tempfile.mkstemp(suffix=".xls"); _os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            res = parser.parse(tmp)
        except Exception:
            res = None
        finally:
            try:
                _os.remove(tmp)
            except OSError:
                pass
        if not res:
            continue
        np = parser.norm_period(res.get("period") or per)
        parts = np.split(" — ")
        start = parser._date(parts[0])
        end = parser._date(parts[-1]) if len(parts) > 1 else start
        if not start:
            continue
        end = end or start
        periods.append({"start": start, "end": end, "days": (end - start).days + 1, "label": np or per,
                        "postup": {rec["otdelenie"]: rec["postup"] for rec in res["records"]}})
    periods.sort(key=lambda p: (p["start"], -p["days"]))
    selected, skipped, last_end = [], [], None
    for p in periods:
        if last_end and p["start"] <= last_end:
            skipped.append(p)
        else:
            selected.append(p); last_end = p["end"]
    gaps = []
    for a, b in zip(selected, selected[1:]):
        g = (b["start"] - a["end"]).days - 1
        if g > 0:
            gaps.append({"after": a["end"].strftime("%d.%m.%Y"),
                         "before": b["start"].strftime("%d.%m.%Y"), "days": g})
    covered = sum(p["days"] for p in selected)
    cum = {}
    for p in selected:
        for od, ps in p["postup"].items():
            cum[od] = cum.get(od, 0) + (ps or 0)
    rows, tf, tp = [], 0, 0
    for od in sorted(set(cum) | {k for k, v in pmap.items() if v}):
        fact = cum.get(od, 0); py = pmap.get(od, 0)
        plan_cov = round(py / 365 * covered) if (py and covered) else 0
        vyp = round(fact / plan_cov * 100, 1) if plan_cov else None
        rows.append({"otdelenie": od, "fact": fact, "plan_year": py, "plan_cov": plan_cov, "vypoln": vyp})
        if py:
            tf += fact; tp += plan_cov
    return {"selected": [{"label": p["label"], "days": p["days"]} for p in selected],
            "skipped": [{"label": p["label"], "days": p["days"]} for p in skipped],
            "gaps": gaps, "covered": covered,
            "span": {"start": selected[0]["start"].strftime("%d.%m.%Y"),
                     "end": selected[-1]["end"].strftime("%d.%m.%Y")} if selected else None,
            "rows": sorted(rows, key=lambda x: (x["vypoln"] is None, x["vypoln"] or 0)),
            "tot_fact": tf, "tot_plan": tp,
            "total_vypoln": round(tf / tp * 100, 1) if tp else None}


def dept_summary():
    """Сводка по подразделениям из отчёта «в разрезе врачей»: агрегаты подписания,
    список врачей с неподписанными документами и почта зав. отделением."""
    with _conn() as c:
        rows = c.execute("""
            SELECT podrazdelenie podr, vrach,
                   SUM(sform) s, SUM(podp) p, SUM(nepodp) n
            FROM vrachi WHERE podrazdelenie<>'' AND vrach<>''
            GROUP BY podrazdelenie, vrach""").fetchall()
        dmap = {r["podr"]: r["email"] for r in c.execute("SELECT * FROM dept_map")}
    depts = {}
    for r in rows:
        d = depts.setdefault(r["podr"], {"podr": r["podr"], "sform": 0, "podp": 0,
                                         "nepodp": 0, "vrachi": []})
        d["sform"] += r["s"] or 0
        d["podp"] += r["p"] or 0
        d["nepodp"] += r["n"] or 0
        if r["n"] or 0:
            d["vrachi"].append({"vrach": r["vrach"], "nepodp": r["n"] or 0})
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


def bulk_set_doctor_emails(items):
    """items: список (vrach, email). Сохраняет почту под тем же ключом, по которому её
    ищет doctors(): СНИЛС (без пробелов) при наличии, иначе нормализованное ФИО."""
    with _conn() as c:
        smap = {r["vrach"]: (r["s"] or "").replace(" ", "")
                for r in c.execute("SELECT vrach, MAX(snils) s FROM vrachi GROUP BY vrach")}
        pairs = [(smap.get(v) or _norm(v), (e or "").strip()) for v, e in items]
        c.executemany("INSERT OR REPLACE INTO email_map(key,email) VALUES(?,?)", pairs)
    return len(pairs)


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
