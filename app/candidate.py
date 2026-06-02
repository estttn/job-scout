"""Extract candidate identity and contacts from resume text for per-user letters."""
from __future__ import annotations

import json
import re
from typing import Any

_PHONE_RE = re.compile(
    r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|\+7\d{10}"
)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_TELEGRAM_RE = re.compile(r"(?:telegram|телеграм|tg)[:\s]*@?([a-zA-Z0-9_]{4,32})", re.I)
_AT_HANDLE_RE = re.compile(r"(?<![a-zA-Z0-9])@([a-zA-Z][a-zA-Z0-9_]{3,31})")
_SALARY_RE = re.compile(
    r"(?:зп|зарплат\w*|доход)[^\d]{0,20}(\d[\d\s]{2,6})(?:\s*[-–]\s*(\d[\d\s]{2,6}))?"
    r"|(\d[\d\s]{2,5})\s*(?:тыс|k)\s*(?:на\s*руки|net|₽|руб)",
    re.I,
)
_CITY_RE = re.compile(
    r"(?:город|локация|location|прожива\w*)[:\s]*([А-Яа-яA-Za-z\-\s]{3,40})"
    r"|(?:^|\n)\s*([А-Я][а-я]+(?:\s+[А-Я][а-я]+)?)\s*[,·]\s*(?:удалён|remote)",
    re.I | re.M,
)
_NAME_LABEL_RE = re.compile(
    r"^(?:имя|фио|кандидат|name)\s*[:\-]\s*(.+)$",
    re.I | re.M,
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _format_phone(raw: str) -> str:
    d = _digits(raw)
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    if len(d) == 11 and d.startswith("7"):
        return f"+7 ({d[1:4]}) {d[4:7]}-{d[7:9]}-{d[9:11]}"
    return raw.strip()


def extract_candidate_meta(text: str, *, display_name: str = "", email: str = "") -> dict[str, Any]:
    t = (text or "").strip()
    contacts: dict[str, str] = {}

    for m in _PHONE_RE.finditer(t):
        phone = _format_phone(m.group(0))
        if len(_digits(phone)) >= 10:
            contacts["phone"] = phone
            break

    for m in _EMAIL_RE.finditer(t):
        em = m.group(0).lower()
        if not em.endswith((".png", ".jpg")):
            contacts["email"] = em
            break

    tg = _TELEGRAM_RE.search(t)
    if tg:
        contacts["telegram"] = f"@{tg.group(1).lstrip('@')}"
    else:
        for m in _AT_HANDLE_RE.finditer(t):
            handle = m.group(1)
            if handle.lower() not in ("gmail", "mail", "yandex"):
                contacts["telegram"] = f"@{handle}"
                break

    candidate_name = ""
    nm = _NAME_LABEL_RE.search(t)
    if nm:
        candidate_name = nm.group(1).strip().split("\n")[0][:80]
    if not candidate_name:
        for line in t.splitlines()[:8]:
            line = line.strip()
            if not line or len(line) > 60:
                continue
            if re.match(r"^[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}$", line):
                candidate_name = line
                break

    if not candidate_name and display_name:
        candidate_name = display_name.strip()

    location = ""
    cm = _CITY_RE.search(t)
    if cm:
        location = (cm.group(1) or cm.group(2) or "").strip().split(",")[0][:60]
    if not location:
        for city in ("Москва", "Санкт-Петербург", "Ярославль", "Казань", "Новосибирск"):
            if city.lower() in t.lower():
                location = city
                break

    salary_note = ""
    sm = _SALARY_RE.search(t)
    if sm:
        g = [x for x in sm.groups() if x]
        if g:
            low = re.sub(r"\s", "", g[0])
            if len(g) > 1 and g[1]:
                high = re.sub(r"\s", "", g[1])
                salary_note = f"от {low} до {high} на руки"
            else:
                salary_note = f"от {low} на руки"

    if email and "email" not in contacts:
        contacts["email"] = email.strip().lower()

    return {
        "candidate_name": candidate_name,
        "contacts": contacts,
        "contacts_block": format_contacts_block(contacts),
        "location": location,
        "salary_note": salary_note,
    }


def format_contacts_block(contacts: dict[str, str]) -> str:
    lines = []
    if contacts.get("phone"):
        lines.append(f"Тел.: {contacts['phone']}")
    if contacts.get("email"):
        lines.append(f"Email: {contacts['email']}")
    if contacts.get("telegram"):
        lines.append(f"Telegram: {contacts['telegram']}")
    return "\n".join(lines)


def letter_footer(profile: dict) -> str:
    name = (profile.get("candidate_name") or "").strip()
    block = (profile.get("contacts_block") or "").strip()
    if not block:
        block = format_contacts_block(profile.get("contacts") or {})
    parts = []
    if block:
        parts.append(block)
    if name:
        parts.append(name)
    return "\n\n".join(parts) if parts else ""


def merge_profile_for_letters(
    profile: dict,
    *,
    display_name: str = "",
    email: str = "",
    resume_text: str = "",
) -> dict:
    """Ensure profile has candidate fields; never fall back to another user's data."""
    out = dict(profile)
    text = resume_text or out.get("resume_summary") or ""
    meta = extract_candidate_meta(text, display_name=display_name, email=email)
    for key, val in meta.items():
        if val or key not in out or not out.get(key):
            out[key] = val
    if text:
        out["resume_summary"] = text[:8000]
    return out


def profile_from_resume_text(
    text: str,
    *,
    display_name: str = "",
    email: str = "",
    search_defaults: dict | None = None,
) -> dict:
    base = search_defaults or search_profile_defaults()
    meta = extract_candidate_meta(text, display_name=display_name, email=email)
    base.update(meta)
    base["resume_summary"] = (text or "")[:8000]
    return base


def search_profile_defaults() -> dict:
    return {
        "area": 113,
        "search_period": 7,
        "pages_per_query": 2,
        "request_delay_sec": 0.8,
        "letter_delay_sec": 0.5,
        "experience": "doesNotMatter",
        "salary_min_net": 0,
        "salary_comfort_net": 0,
        "remote": True,
        "search_queries": [
            "руководитель проектов",
            "delivery manager",
            "project manager",
        ],
        "exclude_title_keywords": [],
        "exclude_english_keywords": [],
        "include_title_keywords": [
            "project",
            "проджект",
            "проект",
            "delivery",
            "pm",
            "руководитель",
            "менеджер",
        ],
    }


def profile_to_json(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False)
