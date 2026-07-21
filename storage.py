# -*- coding: utf-8 -*-
"""Хранилище SQLite: загруженные данные отчётов, маппинг почт, журнал рассылки."""
import os
import sqlite3
import datetime
import zlib

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
        CREATE TABLE IF NOT EXISTS ipa_users(
            uid TEXT PRIMARY KEY, cn TEXT, givenname TEXT, sn TEXT, mail TEXT,
            title TEXT, ou TEXT, phone TEXT, mobile TEXT, empnum TEXT, blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS eisz_users(
            fio TEXT, snils TEXT, frmo TEXT, podr TEXT, position TEXT,
            stavka TEXT, start TEXT, endwork TEXT);
        CREATE TABLE IF NOT EXISTS send_log(
            ts TEXT, vrach TEXT, email TEXT, cnt INTEGER, status TEXT);
        CREATE TABLE IF NOT EXISTS ops_log(ts TEXT, user TEXT, action TEXT, details TEXT);
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
        CREATE TABLE IF NOT EXISTS max_tmk(
            doctor TEXT, position TEXT, purpose TEXT,
            zap INTEGER, zap_max INTEGER, otm INTEGER, otm_max INTEGER,
            prov INTEGER, prov_max INTEGER, bl_max INTEGER);
        CREATE TABLE IF NOT EXISTS xray(
            modality TEXT, total INTEGER, success INTEGER, err INTEGER,
            err_mi INTEGER, err_mo INTEGER, err_conn INTEGER, avg_time REAL);
        CREATE TABLE IF NOT EXISTS koiki_map(otdelenie TEXT PRIMARY KEY, resp TEXT, email TEXT);
        CREATE TABLE IF NOT EXISTS koiki_plan(otdelenie TEXT PRIMARY KEY, plan INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS koiki_group(grp TEXT PRIMARY KEY, plan INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS koiki_group_member(otdelenie TEXT PRIMARY KEY, grp TEXT);
        CREATE TABLE IF NOT EXISTS report_resp(report TEXT, email TEXT, name TEXT DEFAULT '', PRIMARY KEY(report, email));
        CREATE TABLE IF NOT EXISTS report_cfg(rtype TEXT PRIMARY KEY, required INTEGER, comment TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS report_tags(rtype TEXT, tag TEXT, PRIMARY KEY(rtype, tag));
        CREATE TABLE IF NOT EXISTS period_files(
            period TEXT, rtype TEXT, filename TEXT, uploaded_at TEXT, data BLOB,
            PRIMARY KEY(period, rtype));
        CREATE TABLE IF NOT EXISTS emd_docs(
            regnum TEXT, version INTEGER DEFAULT 1, uid TEXT DEFAULT '',
            vid TEXT DEFAULT '', status TEXT DEFAULT '', vrach TEXT DEFAULT '',
            podr TEXT DEFAULT '', oid TEXT DEFAULT '',
            d_created TEXT DEFAULT '', d_signed TEXT DEFAULT '', d_registered TEXT DEFAULT '',
            remd_num TEXT DEFAULT '', err_code TEXT DEFAULT '', err_type TEXT DEFAULT '',
            err_text TEXT DEFAULT '', attempts INTEGER DEFAULT 0, fmt TEXT DEFAULT '',
            days_to_sign REAL, days_to_reg REAL, src_week TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            patient TEXT DEFAULT '', birth TEXT DEFAULT '', snils TEXT DEFAULT '',
            attach_mo TEXT DEFAULT '', uch_type TEXT DEFAULT '', uch_num TEXT DEFAULT '',
            PRIMARY KEY(regnum, version));
        CREATE INDEX IF NOT EXISTS idx_emd_created ON emd_docs(d_created);
        CREATE INDEX IF NOT EXISTS idx_emd_status ON emd_docs(status);
        CREATE INDEX IF NOT EXISTS idx_emd_week ON emd_docs(src_week);
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
        # миграция журнала рассылок: тип, тема, период, кто запустил
        slcols = {r["name"] for r in c.execute("PRAGMA table_info(send_log)")}
        for col in ("kind", "subject", "period", "by_user"):
            if col not in slcols:
                c.execute(f"ALTER TABLE send_log ADD COLUMN {col} TEXT DEFAULT ''")
        # миграция витрины: пациентские поля (полная информация в менеджере)
        emcols = {r["name"] for r in c.execute("PRAGMA table_info(emd_docs)")}
        for col in ("patient", "birth", "snils", "attach_mo", "uch_type", "uch_num"):
            if col not in emcols:
                c.execute(f"ALTER TABLE emd_docs ADD COLUMN {col} TEXT DEFAULT ''")
        # миграция истории файлов: флаг сжатия (большие выгрузки храним в zlib)
        pfcols = {r["name"] for r in c.execute("PRAGMA table_info(period_files)")}
        if "compressed" not in pfcols:
            c.execute("ALTER TABLE period_files ADD COLUMN compressed INTEGER DEFAULT 0")


def cfg_get(key):
    init()
    with _conn() as c:
        r = c.execute("SELECT v FROM config WHERE k=?", (key,)).fetchone()
    return r["v"] if r else None


def cfg_set(key, val):
    init()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO config(k,v) VALUES(?,?)", (key, "" if val is None else str(val)))


def _week_of(iso_date):
    """«ГГГГ-ММ-ДД» -> метка ISO-недели «ГГГГ-Wнн» (для карты покрытия витрины)."""
    try:
        y, w, _ = datetime.date.fromisoformat(iso_date).isocalendar()
        return f"{y}-W{w:02d}"
    except (TypeError, ValueError):
        return ""


def emd_upsert_state(records, c):
    """«Состояние по ЭМД» -> витрина: скелет документа (INSERT или полное обновление).
    Ключ — (рег.№ в региональном реестре, версия). Возвращает (новых, обновлено)."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    before = c.execute("SELECT COUNT(*) n FROM emd_docs").fetchone()["n"]
    c.executemany("""
        INSERT INTO emd_docs(regnum,version,vid,status,vrach,podr,oid,
            d_created,d_signed,d_registered,remd_num,err_code,err_text,
            attempts,fmt,days_to_sign,days_to_reg,src_week,updated_at,
            patient,birth,snils,attach_mo,uch_type,uch_num)
        VALUES(:regnum,:version,:vid,:status,:vrach,:podr,:oid,
            :d_created,:d_signed,:d_registered,:remd_num,:err_code,:err_text,
            :attempts,:fmt,:days_to_sign,:days_to_reg,:src_week,:updated_at,
            :patient,:birth,:snils,:attach_mo,:uch_type,:uch_num)
        ON CONFLICT(regnum, version) DO UPDATE SET
            patient=excluded.patient, birth=excluded.birth, snils=excluded.snils,
            attach_mo=excluded.attach_mo, uch_type=excluded.uch_type, uch_num=excluded.uch_num,
            vid=excluded.vid, status=excluded.status, vrach=excluded.vrach,
            podr=excluded.podr, oid=excluded.oid, d_created=excluded.d_created,
            d_signed=excluded.d_signed, d_registered=excluded.d_registered,
            remd_num=excluded.remd_num, err_code=excluded.err_code,
            err_text=excluded.err_text, attempts=excluded.attempts,
            fmt=excluded.fmt, days_to_sign=excluded.days_to_sign,
            days_to_reg=excluded.days_to_reg, src_week=excluded.src_week,
            updated_at=excluded.updated_at
        """, [dict(r, src_week=_week_of(r["d_created"]), updated_at=now) for r in records])
    after = c.execute("SELECT COUNT(*) n FROM emd_docs").fetchone()["n"]
    ins = after - before
    return ins, max(0, len(records) - ins)


def emd_upsert_detail(records, c):
    """«Детализация отправки» -> витрина: обновляет статус/ошибки/регистрацию
    последней версии документа; неизвестные документы (хвост прошлых недель)
    вставляет со скелетом из самой детализации. Возвращает (новых, обновлено)."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    ins = upd = 0
    for r in records:
        cur = c.execute("""
            UPDATE emd_docs SET
                status=:status, uid=:uid,
                d_registered=CASE WHEN :d_registered<>'' THEN :d_registered ELSE d_registered END,
                remd_num=CASE WHEN :remd_num<>'' THEN :remd_num ELSE remd_num END,
                err_code=:err_code, err_type=:err_type, err_text=:err_text,
                vrach=CASE WHEN vrach='' THEN :vrach ELSE vrach END,
                updated_at=:now
            WHERE regnum=:regnum
              AND version=(SELECT MAX(version) FROM emd_docs WHERE regnum=:regnum)
            """, dict(r, now=now))
        if cur.rowcount:
            upd += 1
        else:
            c.execute("""
                INSERT INTO emd_docs(regnum,version,uid,vid,status,vrach,podr,oid,
                    d_created,d_registered,remd_num,err_code,err_type,err_text,
                    src_week,updated_at)
                VALUES(:regnum,1,:uid,:vid,:status,:vrach,:podr,:oid,
                    :d_doc,:d_registered,:remd_num,:err_code,:err_type,:err_text,
                    :src_week,:now)
                """, dict(r, src_week=_week_of(r["d_doc"]), now=now))
            ins += 1
    return ins, upd


_REG = "status LIKE 'Версия ЭМД успешно зарегистрирована%'"
_ERR = "status LIKE 'Ошибка%'"
_RDY = "status LIKE 'Готов%'"
_SNT = "status LIKE '%отправлена на регистрацию%'"


def _emd_where(dfrom, dto):
    cond, args = "WHERE regnum<>''", []
    if dfrom:
        cond += " AND d_created>=?"
        args.append(dfrom)
    if dto:
        cond += " AND d_created<=?"
        args.append(dto)
    return cond, args


def emd_bounds():
    init()
    with _conn() as c:
        r = c.execute("SELECT MIN(d_created) lo, MAX(d_created) hi, COUNT(*) n "
                      "FROM emd_docs WHERE d_created<>''").fetchone()
    return {"lo": r["lo"] or "", "hi": r["hi"] or "", "n": r["n"]}


def emd_summary(dfrom="", dto=""):
    """Сводка витрины за произвольный период (по дате создания документа)."""
    init()
    W, args = _emd_where(dfrom, dto)
    with _conn() as c:
        t = dict(c.execute(f"""
            SELECT COUNT(*) n, SUM({_REG}) reg, SUM({_ERR}) err,
                   SUM({_RDY}) ready, SUM({_SNT}) sent,
                   ROUND(AVG(CASE WHEN {_REG} THEN days_to_reg END), 1) sla_reg,
                   ROUND(AVG(days_to_sign), 1) sla_sign
            FROM emd_docs {W}""", args).fetchone())
        by_vid = [dict(r) for r in c.execute(f"""
            SELECT vid, COUNT(*) n, SUM({_REG}) reg, SUM({_ERR}) err
            FROM emd_docs {W} GROUP BY vid ORDER BY n DESC LIMIT 15""", args)]
        errors = [dict(r) for r in c.execute(f"""
            SELECT err_code, err_type, COUNT(*) n, MIN(err_text) sample
            FROM emd_docs {W} AND err_code<>'' AND {_ERR}
            GROUP BY err_code, err_type ORDER BY n DESC LIMIT 25""", args)]
        by_podr = [dict(r) for r in c.execute(f"""
            SELECT podr, COUNT(*) n, SUM({_ERR}) err
            FROM emd_docs {W} GROUP BY podr ORDER BY n DESC LIMIT 12""", args)]
    for x in (t, *by_vid, *errors, *by_podr):
        for k, v in list(x.items()):
            if v is None and k != "sample":
                x[k] = 0
    return {"totals": t, "by_vid": by_vid, "errors": errors, "by_podr": by_podr}


def emd_error_docs(dfrom="", dto="", limit=60):
    """Документы с ошибками за период — детально (пациент, врач, код, текст).
    Полная информация для работы в менеджере; в письма эти поля не включаются."""
    init()
    W, args = _emd_where(dfrom, dto)
    with _conn() as c:
        return [dict(r) for r in c.execute(f"""
            SELECT d_created, vid, patient, birth, snils, vrach, podr,
                   err_code, err_text, attempts
            FROM emd_docs {W} AND {_ERR}
            ORDER BY d_created DESC, regnum DESC LIMIT ?""", args + [limit])]


def vrachi_totals():
    """Итог подписного контура из отчёта «в разрезе врачей» (текущая выгрузка)."""
    init()
    with _conn() as c:
        r = c.execute("SELECT SUM(sform) s, SUM(podp) p, SUM(nepodp) np FROM vrachi").fetchone()
    return {"sform": r["s"] or 0, "podp": r["p"] or 0, "nepodp": r["np"] or 0}


def emd_coverage():
    """Недели витрины (по дате создания): загруженные + дырки между ними."""
    init()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(f"""
            SELECT src_week w, COUNT(*) n, SUM({_ERR}) err
            FROM emd_docs WHERE src_week<>'' GROUP BY src_week ORDER BY src_week""")]
    gaps = []
    def _wk(s):
        y, w = s.split("-W")
        return datetime.date.fromisocalendar(int(y), int(w), 1)
    for a, b in zip(rows, rows[1:]):
        d = ( _wk(b["w"]) - _wk(a["w"]) ).days // 7 - 1
        if d > 0:
            gaps.append({"after": a["w"], "before": b["w"], "weeks": d})
    return {"weeks": rows, "gaps": gaps}


def replace_report(rtype, filename, period, nrows, records):
    """Заменяет данные отчёта данного типа на свежие.
    Для первичек РЭМД (state/detail) — не замена, а UPSERT в сквозную витрину emd_docs;
    возвращает {'ins': новых, 'upd': обновлено} (для остальных типов — None)."""
    init()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?,?,?)",
                  (rtype, filename, period, datetime.datetime.now().isoformat(timespec="seconds"), nrows))
        if rtype == "state":
            ins, upd = emd_upsert_state(records, c)
            return {"ins": ins, "upd": upd}
        if rtype == "detail":
            ins, upd = emd_upsert_detail(records, c)
            return {"ins": ins, "upd": upd}
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
        elif rtype == "max":
            c.execute("DELETE FROM max_tmk")
            c.executemany(
                "INSERT INTO max_tmk(doctor,position,purpose,zap,zap_max,otm,otm_max,prov,prov_max,bl_max) "
                "VALUES(:doctor,:position,:purpose,:zap,:zap_max,:otm,:otm_max,:prov,:prov_max,:bl_max)", records)
        elif rtype == "xray":
            c.execute("DELETE FROM xray")
            c.executemany(
                "INSERT INTO xray(modality,total,success,err,err_mi,err_mo,err_conn,avg_time) "
                "VALUES(:modality,:total,:success,:err,:err_mi,:err_mo,:err_conn,:avg_time)", records)


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
                  "fap", "vidy", "docerr", "status", "koiki", "max_tmk", "xray"):
            c.execute(f"DELETE FROM {t}")


