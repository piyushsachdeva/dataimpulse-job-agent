"""SQLite tracker so the agent remembers what it has already seen and shortlisted."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "jobs.db"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, url TEXT,
            sponsorship TEXT, status TEXT DEFAULT 'new', score INTEGER, notes TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP)"""
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "country" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN country TEXT")
    if "matched" not in cols:  # jobs that matched a search; these go in the spreadsheet
        conn.execute("ALTER TABLE jobs ADD COLUMN matched INTEGER DEFAULT 0")
    return conn


def mark_seen(jobs) -> set[str]:
    """Insert jobs not seen before; return the ids that are new."""
    with db() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
        fresh = [j for j in jobs if j.id not in known]
        conn.executemany(
            "INSERT INTO jobs (id, company, title, location, url, sponsorship, country) VALUES (?,?,?,?,?,?,?)",
            [(j.id, j.company, j.title, j.location, j.url, j.sponsorship, j.country) for j in fresh],
        )
    return {j.id for j in fresh}


def mark_matched(ids) -> None:
    with db() as conn:
        conn.executemany("UPDATE jobs SET matched = 1 WHERE id = ?", [(i,) for i in ids])


def sheet_rows() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE matched = 1 OR status != 'new' "
            "ORDER BY first_seen DESC, score DESC")]


def update(job_id: str, status: str | None, score: int | None, notes: str | None) -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = COALESCE(?, status), score = COALESCE(?, score), "
            "notes = COALESCE(?, notes) WHERE id = ?",
            (status, score, notes, job_id),
        )
    return cur.rowcount > 0


def list_jobs(status: str | None = None, country: str | None = None, title: str | None = None,
              matched_only: bool = False, unscored_only: bool = False, limit: int | None = None) -> list[dict]:
    where, args = [], []
    if status:
        where.append("status = ?"); args.append(status)
    if country:
        where.append("country LIKE ?"); args.append(f"%{country}%")
    if title:
        where.append("title LIKE ?"); args.append(f"%{title}%")
    if matched_only:
        where.append("matched = 1")
    if unscored_only:
        where.append("score IS NULL")
    q = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY score DESC, first_seen DESC" + (f" LIMIT {int(limit)}" if limit else "")
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args)]
