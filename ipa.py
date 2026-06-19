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


def fetch_all():
    """Возвращает список {cn, mail} всех пользователей с почтой."""
    from ldap3 import Server, Connection, ALL, SUBTREE
    c = _cfg()
    server = Server(c["uri"], get_info=ALL)
    conn = Connection(server, user=c["bind_dn"] or None, password=c["bind_pw"] or None,
                      auto_bind=True)
    conn.search(c["base"], "(&(objectClass=person)(mail=*))",
                search_scope=SUBTREE, attributes=["cn", "displayName", "mail"])
    res = []
    for e in conn.entries:
        cn = str(e.cn) if "cn" in e else (str(e.displayName) if "displayName" in e else "")
        mail = str(e.mail) if "mail" in e else ""
        if cn and mail:
            res.append({"cn": cn, "mail": mail})
    conn.unbind()
    return res


def sync_to_map():
    """Тянет почты из IPA и кладёт в email_map по нормализованному ФИО.
    Возвращает (кол-во загруженных, кол-во сопоставленных к врачам)."""
    users = fetch_all()
    pairs = []
    for u in users:
        key = " ".join(u["cn"].upper().split())
        pairs.append((key, u["mail"]))
    storage.bulk_set_emails(pairs)
    # сколько врачей теперь имеют почту
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
