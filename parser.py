# -*- coding: utf-8 -*-
"""
Парсер отчётов РЭМД (ЕИСЗ ПК) в формате SpreadsheetML (.xls, который на самом деле XML).
Определяет тип отчёта по заголовку и извлекает структурированные записи.

Поддерживаемые типы:
  - vrachi   : «Отчет по отправке документов в РЭМД в разрезе врачей»
  - debts    : «Список пациентов с неподписанными документами ...»
  - flk      : «РЭМД. Детализация по ошибкам ФЛК»
  - status   : «Статистика по статусам документов в РЭМД» (воронка дашборда)
  - koiki    : «Сводная ведомость движения пациентов и коечного фонда»
  - max      : «Отчёт о количестве записей и оказания услуг ТМК через чат-бот MAX»
  - xray     : «Отчёт по обработке лучевых исследований сервисом ИИ» (.xlsx, без периода)
"""
import re
import datetime
import zipfile
import xml.etree.ElementTree as ET

SS = "urn:schemas-microsoft-com:office:spreadsheet"
CELL = f"{{{SS}}}Cell"
DATA = f"{{{SS}}}Data"
ROW = f"{{{SS}}}Row"
IDX = f"{{{SS}}}Index"
MERGE = f"{{{SS}}}MergeAcross"


def _rows(path):
    """Потоково читает строки (с учётом ss:Index). Возвращает список dict{col->text}."""
    out = []
    for ev, el in ET.iterparse(path, events=("end",)):
        if el.tag == ROW:
            cells, col = {}, 0
            for c in el.findall(CELL):
                i = c.get(IDX)
                col = int(i) if i else col + 1
                d = c.find(DATA)
                cells[col] = (d.text or "").strip() if d is not None else ""
            out.append(cells)
            el.clear()
    return out


def _rows_merged(path):
    """Как _rows, но учитывает объединённые ячейки (ss:MergeAcross): значение кладётся на
    СТАРТОВУЮ колонку диапазона, а следующая ячейка сдвигается на ширину объединения.
    Нужно для отчётов с многоуровневой шапкой и объединёнными ячейками в данных
    (напр., «Сводная ведомость движения пациентов и коечного фонда»)."""
    out = []
    for ev, el in ET.iterparse(path, events=("end",)):
        if el.tag == ROW:
            cells, col = {}, 1
            for c in el.findall(CELL):
                i = c.get(IDX)
                if i:
                    col = int(i)
                d = c.find(DATA)
                cells[col] = (d.text or "").strip() if d is not None else ""
                span = c.get(MERGE)
                col += (int(span) if span else 0) + 1
            out.append(cells)
            el.clear()
    return out


def _is_xlsx(path):
    """.xlsx — это ZIP (сигнатура PK). SpreadsheetML .xls — это XML (начинается с '<')."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _col_num(ref):
    """Ссылка ячейки → номер колонки (1-based): A1→1, B3→2, AA1→27."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n or 1


