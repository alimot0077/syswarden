"""Tiny translation layer: English source strings, Persian catalog, per-user language setting.

Language resolution: settings.json "language" ("en", "fa" or "auto"); "auto" follows $LANG.
"""

import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.config', 'syswarden')
SETTINGS_FILE = os.path.join(CONFIG_DIR, 'settings.json')
LANGUAGES = {'en': 'English', 'fa': 'فارسی'}

_lang = None
_catalog = {}


def load_settings():
    try:
        with open(SETTINGS_FILE) as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = load_settings()
    data.update(changes)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_FILE)
    reload()


def _resolve():
    choice = os.environ.get('SYSWARDEN_LANG') or load_settings().get('language', 'auto')
    if choice in LANGUAGES:
        return choice
    env = os.environ.get('LANGUAGE') or os.environ.get('LC_ALL') or os.environ.get('LC_MESSAGES') \
        or os.environ.get('LANG') or ''
    return 'fa' if env.startswith('fa') else 'en'


def reload():
    global _lang, _catalog
    _lang = _resolve()
    if _lang == 'fa':
        from .locale_fa import FA
        _catalog = FA
    else:
        _catalog = {}
    return _lang


def lang():
    return _lang or reload()


def is_rtl():
    return lang() == 'fa'


def _(text):
    lang()
    return _catalog.get(text, text)


_FA_DIGITS = str.maketrans('0123456789.%', '۰۱۲۳۴۵۶۷۸۹٫٪')


def num(value):
    """Localized digits (Persian digits in Persian mode)."""
    text = str(value)
    return text.translate(_FA_DIGITS) if lang() == 'fa' else text
