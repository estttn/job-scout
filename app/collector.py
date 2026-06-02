# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
from urllib.error import HTTPError, URLError

from app.candidate import merge_profile_for_letters
from app.collect_jobs import start_letter_job
from app.db import (
    FIT_SCORE,
    count_pending_letters,
    get_resume,
    get_user_by_id,
    init_db,
    list_active_users_with_resumes,
    list_pending_letters,
    load_resume_profile,
    update_vacancy_letter,
    upsert_vacancy,
)
from app.letters import generate_cover_letter
from app.scraper import VacancyItem, parse_search_html, parse_vacancy_description
from app.scorer import score_vacancy

HH_SEARCH = "https://hh.ru/search/vacancy"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

DESC_WORKERS = max(1, int(os.getenv("COLLECT_DESC_WORKERS", "8")))
LETTER_WORKERS = max(1, int(os.getenv("COLLECT_LETTER_WORKERS", "15")))


def build_search_url(query: str, *, page: int, profile: dict) -> str:
    params = {
        "text": query,
        "area": profile.get("area", 113),
        "schedule": "remote" if profile.get("remote", True) else None,
        "search_period": profile.get("search_period", 7),
        "page": page,
    }
    exp = profile.get("experience")
    if exp:
        params["experience"] = exp
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{HH_SEARCH}?{urllib.parse.urlencode(clean)}"


def fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_vacancy_description(url: str) -> str:
    try:
        html = fetch_page(url)
        return parse_vacancy_description(html)
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"Description fetch failed {url}: {e}")
        return ""


def _profile_for_collect(user_id: int, resume_id: int, profile: dict) -> dict:
    user = get_user_by_id(user_id) or {}
    resume = get_resume(resume_id, user_id) or {}
    return merge_profile_for_letters(
        profile,
        display_name=user.get("display_name") or "",
        email=user.get("email") or "",
        resume_text=resume.get("text_content") or "",
    )


def _fetch_descriptions_parallel(items: list[VacancyItem]) -> dict[str, str]:
    if not items:
        return {}
    by_id = {it.id: it for it in items}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=DESC_WORKERS) as pool:
        futures = {pool.submit(fetch_vacancy_description, it.url): it.id for it in items}
        for fut in as_completed(futures):
            vid = futures[fut]
            try:
                out[vid] = fut.result()
            except Exception as e:
                print(f"Description worker failed {vid}: {e}")
                out[vid] = ""
    return out


def scrape_for_resume(user_id: int, resume_id: int, profile: dict) -> dict:
    """Phase 1: HH search + scoring, save vacancies with letter_status=pending."""
    profile = _profile_for_collect(user_id, resume_id, profile)
    seen_ids: set[str] = set()
    new_count = 0
    skipped_english = 0
    scanned = 0
    pages_per_query = profile.get("pages_per_query", 2)
    delay = profile.get("request_delay_sec", 0.8)
    queries = profile.get("search_queries") or []
    need_description: dict[str, VacancyItem] = {}

    for query in queries:
        for page in range(pages_per_query):
            url = build_search_url(query, page=page, profile=profile)
            try:
                html = fetch_page(url)
            except (HTTPError, URLError, TimeoutError) as e:
                print(f"HH fetch error uid={user_id} rid={resume_id} {query!r} p{page}: {e}")
                continue
            time.sleep(delay)
            items = parse_search_html(html)
            for item in items:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                scanned += 1

                fit, reason = score_vacancy(
                    title=item.title,
                    company=item.company,
                    salary=item.salary,
                    description="",
                    profile=profile,
                )
                if fit == "no":
                    if reason and reason.startswith("english"):
                        skipped_english += 1
                    continue
                need_description[item.id] = item

    desc_items = list(need_description.values())
    descriptions = _fetch_descriptions_parallel(desc_items)

    for item in desc_items:
        description = descriptions.get(item.id, "")
        fit, reason = score_vacancy(
            title=item.title,
            company=item.company,
            salary=item.salary,
            description=description,
            profile=profile,
        )
        if fit == "no":
            if reason and reason.startswith("english"):
                skipped_english += 1
            continue

        fit_label = "yes" if fit == "yes" else "partial"
        fit_score = FIT_SCORE.get(fit_label, 65)
        if upsert_vacancy(
            {
                "hh_id": item.id,
                "user_id": user_id,
                "resume_id": resume_id,
                "title": item.title,
                "company": item.company,
                "salary": item.salary,
                "url": item.url,
                "fit": fit_label,
                "fit_score": fit_score,
                "reason": reason,
                "description": description,
                "cover_letter": "",
                "letter_status": "pending",
                "letter_error": None,
            }
        ):
            new_count += 1

    pending = count_pending_letters(user_id, resume_id)
    return {
        "user_id": user_id,
        "resume_id": resume_id,
        "scanned": scanned,
        "new": new_count,
        "unique": len(seen_ids),
        "skipped_english": skipped_english,
        "pending_letters": pending,
        "phase": "listed",
    }