# --- История периодов: храним сырые файлы по периодам, можно вернуться и выгрузить ---

def save_period_file(period, rtype, filename, data):
    """Сохраняет сырой загруженный файл в историю (по периоду и типу).
    Крупные файлы (первички РЭМД, десятки МБ XML) сжимаются zlib (~10x)."""
    init()
    comp = 0
    if len(data) > 2 * 1024 * 1024:
        data = zlib.compress(data, 6)
        comp = 1
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO period_files(period,rtype,filename,uploaded_at,data,compressed) "
                  "VALUES(?,?,?,?,?,?)",
                  (period, rtype, filename,
                   datetime.datetime.now().isoformat(timespec="seconds"), sqlite3.Binary(data), comp))


def _ts_human(ts):
    """ISO-метка -> короткий вид для меню («15.07.2026 14:29»)."""
    try:
        return datetime.datetime.fromisoformat(ts).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return (ts or "")[:16]


def periods_history():
    """Список сохранённых периодов (для переключения), новые сверху, активный помечен."""
    init()
    active = cfg_get("active_period") or ""
    with _conn() as c:
        rows = c.execute("SELECT period, COUNT(*) n, MAX(uploaded_at) ts "
                         "FROM period_files GROUP BY period ORDER BY ts DESC").fetchall()
    return [{"period": r["period"], "n": r["n"], "ts": r["ts"], "ts_h": _ts_human(r["ts"]),
             "active": r["period"] == active}
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
        r = c.execute("SELECT filename, data, compressed FROM period_files WHERE period=? AND rtype=?",
                      (period, rtype)).fetchone()
    if not r:
        return (None, None)
    data = bytes(r["data"])
    if r["compressed"]:
        data = zlib.decompress(data)
    return (r["filename"], data)


def switch_period(period):
    """Переключает рабочие данные на сохранённый период: чистит таблицы и
    заново разбирает сырые файлы этого периода. Справочники (почты) не трогает."""
    import parser, tempfile, os as _os
    init()
    with _conn() as c:
        files = [(r["rtype"], r["filename"],
                  zlib.decompress(bytes(r["data"])) if r["compressed"] else bytes(r["data"]))
                 for r in c.execute("SELECT rtype, filename, data, compressed "
                                    "FROM period_files WHERE period=?", (period,))]
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
    "docerr": "docerr", "status": "status", "koiki": "koiki", "max": "max_tmk",
    "xray": "xray",
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


# --- MAX: записи и оказание услуг ТМК через чат-бот MAX (телемедицина/цифровизация) ---

def _max_pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


def _max_rows():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM max_tmk")]


def _max_agg(rows):
    """Свёртка списка строк в суммы + пересчитанные доли через MAX (проценты нельзя усреднять)."""
    g = {k: sum(r[k] or 0 for r in rows)
         for k in ("zap", "zap_max", "otm", "otm_max", "prov", "prov_max", "bl_max")}
    g["zap_pct"] = _max_pct(g["zap_max"], g["zap"])
    g["otm_pct"] = _max_pct(g["otm_max"], g["otm"])
    g["prov_pct"] = _max_pct(g["prov_max"], g["prov"])
    return g


def max_totals():
    """Итоги по учреждению: записи/отменённые/проведённые ТМК — всего и через чат-бот MAX,
    доли через MAX, больничные, закрытые через MAX. None — если отчёт не загружен."""
    rows = _max_rows()
    if not rows:
        return None
    g = _max_agg(rows)
    g["n_doctors"] = len(set(r["doctor"] for r in rows))
    g["n_rows"] = len(rows)
    return g


def _max_group(key):
    """Агрегация по ключу (doctor/position/purpose) со свёрткой и пересчётом долей;
    сортировка по числу записей на ТМК (убыв.)."""
    rows = _max_rows()
    groups = {}
    for r in rows:
        groups.setdefault((r[key] or "—"), []).append(r)
    out = []
    for name, rs in groups.items():
        g = _max_agg(rs)
        g[key] = name
        if key == "doctor":
            g["position"] = rs[0]["position"]  # у врача одна должность
        out.append(g)
    out.sort(key=lambda x: -x["zap"])
    return out


def max_by_doctor():
    return _max_group("doctor")


def max_by_position():
    return _max_group("position")


def max_by_purpose():
    return _max_group("purpose")


# --- Рентген: обработка лучевых исследований сервисом ИИ ---

def xray_list():
    """Модальности с рассчитанными долями (успешно / с ошибкой / по сторонам ошибок).
    Сортировка по числу исследований (убыв.)."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM xray")]
    for r in rows:
        t = r["total"]
        r["pct_success"] = round(100 * r["success"] / t, 1) if t else None
        r["pct_err"] = round(100 * r["err"] / t, 1) if t else None
        r["pct_mi"] = round(100 * r["err_mi"] / t, 1) if t else None
        r["pct_mo"] = round(100 * r["err_mo"] / t, 1) if t else None
        r["pct_conn"] = round(100 * r["err_conn"] / t, 1) if t else None
    rows.sort(key=lambda x: -x["total"])
    return rows


def xray_totals():
    """Итоги по всем модальностям: всего/успешно/с ошибкой, доли, разбивка ошибок
    (МИ/МО/соединение) и средневзвешенное время обработки. None — если не загружено."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM xray")]
    if not rows:
        return None
    def s(k):
        return sum(r[k] or 0 for r in rows)
    total, success, err = s("total"), s("success"), s("err")
    # среднее время — взвешиваем по числу исследований модальности
    tw = sum((r["avg_time"] or 0) * (r["total"] or 0) for r in rows)
    return {
        "n_mod": len(rows), "total": total, "success": success, "err": err,
        "err_mi": s("err_mi"), "err_mo": s("err_mo"), "err_conn": s("err_conn"),
        "pct_success": round(100 * success / total, 1) if total else None,
        "pct_err": round(100 * err / total, 1) if total else None,
        "avg_time": round(tw / total, 1) if total else None,
    }


