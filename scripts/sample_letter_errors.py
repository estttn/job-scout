#!/usr/bin/env python3
"""One-off: top letter_error messages from DB."""
from collections import Counter
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parent.parent / "data" / "hhscout.db"
if not db.exists():
    db = Path("/opt/hh-job-scout/data/hhscout.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """
    SELECT letter_error FROM vacancies
    WHERE letter_status = 'failed' AND letter_error IS NOT NULL
    ORDER BY id DESC LIMIT 80
    """
).fetchall()
errs = [r["letter_error"] or "" for r in rows]
for msg, n in Counter(e[:150] for e in errs).most_common(15):
    print(f"{n:3}  {msg}")
print("--- failed total", conn.execute(
    "SELECT COUNT(*) FROM vacancies WHERE letter_status='failed'"
).fetchone()[0])
