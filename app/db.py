from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.auth import ADMIN_PASSWORD, ADMIN_USERNAME, hash_password
from app.candidate import profile_from_resume_text, profile_to_json

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "hhscout.db"
RESUMES_DIR = Path(__file__).resolve().parent.parent / "data" / "resumes"

FIT_SCORE = {"yes": 90, "partial": 65, "no": 0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                email TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                file_path TEXT,
                text_content TEXT NOT NULL DEFAULT '',
                profile_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_vacancies_schema(conn)
        _migrate_vacancy_columns(conn)
        _seed_admin(conn)
        _migrate_legacy_vacancies(conn)
        conn.commit()


def _migrate_vacancy_columns(conn: sqlite3.Connection) -> None:
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vacancies'"
    ).fetchone():
        return
    cols = _table_columns(conn, "vacancies")
    if "applied_at" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN applied_at TEXT")
    if "letter_status" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN letter_status TEXT NOT NULL DEFAULT 'ok'")
    if "letter_error" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN letter_error TEXT")
    if "last_letter_try_at" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN last_letter_try_at TEXT")
    if "response_status" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN response_status TEXT")
    if "response_at" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN response_at TEXT")
    if "description" not in cols:
        conn.execute("ALTER TABLE vacancies ADD COLUMN description TEXT NOT NULL DEFAULT ''")


def _create_vacancies_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hh_id TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            url TEXT NOT NULL,
            fit TEXT NOT NULL,
            fit_score INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            description TEXT NOT NULL DEFAULT '',
            cover_letter TEXT NOT NULL,
            letter_status TEXT NOT NULL DEFAULT 'ok',
            letter_error TEXT,
            last_letter_try_at TEXT,
            response_status TEXT,
            response_at TEXT,
            applied INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE (hh_id, user_id, resume_id)
        )
        """
    )


def _ensure_vacancies_schema(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vacancies'"
    ).fetchone()
    if not exists:
        _create_vacancies_table(conn)
        return
    cols = _table_columns(conn, "vacancies")
    if "user_id" in cols:
        return
    conn.execute("ALTER TABLE vacancies RENAME TO vacancies_legacy")
    _create_vacancies_table(conn)


def _migrate_legacy_vacancies(conn: sqlite3.Connection) -> None:
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vacancies_legacy'"
    ).fetchone():
        return
    admin = conn.execute(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
        (ADMIN_USERNAME,),
    ).fetchone()
    if not admin:
        return
    admin_id = admin[0]
    resume = conn.execute(
        "SELECT id FROM resumes WHERE user_id = ? ORDER BY id LIMIT 1",
        (admin_id,),
    ).fetchone()
    if not resume:
        return
    resume_id = resume[0]
    for row in conn.execute("SELECT * FROM vacancies_legacy").fetchall():
        d = dict(row)
        hh_id = str(d.get("id") or d.get("hh_id") or "")
        if not hh_id:
            continue
        fit = d.get("fit", "partial")
        conn.execute(
            """
            INSERT OR IGNORE INTO vacancies (
                hh_id, user_id, resume_id, title, company, salary, url,
                fit, fit_score, reason, cover_letter, applied, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hh_id,
                admin_id,
                resume_id,
                d["title"],
                d.get("company") or "—",
                d.get("salary") or "—",
                d["url"],
                fit,
                FIT_SCORE.get(fit, 65),
                d.get("reason") or "",
                d.get("cover_letter") or "",
                d.get("applied") or 0,
                d.get("first_seen") or _now(),
                d.get("last_seen") or _now(),
            ),
        )


def _seed_admin(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
        (ADMIN_USERNAME,),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE users SET role = 'admin', status = 'active',
                password_hash = ?, display_name = COALESCE(display_name, ?)
            WHERE id = ?
            """,
            (hash_password(ADMIN_PASSWORD), ADMIN_USERNAME, row[0]),
        )
        admin_id = row[0]
    else:
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, display_name, role, status, created_at)
            VALUES (?, ?, ?, 'admin', 'active', ?)
            """,
            (ADMIN_USERNAME, hash_password(ADMIN_PASSWORD), ADMIN_USERNAME, now),
        )
        admin_id = cur.lastrowid
    has_resume = conn.execute(
        "SELECT id FROM resumes WHERE user_id = ? LIMIT 1",
        (admin_id,),
    ).fetchone()
    if not has_resume:
        _create_resume(
            conn,
            user_id=admin_id,
            name="Основное резюме",
            text_content="",
            profile_json=admin_seed_profile_json(""),
        )


def default_profile_json(
    resume_summary: str,
    *,
    display_name: str = "",
    email: str = "",
) -> str:
    profile = profile_from_resume_text(
        resume_summary,
        display_name=display_name,
        email=email,
    )
    return profile_to_json(profile)


def _default_profile_json(resume_summary: str) -> str:
    return default_profile_json(resume_summary)