# --- Коечный фонд (занятость коек в стационарах) ---

def _koiki_days():
    import parser
    return parser.period_days(report_period("koiki") or "")


def koiki_list():
    """Строки вкладки «Занятость»: одиночные отделения + ГРУППЫ одной объединённой
    строкой (совмещение коек и движения — суммы по участникам). Показатели:
      zan     — занятость, % = койко-дни / (коек × дни периода) × 100;
      oborot  — оборот койки = выписано / коек;
      dlit    — средняя длительность = койко-дни / выписано;
      overload— койко-дни или пациенты превышают коечный фонд;
      no_beds — коек 0, а движение есть.
    Для групп план/выполнение — общие (годовой план группы). Сортировка по занятости."""
    days = _koiki_days()
    with _conn() as c:
        raw = [dict(r) for r in c.execute("SELECT * FROM koiki")]
        rmap = {r["otdelenie"]: dict(r) for r in c.execute("SELECT * FROM koiki_map")}
        pmap = {r["otdelenie"]: r["plan"] for r in c.execute("SELECT * FROM koiki_plan")}
        gm = {r["otdelenie"]: r["grp"] for r in c.execute("SELECT otdelenie, grp FROM koiki_group_member")}
        gplan = {r["grp"]: (r["plan"] or 0) for r in c.execute("SELECT grp, plan FROM koiki_group")}
    by_od = {r["otdelenie"]: r for r in raw}
    SUMK = ("koek", "kd", "nach", "postup", "vyp", "pered", "umer", "kon")
    # группы — одна объединённая строка (суммируем коечный фонд и движение по участникам)
    groups = {}
    for od, grp in gm.items():
        if od in by_od:
            groups.setdefault(grp, []).append(od)
    units = []
    for grp, members in groups.items():
        agg = {k: sum(by_od[m][k] or 0 for m in members) for k in SUMK}
        agg["otdelenie"] = grp
        agg["day"] = 1 if all(by_od[m]["day"] for m in members) else 0
        agg["is_group"] = True
        agg["members"] = sorted(members)
        agg["plan_year"] = gplan.get(grp, 0)
        units.append(agg)
    for r in raw:                      # одиночные отделения (не входящие в группы)
        if r["otdelenie"] in gm:
            continue
        r["is_group"] = False
        r["members"] = []
        r["plan_year"] = pmap.get(r["otdelenie"], 0) or 0
        units.append(r)
    for r in units:
        koek, kd, vyp = r["koek"], r["kd"], r["vyp"]
        r["zan"] = round(kd / (koek * days) * 100, 1) if koek else None
        r["oborot"] = round(vyp / koek, 1) if koek else None
        r["dlit"] = round(kd / vyp, 1) if vyp else None
        r["overload"] = bool(koek and (kd > koek * days or r["kon"] > koek))
        r["no_beds"] = koek == 0
        m = rmap.get(r["otdelenie"], {})
        r["resp"], r["email"] = m.get("resp", ""), m.get("email", "")
        py = r["plan_year"]
        r["plan"] = round(py / 365 * days) if py else 0        # план за период (для группы — общий)
        r["vypoln"] = round(r["postup"] / r["plan"] * 100, 1) if r["plan"] else None
    units.sort(key=lambda x: (x["zan"] is None, -(x["zan"] or 0)))
    return units