def _rows_xlsx(path):
    """Читает .xlsx (ZIP с XML) минимально, без внешних зависимостей (openpyxl не нужен).
    Возвращает список dict{col(1-based)->text}, как _rows. Для плоских отчётов без
    объединённых ячеек (напр., «обработка лучевых исследований сервисом ИИ»)."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            st = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in st.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheets = sorted(n for n in names
                        if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        if not sheets:
            return []
        sheet = ET.fromstring(z.read(sheets[0]))
        out = []
        for row in sheet.iter(f"{ns}row"):
            cells = {}
            for c in row.findall(f"{ns}c"):
                ref = c.get("r") or ""
                col = _col_num(ref) if ref else (max(cells) + 1 if cells else 1)
                t = c.get("t")
                v = c.find(f"{ns}v")
                if t == "s":
                    txt = shared[int(v.text)] if (v is not None and v.text) else ""
                elif t == "inlineStr":
                    is_ = c.find(f"{ns}is")
                    txt = "".join(x.text or "" for x in is_.iter(f"{ns}t")) if is_ is not None else ""
                else:
                    txt = v.text if v is not None else ""
                cells[col] = (txt or "").strip()
            out.append(cells)
        return out


def _num(x, cast=float):
    try:
        return cast(str(x).replace(" ", "").replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def detect_type(rows):
    """Определяет тип отчёта по тексту первых строк."""
    # «Коечный фонд» — заголовок в объединённой ячейке (не в колонке 1), ищем по всем ячейкам шапки
    allhead = " ".join(v for r in rows[:8] for v in r.values() if v).lower()
    if "коечного фонда" in allhead or "движения пациентов и коечного" in allhead:
        return "koiki"
    # Обработка лучевых исследований ИИ — шапка «Модальность | … исследований …»
    if "модальность" in allhead and "исследован" in allhead:
        return "xray"
    head = " ".join(
        (r.get(1, "") or "") for r in rows[:6]
    ).lower()
    if "чат-бот" in head and "тмк" in head:
        return "max"
    if "фельдшерам фап" in head or "работе в эмк" in head:
        return "fap"
    if "в разрезе врачей" in head:
        return "vrachi"
    if "неподписанными документами" in head or "неподписанных документ" in head:
        return "debts"
    if "в разрезе видов документов и работников" in head:
        return "vid_worker"   # агрегат вид×врач: считается из витрины, загрузка не нужна
    if "детализация статистики отправки" in head:
        return "detail"
    if "в разрезе видов документов" in head:
        return "vidy"
    if "по статусам документ" in head:
        return "status"
    if "ошибк" in head and "флк" in head:
        return "flk"
    if "по ошибкам документ" in head:
        return "docerr"
    if "не передан" in head and "рэмд" in head:
        return "notrans"
    if "состояние по эмд" in head:
        return "state"
    return "unknown"


def _period(rows):
    for r in rows[:14]:
        joined = " ".join(v for v in r.values() if v).strip()
        low = joined.lower()
        if "период" in low or ("с " in low and "по" in low and "202" in low):
            return joined
    return _period_from_dates(rows)


def _period_from_dates(rows):
    """Период из строк «Дата начала: …» / «Дата окончания: …» (выгрузки РЭМД)."""
    d1 = d2 = ""
    for r in rows[:16]:
        joined = " ".join(v for v in r.values() if v)
        low = joined.lower()
        m = re.findall(r"\d{2}\.\d{2}\.\d{4}", joined)
        if "дата начала" in low and m:
            d1 = m[0]
            if "дата оконча" in low and len(m) > 1:
                d2 = m[1]
        elif "дата оконча" in low and m:
            d2 = m[0]
    if d1 and d2:
        return f"с {d1} по {d2}"
    return ""


def _iso(d):
    """«ДД.ММ.ГГГГ» -> «ГГГГ-ММ-ДД» (для сортируемого хранения в витрине)."""
    d = (d or "").strip()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", d)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _parse_state(rows):
    """«РЭМД. Состояние по ЭМД» — подокументный реестр (по дате создания документа).
    Читаем полностью, включая пациента (ФИО/дата рождения/СНИЛС/прикрепление/участок):
    в менеджере — полная информация; ПДн отрезаются на границе почтовых рассылок."""
    out = []
    started = False
    for r in rows:
        if not started:
            if " ".join((r.get(1, "") or "").split()) == "№ п/п":
                started = True
            continue
        no = (r.get(1, "") or "").strip()
        if not no.replace(".", "").isdigit():
            continue
        if (r.get(2, "") or "").strip().isdigit():
            continue          # строка с номерами колонок под шапкой — не данные
        regnum = (r.get(21, "") or "").strip()
        if not regnum:
            continue
        out.append({
            "regnum": regnum,
            "version": _num(r.get(24), int) or 1,
            "patient": r.get(8, ""), "birth": _iso(r.get(9)), "snils": r.get(10, ""),
            "attach_mo": r.get(11, ""), "uch_type": r.get(13, ""), "uch_num": r.get(14, ""),
            "vid": r.get(2, ""), "status": r.get(7, ""),
            "vrach": r.get(15, ""), "podr": r.get(18, ""), "oid": r.get(20, ""),
            "d_created": _iso(r.get(5)), "d_signed": _iso(r.get(25)),
            "d_registered": _iso(r.get(22)), "remd_num": r.get(23, ""),
            "err_code": " ".join((r.get(30, "") or "").split()), "err_text": r.get(31, ""),
            "attempts": _num(r.get(32), int) or 0, "fmt": r.get(29, ""),
            "days_to_sign": _num(r.get(26)), "days_to_reg": _num(r.get(28)),
        })
    return out


def _parse_detail(rows):
    """«Детализация статистики отправки ЭМД» — события отправки/регистрации за период
    (включая документы прошлых недель — этим выгрузка сама актуализирует витрину)."""
    out = []
    started = False
    for r in rows:
        if not started:
            if " ".join((r.get(1, "") or "").split()) == "№ п/п":
                started = True
            continue
        no = (r.get(1, "") or "").strip()
        if not no.replace(".", "").isdigit():
            continue
        if (r.get(2, "") or "").strip().isdigit():
            continue          # строка с номерами колонок под шапкой — не данные
        uid = (r.get(11, "") or "").strip()
        regnum = (r.get(8, "") or "").strip() or (f"uid:{uid}" if uid else "")
        if not regnum:
            continue
        out.append({
            "uid": uid, "regnum": regnum,
            "vid": r.get(2, ""), "status": r.get(3, ""),
            "d_registered": _iso(r.get(4)), "oid": r.get(6, ""), "podr": r.get(7, ""),
            "vrach": r.get(10, ""), "remd_num": r.get(12, ""),
            "err_code": " ".join((r.get(13, "") or "").split()),
            "err_type": " ".join((r.get(14, "") or "").split()), "err_text": r.get(15, ""),
            "d_doc": _iso(r.get(9)),
        })
    return out


def norm_period(text):
    """Нормализует строку периода к виду «ДД.ММ.ГГГГ — ДД.ММ.ГГГГ» для сравнения.
    Возвращает '' если дат нет."""
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*(?:по|[-–—])\s*(\d{2}\.\d{2}\.\d{4})", text or "")
    if m:
        return f"{m.group(1)} — {m.group(2)}"
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text or "")
    return m.group(1) if m else ""


def _date(s):
    try:
        d, m, y = s.split(".")
        return datetime.date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def max_period(period_texts):
    """Период выгрузки — максимальный охват отчётов: самое раннее начало и самый
    поздний конец среди нормализованных периодов. «ДД.ММ.ГГГГ — ДД.ММ.ГГГГ» или ''."""
    starts, ends = [], []
    for t in period_texts:
        np = norm_period(t)
        if not np:
            continue
        parts = np.split(" — ")
        s, e = _date(parts[0]), _date(parts[-1])
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if not starts:
        return ""
    lo, hi = min(starts), max(ends or starts)
    return lo.strftime("%d.%m.%Y") if lo == hi else f"{lo.strftime('%d.%m.%Y')} — {hi.strftime('%d.%m.%Y')}"


def period_days(text):
    """Число календарных дней в периоде (включительно) — знаменатель для занятости коек.
    По умолчанию 7 (неделя), если период не распознан или задан одной датой."""
    np = norm_period(text or "")
    if not np:
        return 7
    parts = np.split(" — ")
    s = _date(parts[0])
    e = _date(parts[-1]) if len(parts) > 1 else None
    if s and e:
        return (e - s).days + 1
    return 7


def parse(path):
    """Главная функция. Возвращает dict с типом, периодом и записями.
    Поддерживает и SpreadsheetML .xls (XML), и настоящий .xlsx (ZIP)."""
    rows = _rows_xlsx(path) if _is_xlsx(path) else _rows(path)
    rtype = detect_type(rows)
    res = {"type": rtype, "period": _period(rows), "rows": len(rows), "records": []}

    if rtype == "koiki":
        res["records"] = _parse_koiki(_rows_merged(path))
    elif rtype == "state":
        res["records"] = _parse_state(rows)
        res["period"] = _period_from_dates(rows) or res["period"]
    elif rtype == "detail":
        res["records"] = _parse_detail(rows)
        res["period"] = _period_from_dates(rows) or res["period"]
    elif rtype == "vrachi":
        res["records"] = _parse_vrachi(rows)
    elif rtype == "debts":
        res["records"] = _parse_debts(rows)
    elif rtype == "flk":
        res["records"] = _parse_flk(rows)
    elif rtype == "notrans":
        res["records"] = _parse_notrans(rows)
    elif rtype == "fap":
        res["records"] = _parse_fap(rows)
    elif rtype == "vidy":
        res["records"] = _parse_vidy(rows)
    elif rtype == "docerr":
        res["records"] = _parse_docerr(rows)
    elif rtype == "status":
        res["records"] = _parse_status(rows)
    elif rtype == "max":
        res["records"] = _parse_max(rows)
    elif rtype == "xray":
        res["records"] = _parse_xray(rows)
    return res


def _parse_fap(rows):
    """ФАП — работа фельдшеров в ЭМК. c2 ФАП, c6 интернет, c8 ФИО, c9 посещений,
    c10 с документами, c11 % заполнения, c12 направлений, c13 рецептов,
    c14 назначений, c15 ЭЛН."""
    out = []
    for r in rows:
        fio = (r.get(8, "") or "").strip()
        if not fio or fio.isdigit() or fio.lower().startswith("фио"):
            continue
        vis = _num(r.get(9, ""))
        if vis is None:
            continue
        out.append({
            "fap": (r.get(2, "") or "").strip().split("\n")[0],
            "internet": (r.get(6, "") or "").strip(),
            "fio": fio,
            "visits": int(vis),
            "visits_doc": int(_num(r.get(10, "")) or 0),
            "pct": int(_num(r.get(11, "")) or 0),
            "naprav": int(_num(r.get(12, "")) or 0),
            "recipes": int(_num(r.get(13, "")) or 0),
            "naznach": int(_num(r.get(14, "")) or 0),
            "eln": int(_num(r.get(15, "")) or 0),
            "telemed": int(_num(r.get(16, "")) or 0),
            "er": int(_num(r.get(17, "")) or 0),
        })
    return out


def _parse_vidy(rows):
    """Статистика по видам документов: c2 вид, c4 зарегистрировано, c5 отправлено,
    c6 ошибка синхронной отправки, c7 ошибка регистрации, c8 общий итог."""
    out = []
    for r in rows:
        vid = (r.get(2, "") or "").strip()
        total = _num(r.get(8, ""))
        if not vid or vid.isdigit() or total is None:
            continue
        if vid.lower().startswith(("вид документ", "итог", "общий")):
            continue
        out.append({
            "doc_type": vid,
            "zareg": int(_num(r.get(4, "")) or 0),
            "sent": int(_num(r.get(5, "")) or 0),
            "err_sync": int(_num(r.get(6, "")) or 0),
            "err_reg": int(_num(r.get(7, "")) or 0),
            "total": int(total),
        })
    return out


def _parse_docerr(rows):
    """Статистика по ошибкам документов: c3 вид, c4 не найдена запись справочника,
    c5 ошибка валидации значения, c6 переданная должность."""
    out = []
    for r in rows:
        vid = (r.get(3, "") or "").strip()
        if not vid or vid.isdigit() or vid.lower().startswith(("вид документ", "итог")):
            continue
        nf = int(_num(r.get(4, "")) or 0)
        val = int(_num(r.get(5, "")) or 0)
        pos = int(_num(r.get(6, "")) or 0)
        if nf + val + pos == 0:
            continue
        out.append({"doc_type": vid, "not_found": nf, "validation": val,
                    "position": pos, "total": nf + val + pos})
    return out


def _parse_status(rows):
    """Статистика по статусам документов: c3 статус, c4 количество."""
    out = []
    for r in rows:
        st = (r.get(3, "") or "").strip()
        cnt = _num(r.get(4, ""))
        if not st or st.isdigit() or cnt is None:
            continue
        if st.lower().startswith(("итог", "статус документ")):
            continue
        out.append({"status": st, "count": int(cnt)})
    return out


def _parse_vrachi(rows):
    """Колонки: c1 Подразделение, c2 Врач, c4 СНИЛС, c5 Вид документа,
    c6 сформировано, c7 подписано, c8 не подписано, c10 зарегистрировано."""
    out = []
    cur_pod = cur_vrach = cur_snils = None
    for r in rows[10:]:
        c1, c2, c4, c5 = r.get(1, ""), r.get(2, ""), r.get(4, ""), r.get(5, "")
        # строка нумерации колонок «1,2,3,4,…» — это не данные
        if c1.strip() == "1" and c2.strip() == "2" and c5.strip() == "4":
            continue
        if c1:
            cur_pod = c1
        sform = _num(r.get(6, ""))
        if c5 and sform is not None:  # детальная строка по виду документа
            if c2:
                cur_vrach, cur_snils = c2, (c4 or cur_snils)
            out.append({
                "podrazdelenie": cur_pod or "",
                "vrach": cur_vrach or "",
                "snils": (cur_snils or "").replace(" ", ""),
                "doc_type": c5,
                "sform": int(sform or 0),
                "podp": int(_num(r.get(7, "")) or 0),
                "nepodp": int(_num(r.get(8, "")) or 0),
                "zareg": int(_num(r.get(10, "")) or 0),
            })
    return out


def _parse_debts(rows):
    """Один документ = одна строка с «видом неподписанного документа» (c7) и врачом (c8).
    Идентификация случая (c2 пациент, c3 ДР, c4 № случая, c5/c6 даты) объединена по группе
    строк одного случая и заполнена ТОЛЬКО в первой строке группы — переносим её
    на последующие строки-продолжения (у них c2–c6 пустые)."""
    out = []
    case = {"patient": "", "birth": "", "case_no": "", "d_start": "", "d_end": ""}
    for r in rows[10:]:
        patient = (r.get(2, "") or "").strip()
        doc_type = (r.get(7, "") or "").strip()
        vrach = (r.get(8, "") or "").strip()
        # пропуск строк заголовка и строки нумерации колонок
        if patient in ("ФИО пациента", "2"):
            continue
        if patient:  # начало нового случая — запоминаем идентификацию
            case = {
                "patient": patient,
                "birth": (r.get(3, "") or "").strip(),
                "case_no": (r.get(4, "") or "").strip(),
                "d_start": (r.get(5, "") or "").strip(),
                "d_end": (r.get(6, "") or "").strip(),
            }
        if not doc_type or doc_type in ("7", "Вид  неподписанного документа",
                                        "Вид неподписанного документа"):
            continue
        out.append({
            "vrach": vrach,
            "patient": case["patient"],
            "birth": case["birth"],
            "case_no": case["case_no"],
            "d_start": case["d_start"],
            "d_end": case["d_end"],
            "doc_type": doc_type,
            "otdelenie": (r.get(9, "") or "").strip(),  # c9 — отделение/кабинет врача
        })
    return [x for x in out if x["vrach"]]


def _parse_flk(rows):
    """Колонки: c1 Фамилия, c2 Имя, c4 Отчество, c5 ДР, c6 СНИЛС,
    c7 тип запроса, c8 код ошибки, c9 описание, c10 подразделение/доп."""
    out = []
    for r in rows[8:]:
        fam = r.get(1, "")
        code = r.get(8, "")
        if not fam and not code:
            continue
        if fam.lower() in ("фамилия сотрудника", "фамилия"):
            continue
        fio = " ".join(x for x in [r.get(1, ""), r.get(2, ""), r.get(4, "")] if x).strip()
        out.append({
            "fio": fio,
            "snils": (r.get(6, "") or "").replace(" ", ""),
            "req_type": r.get(7, ""),
            "code": code,
            "descr": r.get(9, ""),
            "extra": r.get(10, ""),
        })
    return [x for x in out if x["code"]]


def _parse_notrans(rows):
    """Документы, не переданные в РЭМД (агрегат по МО):
    c3 всего, c4 не сформированы, c11 сформированы, но не переданы."""
    for r in rows[8:]:
        name = (r.get(1, "") or "").upper()
        if "ОСИНСК" in name and "ИТОГО" not in name:
            t = _num(r.get(3, ""))
            if t is not None:
                return [{
                    "total": int(t),
                    "not_formed": int(_num(r.get(4, "")) or 0),
                    "formed_not_trans": int(_num(r.get(11, "")) or 0),
                }]
    return []


def _parse_koiki(rows):
    """«Сводная ведомость движения пациентов и коечного фонда» (стационары/дневные).
    rows — из _rows_merged (учтены объединённые ячейки шапки). Колонки по графам формы:
      c1 наименование, c4 число коек (гр.3), c8 состояло на начало (гр.6),
      c9 поступило (гр.7), c17 переведено в другие отделения (гр.13),
      c18 выписано (гр.14), c23 умерло (гр.18),
      c27 состояло на конец (гр.21), c28 проведено пациентами койко-дней (гр.22).
    Занятость и производные (оборот, длительность) считаются в storage — здесь только сырьё.
    Койко-дни берём как есть из отчёта (это фактическая сумма дней каждого пациента)."""
    out = []
    for r in rows:
        name = (r.get(1, "") or "").strip()
        if not name:
            continue
        low = name.lower()
        if name in ("1", "2") or low.startswith(("итого", "наименование")):
            continue
        koek = _num(r.get(4, ""))
        kd = _num(r.get(28, ""))
        if koek is None and kd is None:  # строка шапки/служебная — не данные
            continue
        out.append({
            "otdelenie": name.split("\n")[0].strip(),
            "koek": int(koek or 0),
            "kd": int(kd or 0),
            "nach": int(_num(r.get(8, "")) or 0),
            "postup": int(_num(r.get(9, "")) or 0),
            "pered": int(_num(r.get(17, "")) or 0),
            "vyp": int(_num(r.get(18, "")) or 0),
            "umer": int(_num(r.get(23, "")) or 0),
            "kon": int(_num(r.get(27, "")) or 0),
            "day": 1 if ("дневн" in low or "пациенто" in low) else 0,
        })
    return out


def _parse_max(rows):
    """«Отчёт о количестве записей и оказания услуг ТМК через чат-бот MAX».
    Сводная (pivot) раскладка: МО → Должность → Врач → строки по «Цели консультации»,
    с промежуточными строками «Итого по …» и заголовками групп. Берём только
    ЛИСТОВЫЕ строки (у них № п/п — число), агрегаты и заголовки пропускаем.
    Колонки (с учётом ss:Index):
      c1 № п/п, c3 должность, c4 врач, c5 цель консультации,
      c6 записей на ТМК всего,  c7 из них через чат-бот MAX,
      c9 отменённых записей всего, c10 из них через MAX,
      c12 проведённых ТМК всего,   c13 из них через MAX,
      c15 больничных листов, закрытых через MAX.
    Проценты не храним — пересчитываем из сумм при агрегации (нельзя усреднять %)."""
    out = []
    for r in rows:
        pp = (r.get(1, "") or "").strip()
        doctor = (r.get(4, "") or "").strip()
        # лист: № — целое, врач — ФИО (не число, не пусто); отсекает «Итого по», «ВСЕГО»,
        # заголовки групп (одна ячейка) и строку нумерации колонок (там c4 = «4»).
        if not pp.isdigit() or not doctor or doctor.isdigit():
            continue
        out.append({
            "doctor": doctor,
            "position": (r.get(3, "") or "").strip(),
            "purpose": (r.get(5, "") or "").strip(),
            "zap": int(_num(r.get(6, "")) or 0),
            "zap_max": int(_num(r.get(7, "")) or 0),
            "otm": int(_num(r.get(9, "")) or 0),
            "otm_max": int(_num(r.get(10, "")) or 0),
            "prov": int(_num(r.get(12, "")) or 0),
            "prov_max": int(_num(r.get(13, "")) or 0),
            "bl_max": int(_num(r.get(15, "")) or 0),
        })
    return out


def parse_birt_eisz(text):
    """Разбор HTML-выгрузки сотрудников ЕИСЗ ПК (BIRT Report Viewer).
    Колонки: № | ФИО | СНИЛС | синхр.ФРМО(сотр) | ID ФРМР(сотр) | структурный элемент |
    должность | ставка | начало работы | окончание работы | синхр.ФРМО(место) | ID ФРМР(место).
    Одна строка = одно рабочее место (у сотрудника может быть несколько). Пустое «окончание
    работы» — место действующее."""
    import html as _html
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        c = [_html.unescape(re.sub(r"<[^>]+>", "", x)).replace("\xa0", " ").strip() for x in cells]
        if len(c) < 10:
            continue
        pp, fio = c[0].strip(), c[1].strip()
        if not pp.isdigit() or not fio or fio.isdigit() or fio.lower().startswith("фио"):
            continue
        out.append({
            "fio": " ".join(fio.split()),
            "snils": re.sub(r"\D", "", c[2]),
            "frmo": c[3].strip(),
            "podr": c[5].strip(),
            "position": c[6].strip(),
            "stavka": c[7].strip(),
            "start": c[8].strip(),
            "end": c[9].strip(),
        })
    return out


def _parse_xray(rows):
    """«Отчёт по обработке лучевых исследований сервисом ИИ» (.xlsx).
    В отчёте НЕТ периода — период берётся из выгрузки (report_period → active_period).
    Одна строка = одна модальность (ФЛГ, ММГ, РГ, КТ, …). Колонки (1-based):
      c1 модальность, c2 всего исследований, c3 успешно обработано, c4 с ошибкой,
      c7 ошибок на стороне МИ (медизделие/ИИ-сервис), c9 на стороне МО,
      c11 ошибок соединения, c13 среднее время обработки, сек.
    Доли % (c5,c6,c8,c10,c12) не храним — пересчитываем из сумм."""
    out = []
    for r in rows:
        mod = (r.get(1, "") or "").strip()
        if not mod or mod.lower().startswith(("модальность", "итог", "всего")):
            continue
        total = _num(r.get(2, ""))
        if total is None:
            continue
        out.append({
            "modality": mod,
            "total": int(total or 0),
            "success": int(_num(r.get(3, "")) or 0),
            "err": int(_num(r.get(4, "")) or 0),
            "err_mi": int(_num(r.get(7, "")) or 0),
            "err_mo": int(_num(r.get(9, "")) or 0),
            "err_conn": int(_num(r.get(11, "")) or 0),
            "avg_time": round(_num(r.get(13, "")) or 0, 1),
        })
    return out
