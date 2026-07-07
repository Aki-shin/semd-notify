# -*- coding: utf-8 -*-
"""Слой конфигурации: значение из БД (заданное в UI) имеет приоритет над переменной окружения.
Пароли и параметры SMTP/FreeIPA можно задать как в env (Host Manager), так и на странице «Настройки»."""
import os
import storage

# Ключи, которыми управляет приложение
KEYS = [
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
    "SMTP_FROM_NAME", "SMTP_TLS", "SMTP_DRYRUN",
    "SMTP_BATCH_DELAY", "SMTP_BATCH_SIZE", "SMTP_BATCH_PAUSE",
    "IPA_LDAP_URI", "IPA_BASE_DN", "IPA_BIND_DN", "IPA_BIND_PW",
    "IPA_AUTOSYNC", "IPA_SYNC_HOURS",
    "CUSTOM_DEBT", "CUSTOM_DEPT", "CUSTOM_ERR", "CUSTOM_FAP", "CUSTOM_KOIKI",
]
SECRET_KEYS = {"SMTP_PASS", "IPA_BIND_PW"}


def get(key, default=""):
    v = storage.cfg_get(key)
    if v is not None and v != "":
        return v
    return os.environ.get(key, default)


def set(key, val):
    storage.cfg_set(key, val)


def is_set(key):
    return bool(get(key, ""))


def get_bool(key, default=False):
    v = get(key, "1" if default else "0")
    return str(v).strip().lower() not in ("0", "", "false", "нет", "off")