def koiki_totals():
    """Итоги по учреждению: всего / круглосуточные / дневные (места считаем отдельно),
    плюс счётчики отделений с перевыполнением (>100%) и недозагрузкой (<80%)."""
    days = _koiki_days()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT otdelenie, koek, kd, day, postup, vyp, pered, umer FROM koiki")]

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
    # выполнение плана за текущий период по план-единицам (группы + одиночные отделения с планом):
    # годовой план пропорционально дням; факт — сумма поступивших по участникам единицы
    postup_by_od = {r["otdelenie"]: (r["postup"] or 0) for r in rows}
    plan_period = plan_fact = 0
    for u in _plan_units():
        if not u["plan_year"]:
            continue
        plan_period += round(u["plan_year"] / 365 * days)
        plan_fact += sum(postup_by_od.get(m, 0) for m in u["members"])
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


def set_report_comment(rtype, comment):
    with _conn() as c:
        c.execute("INSERT INTO report_cfg(rtype,required,comment) VALUES(?,NULL,?) "
                  "ON CONFLICT(rtype) DO UPDATE SET comment=excluded.comment",
                  (rtype, (comment or "").strip()))


# --- Теги отчётов: свободная пользовательская классификация (страница «Загрузка») ---

def report_tags_all():
    """{rtype: [теги]} — по алфавиту, без учёта регистра."""
    init()
    out = {}
    with _conn() as c:
        for r in c.execute("SELECT rtype, tag FROM report_tags ORDER BY tag COLLATE NOCASE"):
            out.setdefault(r["rtype"], []).append(r["tag"])
    return out


