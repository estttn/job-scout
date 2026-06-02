# -*- coding: utf-8 -*-
"""Cover letters via DeepSeek — per-resume candidate data only."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.candidate import letter_footer, merge_profile_for_letters

ROOT = Path(__file__).resolve().parent.parent

BAD_PATTERNS = (
    "По описанию вижу пересечение с моим опытом",
    "UX/UI -> PM -> delivery: discovery, бэклог",
    "Задачи внедрения и интеграций близки: presale -> требования",
    "Вёл масштабные enterprise-проекты: проработка процессов",
)

_ENGLISH_SKILL_RE = re.compile(
    r"(англиск\w*|english|upper[\s-]?intermediate|pre[\s-]?intermediate|"
    r"fluent|bilingual|b1|b2|c1|уровень\s+языка)",
    re.IGNORECASE,
)


def _load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_project_env()


def _resolved_profile(profile: dict | None) -> dict:
    return merge_profile_for_letters(profile or {})


def is_bad_letter(text: str) -> bool:
    """Template / spam phrases only (not «english» — too many false positives)."""
    lower = (text or "").lower()
    return any(p.lower() in lower for p in BAD_PATTERNS)


def _name_in_letter(text: str, name: str) -> bool:
    if not name.strip():
        return True
    t = text.lower()
    n = name.lower().strip()
    if n in t:
        return True
    first = n.split()[0]
    return len(first) >= 3 and first in t


def _has_contacts_in_letter(text: str, profile: dict) -> bool:
    p = _resolved_profile(profile)
    block = (p.get("contacts_block") or "").strip()
    if block and block in text:
        return True
    contacts = p.get("contacts") or {}
    for key in ("phone", "email", "telegram"):
        val = (contacts.get(key) or "").strip()
        if val and val in text:
            return True
    return bool(block or contacts.get("phone") or contacts.get("email"))


def _why_incomplete(text: str, profile: dict, *, strict: bool) -> str:
    t = (text or "").strip()
    if not t:
        return "пустой ответ модели"
    min_len = 200 if strict else 140
    if len(t) < min_len:
        return f"слишком короткое ({len(t)} симв.)"
    if is_bad_letter(t):
        return "шаблонная фраза"
    name = (_resolved_profile(profile).get("candidate_name") or "").strip()
    if strict and name and not _name_in_letter(t, name):
        return f"нет имени «{name.split()[0]}» в тексте"
    if not _has_contacts_in_letter(t, profile):
        return "нет блока контактов"
    return "не прошло проверку качества"


def is_complete_letter(text: str, profile: dict | None = None, *, strict: bool = True) -> bool:
    p = _resolved_profile(profile)
    t = (text or "").strip()
    return not _why_incomplete(t, p, strict=strict)


def _clean_description(description: str, company: str) -> str:
    text = re.sub(r"\s+", " ", (description or "").strip())
    if not text:
        return ""
    text = re.sub(
        r"^(Мы\s*[—\-]\s*|We are\s*|О компании\s*|About us\s*)[^.]{0,200}\.\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if company and company != "—":
        text = re.sub(
            rf"^{re.escape(company)}\s*[—\-]?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
    return text[:2800].strip()


def _strip_foreign_footer(text: str, profile: dict) -> str:
    """Remove lines that look like another person's signature/footer."""
    p = _resolved_profile(profile)
    name = (p.get("candidate_name") or "").strip()
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2:
        tail = "\n".join(lines[-4:]).lower()
        if name and name.lower() not in tail:
            if re.search(r"владислав|180\s*k|ярославл", tail):
                lines = lines[:-4]
    return "\n".join(lines).strip()