def admin_seed_profile_json(resume_summary: str = "") -> str:
    """Admin-only defaults when bootstrapping from legacy profile.json."""
    profile = profile_from_resume_text(
        resume_summary,
        display_name=ADMIN_USERNAME,
    )
    legacy_path = Path(__file__).resolve().parent.parent / "profile.json"
    if legacy_path.exists():
        with open(legacy_path, encoding="utf-8") as f:
            legacy = json.load(f)
        for key in (
            "search_queries",
            "salary_min_net",
            "salary_comfort_net",
            "area",
            "remote",
        ):
            if key in legacy:
                profile[key] = legacy[key]
        if legacy.get("resume_summary") and not resume_summary:
            profile["resume_summary"] = legacy["resume_summary"][:8000]
            meta = profile_from_resume_text(
                profile["resume_summary"],
                display_name=ADMIN_USERNAME,
            )
            profile.update({k: v for k, v in meta.items() if v})
    return profile_to_json(profile)


def update_resume_file(
    resume_id: int,
    user_id: int,
    *,
    file_path: str,
    text_content: str,
    display_name: str = "",
    email: str = "",
) -> None:
    profile_json = default_profile_json(
        text_content,
        display_name=display_name,
        email=email,
    )
    with connect() as conn:
        conn.execute(
            """
            UPDATE resumes SET file_path = ?, text_content = ?, profile_json = ?
            WHERE id = ? AND user_id = ?
            """,
            (file_path, text_content, profile_json, resume_id, user_id),
        )
        conn.commit()


