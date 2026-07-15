# -*- coding: utf-8 -*-
"""Получение корпоративных почт сотрудников из FreeIPA (LDAP).
Сопоставление по ФИО (cn / displayName). Настраивается через переменные окружения.
"""
import datetime
import storage
import appconfig as cfg


def _cfg():
    return {
        "uri": cfg.get("IPA_LDAP_URI", ""),          # ldaps://ipa.example.local
        "base": cfg.get("IPA_BASE_DN", ""),          # cn=users,cn=accounts,dc=example,dc=local
        "bind_dn": cfg.get("IPA_BIND_DN", ""),       # uid=svc,cn=users,...
        "bind_pw": cfg.get("IPA_BIND_PW", ""),
    }


def available():
    c = _cfg()
    return bool(c["uri"] and c["base"])


# Атрибуты FreeIPA/LDAP, заполняемые кадровой синхронизацией (см. User Manager):
#   title→должность, ou→подразделение, telephoneNumber→рабочий, mobile→моб.,
#   employeeNumber→кадровый код, nsaccountlock→блокировка.
_ATTRS = ["uid", "cn", "displayName", "givenName", "sn", "mail", "title", "ou",
          "telephoneNumber", "mobile", "employeeNumber", "nsaccountlock"]


def fetch_users():
    """Полный профиль ВСЕХ пользователей FreeIPA (для страницы «Пользователи»).
    Возвращает список dict {uid, cn, givenname, sn, mail, title, ou, phone, mobile, empnum, blocked}."""
    from ldap3 import Server, Connection, ALL, SUBTREE
    c = _cfg()
    server = Server(c["uri"], get_info=ALL)
    conn = Connection(server, user=c["bind_dn"] or None, password=c["bind_pw"] or None,
                      auto_bind=True)
    conn.search(c["base"], "(uid=*)", search_scope=SUBTREE, attributes=_ATTRS)

    def val(e, a):
        if a not in e:
            return ""
        v = e[a].value
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        return str(v) if v is not None else ""

    res = []
    for e in conn.entries:
        uid = val(e, "uid")
        if not uid:
            continue
        res.append({
            "uid": uid,
            "cn": val(e, "cn") or val(e, "displayName"),
            "givenname": val(e, "givenName"),
            "sn": val(e, "sn"),
            "mail": val(e, "mail"),
            "title": val(e, "title"),
            "ou": val(e, "ou"),
            "phone": val(e, "telephoneNumber"),
            "mobile": val(e, "mobile"),
            "empnum": val(e, "employeeNumber"),
            "blocked": val(e, "nsaccountlock").upper() == "TRUE",
        })
    conn.unbind()
    return res


def fetch_all():
    """Совместимость: список {cn, mail} пользователей с почтой (для сопоставления врачам)."""
    return [{"cn": u["cn"], "mail": u["mail"]} for u in fetch_users() if u["cn"] and u["mail"]]


def sync_to_map():
    """Тянет пользователей из IPA: сохраняет полный список (страница «Пользователи»)
    и почты в email_map по нормализованному ФИО. Возвращает (загружено, сопоставлено врачам)."""
    users = fetch_users()
    storage.set_ipa_users(users)
    pairs = [(" ".join(u["cn"].upper().split()), u["mail"])
             for u in users if u["cn"] and u["mail"]]
    storage.bulk_set_emails(pairs)
    docs = storage.doctors()
    matched = sum(1 for d in docs if d["email"])
    return len(users), matched


def run_sync_and_record():
    """Синхронизация + запись времени и результата (для автосинхронизации и UI)."""
    loaded, matched = sync_to_map()
    storage.cfg_set("ipa_last_sync", datetime.datetime.now().isoformat(timespec="seconds"))
    storage.cfg_set("ipa_last_result", f"загружено {loaded}, сопоставлено врачам {matched}")
    return loaded, matched


def last_sync_info():
    return storage.cfg_get("ipa_last_sync"), storage.cfg_get("ipa_last_result")


def autosync_enabled():
    return cfg.get_bool("IPA_AUTOSYNC", False)


def sync_hours():
    try:
        return max(1, int(float(cfg.get("IPA_SYNC_HOURS", "24") or 24)))
    except ValueError:
        return 24


def due():
    """Пора ли запускать автосинхронизацию."""
    if not (autosync_enabled() and available()):
        return False
    ts = storage.cfg_get("ipa_last_sync")
    if not ts:
        return True
    try:
        last = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return True
    return (datetime.datetime.now() - last) >= datetime.timedelta(hours=sync_hours())
