"""StarAstra i18n — лёгкая локализация RU/EN для UI.

Использование:
    from starastra_i18n import tr, set_language, get_language, available_languages
    tr("btn_cancel")                 # "Отмена" / "Cancel"
    tr("status_paused_left", min=3, sec=42)

Выбор языка хранится в starastra_settings.json рядом с пакетом.
"""
from __future__ import annotations

import json
import locale
import logging
import os
import sys
import threading

logger = logging.getLogger("starastra.i18n")

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCALES_DIR = os.path.join(_PKG_DIR, "locales")
if getattr(sys, "frozen", False):                  # 3.3: рядом с exe, не в _MEIPASS
    _SETTINGS_PATH = os.path.join(
        os.path.dirname(os.path.abspath(sys.executable)),
        "starastra_settings.json")
else:
    _SETTINGS_PATH = os.path.join(os.path.dirname(_PKG_DIR), "starastra_settings.json")

_lock = threading.Lock()
_cache: dict[str, dict[str, str]] = {}
_current: str | None = None


def available_languages() -> list[str]:
    """Список кодов языков, для которых есть locales/<code>.json."""
    if not os.path.isdir(_LOCALES_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(_LOCALES_DIR)
        if f.endswith(".json")
    )


def _load(code: str) -> dict[str, str]:
    path = os.path.join(_LOCALES_DIR, f"{code}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _detect_default() -> str:
    """Системный язык: 'ru*' -> 'ru', иначе 'en'."""
    try:
        lang = (locale.getdefaultlocale()[0] or "").lower()
    except Exception:
        lang = ""
    return "ru" if lang.startswith("ru") else "en"


def _saved() -> str | None:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            code = json.load(f).get("language")
        return code if code in available_languages() else None
    except Exception:
        return None


def set_language(code: str) -> None:
    """Установить язык ("ru"/"en") и сохранить выбор в настройках.

    NB: файл настроек общий (язык — один из ключей), поэтому мерджим,
    а не перезаписываем целиком; замена атомарная.
    """
    global _current
    if code not in available_languages():
        raise ValueError(f"unknown language: {code!r}; available: {available_languages()}")
    with _lock:
        _current = code
    try:
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["language"] = code
        tmp = _SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _SETTINGS_PATH)
    except OSError as exc:
        logger.warning("could not save language preference: %s", exc)


def get_language() -> str:
    """Текущий язык (сохранённый → системный → 'en')."""
    global _current
    if _current is None:
        _current = _saved() or _detect_default()
    return _current


def tr(key: str, **kwargs) -> str:
    """Перевод по ключу с подстановкой {kwargs}. Fallback: en → сам ключ + warning."""
    lang = get_language()
    with _lock:
        table = _cache.get(lang)
        if table is None:
            try:
                table = _load(lang)
            except Exception as exc:
                logger.warning("cannot load locale %s: %s", lang, exc)
                table = {}
            _cache[lang] = table
    text = table.get(key)
    if text is None and lang != "en":
        # fallback на en, затем на ключ
        with _lock:
            en = _cache.get("en")
            if en is None:
                try:
                    en = _load("en")
                except Exception:
                    en = {}
                _cache["en"] = en
        text = en.get(key)
        if text is None:
            logger.warning("missing i18n key: %r (lang=%s)", key, lang)
            return key
    elif text is None:
        logger.warning("missing i18n key: %r (lang=%s)", key, lang)
        return key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("format failed for %r: %s", key, exc)
    return text
