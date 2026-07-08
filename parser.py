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
"""
import re
import datetime
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


def _num(x):
    try:
        return float(str(x).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def detect_type(rows):
    """Определяет тип отчёта по тексту первых строк."""
    # «Коечный фонд» — заголовок в объединённой ячейке (не в колонке 1), ищем по всем ячейкам шапки
    allhead = " ".join(v for r in rows[:8] for v in r.values() if v).lower()
    if "коечного фонда" in allhead or "движения пациентов и коечного" in allhead:
        return "koiki"
    head = " ".join(
        (r.get(1, "") or "") for r in rows[:6]
    ).lower()
    if "фельдшерам фап" in head or "работе в эмк" in head:
        return "fap"
    if "в разрезе врачей" in head:
        return "vrachi"
    if "неподписанными документами" in head or "неподписанных документ" in head:
        return "debts"
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
    return ""


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
    """Главная функция. Возвращает dict с типом, периодом и записями."""
    rows = _rows(path)
    rtype = detect_type(rows)
    res = {"type": rtype, "period": _period(rows), "rows": len(rows), "records": []}

    if rtype == "koiki":
        res["records"] = _parse_koiki(_rows_merged(path))
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
      c9 поступило (гр.7), c18 выписано (гр.14), c23 умерло (гр.18),
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
            "vyp": int(_num(r.get(18, "")) or 0),
            "umer": int(_num(r.get(23, "")) or 0),
            "kon": int(_num(r.get(27, "")) or 0),
            "day": 1 if ("дневн" in low or "пациенто" in low) else 0,
        })
    return out