def get_user_by_username(username: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def create_user(
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    email: str | None = None,
    role: str = "user",
    status: str = "pending",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, display_name, email, role, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username.strip(),
                hash_password(password),
                display_name or username.strip(),
                email,
                role,
                status,
                _now(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_pending_users() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM users WHERE status = 'pending' AND role = 'user'
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_users() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role = 'user' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def set_user_status(user_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        conn.commit()


def list_active_users_with_resumes() -> list[dict]:
    with connect() as conn:
        users = conn.execute(
            "SELECT * FROM users WHERE status = 'active'"
        ).fetchall()
        out = []
        for u in users:
            ud = dict(u)
            resumes = conn.execute(
                "SELECT * FROM resumes WHERE user_id = ? ORDER BY id",
                (ud["id"],),
            ).fetchall()
            ud["resumes"] = [dict(r) for r in resumes]
            if ud["resumes"]:
                out.append(ud)
    return out


def _create_resume(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    name: str,
    text_content: str,
    profile_json: str,
    file_path: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO resumes (user_id, name, file_path, text_content, profile_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, file_path, text_content, profile_json, _now()),
    )
    return int(cur.lastrowid)


def create_resume(
    *,
    user_id: int,
    name: str,
    text_content: str,
    file_path: str | None = None,
    display_name: str = "",
    email: str = "",
) -> int:
    profile_json = default_profile_json(
        text_content,
        display_name=display_name,
        email=email,
    )
    with connect() as conn:
        rid = _create_resume(
            conn,
            user_id=user_id,
            name=name,
            text_content=text_content,
            profile_json=profile_json,
            file_path=file_path,
        )
        conn.commit()
        return rid


def list_resumes(user_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM resumes WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_resume(resume_id: int, user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM resumes WHERE id = ? AND user_id = ?",
            (resume_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def delete_resume(resume_id: int, user_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT file_path FROM resumes WHERE id = ? AND user_id = ?",
            (resume_id, user_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "DELETE FROM vacancies WHERE resume_id = ? AND user_id = ?",
            (resume_id, user_id),
        )
        conn.execute(
            "DELETE FROM resumes WHERE id = ? AND user_id = ?",
            (resume_id, user_id),
        )
        conn.commit()
    if row["file_path"]:
        path = Path(row["file_path"])
        if path.exists():
            path.unlink(missing_ok=True)
    return True


def load_resume_profile(resume: dict) -> dict:
    return json.loads(resume.get("profile_json") or "{}")


def upsert_vacancy(row: dict) -> bool:
    now = _now()
    fit = row["fit"]
    fit_score = row.get("fit_score", FIT_SCORE.get(fit, 65))
    letter_status = row.get("letter_status", "ok")
    letter_error = row.get("letter_error")
    description = row.get("description") or ""
    cover_letter = row.get("cover_letter", "")
    with connect() as conn:
        cur = conn.execute(
            """
            SELECT id, cover_letter, letter_status FROM vacancies
            WHERE hh_id = ? AND user_id = ? AND resume_id = ?
            """,
            (row["hh_id"], row["user_id"], row["resume_id"]),
        )
        exists = cur.fetchone()
        if exists:
            existing_letter = (exists["cover_letter"] or "").strip()
            if existing_letter and exists["letter_status"] == "ok":
                cover_letter = exists["cover_letter"]
                letter_status = "ok"
                letter_error = None
            conn.execute(
                """
                UPDATE vacancies SET
                    last_seen = ?,
                    salary = COALESCE(?, salary),
                    fit = ?,
                    fit_score = ?,
                    reason = COALESCE(?, reason),
                    description = COALESCE(?, description),
                    cover_letter = ?,
                    letter_status = ?,
                    letter_error = ?,
                    last_letter_try_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    row.get("salary"),
                    fit,
                    fit_score,
                    row.get("reason"),
                    description or None,
                    cover_letter,
                    letter_status,
                    letter_error,
                    _now(),
                    exists["id"],
                ),
            )
            conn.commit()
            return False
        conn.execute(
            """
            INSERT INTO vacancies (
                hh_id, user_id, resume_id, title, company, salary, url,
                fit, fit_score, reason, description, cover_letter, letter_status,
                letter_error, last_letter_try_at, applied, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                row["hh_id"],
                row["user_id"],
                row["resume_id"],
                row["title"],
                row.get("company") or "—",
                row.get("salary") or "—",
                row["url"],
                fit,
                fit_score,
                row.get("reason") or "",
                description,
                cover_letter,
                letter_status,
                letter_error,
                _now(),
                now,
                now,
            ),
        )
        conn.commit()
        return True


def list_pending_letters(user_id: int, resume_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, company, salary, url, description
            FROM vacancies
            WHERE user_id = ? AND resume_id = ?
              AND applied = 0
              AND letter_status = 'pending'
            ORDER BY fit_score DESC, id ASC
            """,
            (user_id, resume_id),
        ).fetchall()
    return [dict(r) for r in rows]


def count_pending_letters(user_id: int, resume_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM vacancies
            WHERE user_id = ? AND resume_id = ?
              AND applied = 0 AND letter_status = 'pending'
            """,
            (user_id, resume_id),
        ).fetchone()
    return int(row[0]) if row else 0


def update_vacancy_letter(
    vacancy_id: int,
    user_id: int,
    *,
    cover_letter: str,
    letter_status: str,
    letter_error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE vacancies SET
                cover_letter = ?,
                letter_status = ?,
                letter_error = ?,
                last_letter_try_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (cover_letter, letter_status, letter_error, _now(), vacancy_id, user_id),
        )
        conn.commit()


def list_vacancies(
    user_id: int,
    resume_id: int,
    *,
    hide_applied: bool = False,
    only_applied: bool = False,
    fit_min: int | None = None,
    date_filter: str | None = None,
    sort: str = "date_desc",
) -> list[dict]:
    clauses = ["user_id = ?", "resume_id = ?"]
    params: list[Any] = [user_id, resume_id]

    if only_applied:
        clauses.append("applied = 1")
    elif hide_applied:
        clauses.append("applied = 0")
    if fit_min is not None:
        clauses.append("fit_score >= ?")
        params.append(fit_min)

    date_col = "applied_at" if only_applied else "first_seen"
    if date_filter == "today":
        clauses.append(f"date({date_col}) = date('now', 'localtime')")
    elif date_filter == "yesterday":
        clauses.append(f"date({date_col}) = date('now', 'localtime', '-1 day')")
    elif date_filter == "week":
        clauses.append(f"date({date_col}) >= date('now', 'localtime', '-7 days')")

    where = "WHERE " + " AND ".join(clauses)
    if only_applied:
        default_order = "applied_at DESC"
    else:
        default_order = "first_seen DESC"
    order = {
        "date_desc": default_order,
        "date_asc": f"{date_col} ASC",
        "fit_desc": f"fit_score DESC, {default_order}",
    }.get(sort, default_order)

    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM vacancies {where} ORDER BY {order}",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def mark_applied(vacancy_id: int, user_id: int) -> bool:
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE vacancies SET applied = 1, applied_at = COALESCE(applied_at, ?)
            WHERE id = ? AND user_id = ?
            """,
            (now, vacancy_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_vacancy_response(vacancy_id: int, user_id: int, response_status: str) -> bool:
    if response_status not in ("invited", "rejected"):
        return False
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE vacancies
            SET response_status = ?, response_at = ?
            WHERE id = ? AND user_id = ? AND applied = 1
            """,
            (response_status, now, vacancy_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def stats(user_id: int, resume_id: int) -> dict:
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM vacancies WHERE user_id = ? AND resume_id = ?",
            (user_id, resume_id),
        ).fetchone()[0]
        applied = conn.execute(
            """
            SELECT COUNT(*) FROM vacancies
            WHERE user_id = ? AND resume_id = ? AND applied = 1
            """,
            (user_id, resume_id),
        ).fetchone()[0]
        new_today = conn.execute(
            """
            SELECT COUNT(*) FROM vacancies
            WHERE user_id = ? AND resume_id = ?
              AND date(first_seen) = date('now', 'localtime')
            """,
            (user_id, resume_id),
        ).fetchone()[0]
    return {"total": total, "applied": applied, "new_today": new_today}


def get_vacancy_for_user(vacancy_id: int, user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM vacancies WHERE id = ? AND user_id = ?",
            (vacancy_id, user_id),
        ).fetchone()
    return dict(row) if row else None