def _finalize_letter(content: str, profile: dict | None) -> str:
    p = _resolved_profile(profile)
    t = content.strip()
    t = re.sub(r"^```(?:markdown|text)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    t = _strip_foreign_footer(t, p)

    footer = letter_footer(p)
    if not footer:
        return t

    name = (p.get("candidate_name") or "").strip()
    if name and name in t[-120:] and (p.get("contacts_block") or "") in t:
        return t

    t = re.sub(
        r"\n*(Удалёнка|Ярославль|ЗП:|от \d+.*на руки|Владислав).*$",
        "",
        t,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    if t and t[-1].isalpha() and len(t) > 400:
        t = re.sub(r"[^.!?\n]*$", "", t).strip()

    return f"{t}\n\n{footer}".strip()


def generate_cover_letter(
    *,
    title: str,
    company: str,
    salary: str,
    description: str,
    profile: dict | None = None,
) -> str:
    p = _resolved_profile(profile)
    if not (p.get("resume_summary") or "").strip():
        raise RuntimeError(
            "В резюме нет текста — загрузите файл резюме, чтобы письма были персональными"
        )
    if not (p.get("candidate_name") or "").strip():
        raise RuntimeError("Не удалось определить имя из резюме — проверьте файл")
    if not (p.get("contacts_block") or "").strip():
        raise RuntimeError(
            "В резюме не найдены контакты (телефон или email) — добавьте их в файл"
        )

    desc = _clean_description(description, company)
    last_err: Exception | None = None
    last_letter = ""
    incomplete_reason = ""
    for attempt in range(4):
        strict = attempt < 3
        try:
            letter = _deepseek_letter(title, company, salary, desc, p)
            last_letter = letter or ""
            if letter and is_complete_letter(letter, p, strict=strict):
                return letter
            incomplete_reason = _why_incomplete(letter or "", p, strict=strict)
            print(
                f"Incomplete letter attempt {attempt + 1} [{title[:40]}]: {incomplete_reason}",
                flush=True,
            )
        except Exception as e:
            last_err = e
            incomplete_reason = str(e)[:200]
            print(f"DeepSeek attempt {attempt + 1} [{title[:40]}]: {e}")
            time.sleep(2 * (attempt + 1))

    if last_letter and is_complete_letter(last_letter, p, strict=False):
        return last_letter
    if last_letter and len(last_letter) >= 140 and _has_contacts_in_letter(last_letter, p):
        return last_letter

    if last_err:
        raise RuntimeError(str(last_err)[:500]) from last_err
    detail = incomplete_reason or "неизвестная причина"
    raise RuntimeError(f"Не удалось сгенерировать письмо для «{title}»: {detail}")


def _api_key() -> str:
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_MAX"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    raise RuntimeError("DEEPSEEK_API_KEY не найден в /opt/hh-job-scout/.env")


def _model() -> str:
    return os.environ.get("DEEPSEEK_MODEL_LETTERS") or "deepseek-chat"


def _deepseek_letter(
    title: str,
    company: str,
    salary: str,
    description: str,
    profile: dict,
) -> str:
    company_clean = company if company and company != "—" else "компания"
    resume = profile.get("resume_summary") or ""
    sal_note = salary if salary and salary not in ("—", "?") else "не указана"
    name = profile.get("candidate_name") or "кандидат"
    location = profile.get("location") or "не указан"
    salary_expect = profile.get("salary_note") or "не указана"
    footer = letter_footer(profile)

    prompt = f"""Напиши сопроводительное письмо на русском для отклика на HeadHunter.

Компания-работодатель: {company_clean}
Вакансия: «{title}»
Зарплата в вакансии: {sal_note}

Текст вакансии:
{description or "опирайся на название вакансии"}

Профиль кандидата (используй ТОЛЬКО эти данные о кандидате, не выдумывай других людей):
Имя: {name}
Город/формат: {location}
Ожидания по ЗП: {salary_expect}
Резюме:
{resume[:6000]}

Требования:
- Обращение к компании {company_clean}
- 2-3 конкретные связи опыта кандидата с задачами вакансии «{title}»
- Не шаблонные фразы вроде «По описанию вижу пересечение»
- НЕ упоминать английский язык и языковые навыки
- Используй только имя {name}, город {location}, зарплату {salary_expect} — не подставляй другие имена, города или суммы
- 5-7 предложений, деловой тон
- Начни: «Добрый день!»
- Основной текст БЕЗ контактов и подписи в конце
- После текста письма ОБЯЗАТЕЛЬНО добавь ровно этот блок контактов (скопируй дословно):

{footer}

- Только текст письма, без markdown"""

    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    max_tokens = int(
        os.environ.get("DEEPSEEK_MAX_TOKENS_PRO")
        or os.environ.get("DEEPSEEK_MAX_TOKENS")
        or "1200"
    )

    payload = {
        "model": _model(),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 429:
            raise RuntimeError(
                "Слишком много запросов к DeepSeek (429) — уменьшите COLLECT_LETTER_WORKERS"
            ) from e
        raise RuntimeError(f"HTTP {e.code}: {body}") from e

    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("truncated: max_tokens")

    content = choice["message"]["content"].strip()
    return _finalize_letter(content, profile)