def report_tag_add(rtype, tag):
    tag = (tag or "").strip()[:40]
    if not (rtype and tag):
        return
    init()
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO report_tags(rtype,tag) VALUES(?,?)", (rtype, tag))


def report_tag_remove(rtype, tag):
    with _conn() as c:
        c.execute("DELETE FROM report_tags WHERE rtype=? AND tag=?", (rtype, tag))


# --- План госпитализаций по отделениям (стационары) ---

def _plan_int(plan):
    try:
        return int(float(str(plan).replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def set_koiki_plan(otdelenie, plan):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO koiki_plan(otdelenie,plan) VALUES(?,?)", (otdelenie, _plan_int(plan)))


def set_koiki_group_plan(grp, plan):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO koiki_group(grp,plan) VALUES(?,?)", (grp, _plan_int(plan)))


def koiki_groups():
    """{имя_группы: [отделения-участники]} — объединения отделений с общим планом."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT otdelenie, grp FROM koiki_group_member")]
    g = {}
    for r in rows:
        g.setdefault(r["grp"], []).append(r["otdelenie"])
    return {k: sorted(v) for k, v in g.items()}


def koiki_group_create(name, otdeleniya):
    """Объединяет отделения (≥2) в группу с общим планом. Индивидуальные планы участников
    сохраняются (пока в группе — не учитываются; при разъединении вернутся). План группы по
    умолчанию — сумма индивидуальных планов участников (если у группы плана ещё нет)."""
    name = (name or "").strip()
    members = [o.strip() for o in (otdeleniya or []) if (o or "").strip()]
    if not name or len(members) < 2:
        return False
    with _conn() as c:
        for od in members:
            c.execute("INSERT OR REPLACE INTO koiki_group_member(otdelenie,grp) VALUES(?,?)", (od, name))
        if c.execute("SELECT 1 FROM koiki_group WHERE grp=?", (name,)).fetchone() is None:
            total = 0
            for od in members:
                r = c.execute("SELECT plan FROM koiki_plan WHERE otdelenie=?", (od,)).fetchone()
                total += (r["plan"] if r else 0) or 0
            c.execute("INSERT INTO koiki_group(grp,plan) VALUES(?,?)", (name, total))
    return True


def koiki_group_disband(name):
    """Разъединяет группу: убирает участников и план группы. Отделения снова считаются
    по отдельности (их сохранённые индивидуальные планы возвращаются)."""
    with _conn() as c:
        c.execute("DELETE FROM koiki_group_member WHERE grp=?", (name,))
        c.execute("DELETE FROM koiki_group WHERE grp=?", (name,))


def _plan_units(extra_ods=None):
    """План-единицы: группы (общий план) + одиночные отделения (не входящие в группы).
    Каждая: {name, is_group, members(list), plan_year}."""
    with _conn() as c:
        ods = {r["otdelenie"] for r in c.execute("SELECT otdelenie FROM koiki")}
        pmap = {r["otdelenie"]: (r["plan"] or 0) for r in c.execute("SELECT * FROM koiki_plan")}
        gm = [dict(r) for r in c.execute("SELECT otdelenie, grp FROM koiki_group_member")]
        gplan = {r["grp"]: (r["plan"] or 0) for r in c.execute("SELECT grp, plan FROM koiki_group")}
    groups = {}
    for r in gm:
        groups.setdefault(r["grp"], []).append(r["otdelenie"])
    grouped = {r["otdelenie"] for r in gm}
    ods |= set(extra_ods or []) | set(pmap)
    ods.discard("")
    units = [{"name": grp, "is_group": True, "members": sorted(mem), "plan_year": gplan.get(grp, 0)}
             for grp, mem in sorted(groups.items())]
    # одиночные — всё, кроме участников групп И самих имён групп (имя группы может прийти
    # через extra_ods из сводного cum и иначе задвоилось бы как «отделение»)
    units += [{"name": od, "is_group": False, "members": [od], "plan_year": pmap.get(od, 0)}
              for od in sorted(o for o in ods if o not in grouped and o not in groups)]
    return units


def koiki_plan_list(extra_ods=None):
    """План-единицы с годовым планом и производными (месяц = год/12, неделя = год/52).
    Объединение: текущая загрузка коек + все загруженные периоды (extra_ods) + справочник
    планов; отделения, сведённые в группу, показываются как одна строка-группа."""
    out = []
    for u in _plan_units(extra_ods):
        py = u["plan_year"]
        out.append({"name": u["name"], "is_group": u["is_group"], "members": u["members"],
                    "year": py,
                    "month": round(py / 12) if py else 0,
                    "week": round(py / 52) if py else 0})
    return out


def koiki_cumulative():
    """Сводное выполнение плана за ВСЕ загруженные периоды коек.
    Пересекающиеся периоды де-дублируются (жадно: приоритет раньше начавшимся и более длинным),
    пробелы между периодами показываются явно. План годовой, пропорционально числу покрытых дней."""
    import parser, tempfile, os as _os
    with _conn() as c:
        blobs = [(r["period"], bytes(r["data"])) for r in
                 c.execute("SELECT period, data FROM period_files WHERE rtype='koiki'")]
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
    # сворачиваем факт по план-единицам (группы суммируют поступивших по всем участникам)
    rows, tf, tp = [], 0, 0
    for u in _plan_units(extra_ods=list(cum)):
        fact = sum(cum.get(m, 0) for m in u["members"])
        py = u["plan_year"]
        if not (fact or py):
            continue
        plan_cov = round(py / 365 * covered) if (py and covered) else 0
        vyp = round(fact / plan_cov * 100, 1) if plan_cov else None
        rows.append({"otdelenie": u["name"], "is_group": u["is_group"], "members": u["members"],
                     "fact": fact, "plan_year": py, "plan_cov": plan_cov, "vypoln": vyp})
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


# --- Пользователи FreeIPA (полный профиль, для страницы «Пользователи») ---

def set_ipa_users(users):
    """Заменяет таблицу пользователей IPA свежей выгрузкой. users — список dict
    с ключами uid, cn, givenname, sn, mail, title, ou, phone, mobile, empnum, blocked."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM ipa_users")
        c.executemany(
            "INSERT OR REPLACE INTO ipa_users(uid,cn,givenname,sn,mail,title,ou,phone,mobile,empnum,blocked) "
            "VALUES(:uid,:cn,:givenname,:sn,:mail,:title,:ou,:phone,:mobile,:empnum,:blocked)",
            [{"uid": u.get("uid", ""), "cn": u.get("cn", ""), "givenname": u.get("givenname", ""),
              "sn": u.get("sn", ""), "mail": u.get("mail", ""), "title": u.get("title", ""),
              "ou": u.get("ou", ""), "phone": u.get("phone", ""), "mobile": u.get("mobile", ""),
              "empnum": u.get("empnum", ""), "blocked": 1 if u.get("blocked") else 0} for u in users])


def ipa_users_list():
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM ipa_users ORDER BY (mail=''), cn")]


def ipa_users_stats():
    init()
    with _conn() as c:
        rows = list(c.execute("SELECT blocked, mail FROM ipa_users"))
    return {"total": len(rows),
            "blocked": sum(1 for r in rows if r["blocked"]),
            "no_mail": sum(1 for r in rows if not (r["mail"] or "").strip())}


# --- Доступ в ЕИСЗ ПК (выгрузка BIRT) и сверка с актуальным штатом (FreeIPA) ---

def set_eisz_users(records):
    """Заменяет выгрузку ЕИСЗ свежей. records — из parser.parse_birt_eisz."""
    init()
    with _conn() as c:
        c.execute("DELETE FROM eisz_users")
        c.executemany(
            "INSERT INTO eisz_users(fio,snils,frmo,podr,position,stavka,start,endwork) "
            "VALUES(:fio,:snils,:frmo,:podr,:position,:stavka,:start,:end)", records)


def eisz_list():
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM eisz_users ORDER BY fio")]


def _norm_fio(s):
    return " ".join((s or "").upper().split())


def eisz_reconcile():
    """Сверяет доступ в ЕИСЗ с актуальным штатом (FreeIPA), сопоставляя по ФИО.
    Возвращает группы: есть доступ, нет доступа (штат без ЕИСЗ), на удаление
    (ЕИСЗ без действующего сотрудника в штате). У человека может быть несколько мест —
    схлопываем по СНИЛС/ФИО; уволенным считаем того, у кого все места с датой окончания."""
    init()
    with _conn() as c:
        eisz = [dict(r) for r in c.execute("SELECT * FROM eisz_users")]
        staff = [dict(r) for r in c.execute(
            "SELECT cn, uid, mail, ou, title, blocked FROM ipa_users")]
    staff_names = {_norm_fio(s["cn"]) for s in staff}
    # схлопываем ЕИСЗ по человеку
    persons = {}
    for e in eisz:
        key = e["snils"] or _norm_fio(e["fio"])
        p = persons.setdefault(key, {"fio": e["fio"], "snils": e["snils"], "podrs": set(),
                                     "positions": set(), "active": False, "ends": set()})
        if e["position"]:
            p["positions"].add(e["position"])
        if e["podr"]:
            p["podrs"].add(e["podr"])
        if (e["endwork"] or "").strip():
            p["ends"].add(e["endwork"])
        else:
            p["active"] = True
    for p in persons.values():
        p["in_staff"] = _norm_fio(p["fio"]) in staff_names
        p["terminated"] = not p["active"]  # у всех мест проставлена дата окончания
        p["positions"] = " · ".join(sorted(p["positions"]))
        p["podrs"] = " · ".join(sorted(p["podrs"]))
        p["end"] = " · ".join(sorted(p["ends"]))
    plist = list(persons.values())
    eisz_names = {_norm_fio(p["fio"]) for p in plist}
    has_access = sorted([p for p in plist if p["in_staff"]], key=lambda x: x["fio"])
    # на удаление: нет в актуальном штате ИЛИ уволен (все места закрыты)
    to_delete = sorted([p for p in plist if not p["in_staff"] or p["terminated"]],
                       key=lambda x: (x["in_staff"], x["fio"]))
    no_access = sorted([s for s in staff if not s["blocked"]
                        and _norm_fio(s["cn"]) not in eisz_names], key=lambda x: x["cn"])
    return {
        "persons": len(plist), "records": len(eisz), "staff": len(staff),
        "staff_loaded": len(staff) > 0,
        "has_access": has_access, "no_access": no_access, "to_delete": to_delete,
        "n_has": len(has_access), "n_no": len(no_access), "n_del": len(to_delete),
    }


def bulk_set_doctor_emails(items):
    """items: список (vrach, email). Сохраняет почту под тем же ключом, по которому её
    ищет doctors(): СНИЛС (без пробелов) при наличии, иначе нормализованное ФИО."""
    with _conn() as c:
        smap = {r["vrach"]: (r["s"] or "").replace(" ", "")
                for r in c.execute("SELECT vrach, MAX(snils) s FROM vrachi GROUP BY vrach")}
        pairs = [(smap.get(v) or _norm(v), (e or "").strip()) for v, e in items]
        c.executemany("INSERT OR REPLACE INTO email_map(key,email) VALUES(?,?)", pairs)
    return len(pairs)


def log_send(vrach, email, cnt, status, kind="", subject="", period="", by_user=""):
    with _conn() as c:
        c.execute("INSERT INTO send_log(ts,vrach,email,cnt,status,kind,subject,period,by_user) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (datetime.datetime.now().isoformat(timespec="seconds"), vrach, email, cnt, status,
                   kind, subject, period, by_user))