def generate_letters_parallel(
    user_id: int,
    resume_id: int,
    profile: dict,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> dict:
    profile = _profile_for_collect(user_id, resume_id, profile)
    pending = list_pending_letters(user_id, resume_id)
    total = len(pending)
    done = 0
    failed = 0

    if progress_cb:
        progress_cb(0, total, 0)

    def one(row: dict) -> bool:
        desc = (row.get("description") or "").strip()
        if not desc:
            desc = fetch_vacancy_description(row["url"])
        try:
            letter = generate_cover_letter(
                title=row["title"],
                company=row["company"],
                salary=row["salary"],
                description=desc,
                profile=profile,
            )
            update_vacancy_letter(
                row["id"],
                user_id,
                cover_letter=letter,
                letter_status="ok",
            )
            return True
        except Exception as e:
            update_vacancy_letter(
                row["id"],
                user_id,
                cover_letter="",
                letter_status="failed",
                letter_error=str(e)[:500],
            )
            return False

    if not pending:
        return {"letters_done": 0, "letters_failed": 0, "letters_total": 0}

    with ThreadPoolExecutor(max_workers=LETTER_WORKERS) as pool:
        futures = [pool.submit(one, row) for row in pending]
        for fut in as_completed(futures):
            if fut.result():
                done += 1
            else:
                failed += 1
            if progress_cb:
                progress_cb(done + failed, total, failed)

    return {"letters_done": done, "letters_failed": failed, "letters_total": total}


def collect_for_resume(
    user_id: int,
    resume_id: int,
    profile: dict,
    *,
    background_letters: bool = True,
) -> dict:
    result = scrape_for_resume(user_id, resume_id, profile)
    pending = result.get("pending_letters", 0)
    if pending <= 0:
        result["letters_started"] = False
        return result

    if background_letters:

        def worker(progress_cb: Callable[[int, int, int], None]) -> dict:
            agg = {"letters_done": 0, "letters_failed": 0, "letters_total": 0}
            while count_pending_letters(user_id, resume_id) > 0:
                stats = generate_letters_parallel(
                    user_id, resume_id, profile, progress_cb=progress_cb
                )
                if stats["letters_total"] == 0:
                    break
                agg["letters_done"] += stats["letters_done"]
                agg["letters_failed"] += stats["letters_failed"]
                agg["letters_total"] += stats["letters_total"]
            return agg

        started = start_letter_job(user_id, resume_id, worker)
        result["letters_started"] = started
    else:
        stats = generate_letters_parallel(user_id, resume_id, profile)
        result.update(stats)
        result["letters_started"] = False
        result["phase"] = "done"
    return result


def collect_all_sync() -> list[dict]:
    init_db()
    results: list[dict] = []
    for user in list_active_users_with_resumes():
        uid = user["id"]
        for resume in user["resumes"]:
            profile = load_resume_profile(resume)
            if not profile.get("search_queries"):
                print(f"Skip collect: no queries uid={uid} rid={resume['id']}")
                continue
            print(f"Collecting uid={uid} rid={resume['id']} resume={resume['name']!r}")
            results.append(
                collect_for_resume(uid, resume["id"], profile, background_letters=False)
            )
    return results


def collect_sync_for_user(
    user_id: int,
    resume_id: int,
    *,
    background_letters: bool = True,
) -> dict:
    init_db()
    resume = get_resume(resume_id, user_id)
    if not resume:
        return {"ok": False, "error": "resume not found", "new": 0, "scanned": 0, "unique": 0}
    profile = load_resume_profile(resume)
    result = collect_for_resume(
        user_id, resume_id, profile, background_letters=background_letters
    )
    result["ok"] = True
    return result


async def collect(
    user_id: int | None = None,
    resume_id: int | None = None,
    *,
    background_letters: bool = True,
) -> dict | list[dict]:
    if user_id is not None and resume_id is not None:
        return await asyncio.to_thread(
            collect_sync_for_user,
            user_id,
            resume_id,
            background_letters=background_letters,
        )
    return await asyncio.to_thread(collect_all_sync)


def main() -> None:
    for r in collect_all_sync():
        print(r)


if __name__ == "__main__":
    main()
