#!/usr/bin/env python3
"""Re-generate letters for vacancies with letter_status=failed."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.collector import retry_failed_letters_parallel  # noqa: E402
from app.db import (  # noqa: E402
    count_failed_letters,
    get_resume,
    init_db,
    list_active_users_with_resumes,
    load_resume_profile,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, default=None)
    parser.add_argument("--resume-id", type=int, default=None)
    args = parser.parse_args()
    init_db()

    targets: list[tuple[int, int]] = []
    if args.user_id and args.resume_id:
        targets.append((args.user_id, args.resume_id))
    else:
        for user in list_active_users_with_resumes():
            for r in user["resumes"]:
                targets.append((user["id"], r["id"]))

    for uid, rid in targets:
        before = count_failed_letters(uid, rid)
        if not before:
            print(f"uid={uid} rid={rid}: no failed letters")
            continue
        resume = get_resume(rid, uid)
        if not resume:
            continue
        profile = load_resume_profile(resume)
        stats = retry_failed_letters_parallel(uid, rid, profile)
        after = count_failed_letters(uid, rid)
        print(
            f"uid={uid} rid={rid}: retried {stats['letters_total']}, "
            f"ok+{stats['letters_done']}, still failed {after} (was {before})"
        )


if __name__ == "__main__":
    main()