def send_log(limit=100):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM send_log ORDER BY ts DESC LIMIT ?", (limit,))]


def send_log_stats():
    """Карточки журнала: сегодня / ошибок сегодня / всего / последняя отправка."""
    init()
    today = datetime.date.today().isoformat()
    bad = "status LIKE 'ошибка%' OR status IN ('SMTP не настроен','нет адреса')"
    with _conn() as c:
        r = c.execute(f"SELECT COUNT(*) n, SUM(CASE WHEN {bad} THEN 1 ELSE 0 END) err "
                      "FROM send_log WHERE ts LIKE ?", (today + "%",)).fetchone()
        total = c.execute("SELECT COUNT(*) n FROM send_log").fetchone()["n"]
        last = c.execute("SELECT MAX(ts) t FROM send_log").fetchone()["t"]
    return {"today": r["n"] or 0, "today_err": r["err"] or 0, "total": total,
            "last": _ts_human(last) if last else ""}


# --- Журнал операций: кто и что сделал в менеджере ---

def log_op(user, action, details=""):
    init()
    with _conn() as c:
        c.execute("INSERT INTO ops_log VALUES(?,?,?,?)",
                  (datetime.datetime.now().isoformat(timespec="seconds"),
                   user or "—", action, (details or "")[:500]))


def ops_log_list(limit=300):
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM ops_log ORDER BY ts DESC LIMIT ?", (limit,))]


def _norm(fio):
    return " ".join((fio or "").upper().split())
