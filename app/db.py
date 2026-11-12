import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );
            -- 每道工序各自的进度：取回、修整、入账。
            CREATE TABLE IF NOT EXISTS job_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                rows_total INTEGER NOT NULL DEFAULT 0,
                rows_ok INTEGER NOT NULL DEFAULT 0,
                rows_blocked INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(job_id, stage)
            );
            -- 中转筐：只放取回的原文行，修整后放行的才进入入账册。
            CREATE TABLE IF NOT EXISTS staging_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                sku TEXT NOT NULL,
                raw_title TEXT NOT NULL,
                payload_json TEXT,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reject_kind TEXT,
                reason TEXT,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                cleaned_at TEXT,
                UNIQUE(job_id, sku)
            );
            """
        )
        db.commit()
