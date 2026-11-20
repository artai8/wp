"""
SQLite -> PostgreSQL One-Command Data Migration Utility
=======================================================
Usage:
    python3 tools/migrate_sqlite_to_pg.py \
        --storage-root ./storage_data \
        --pg-url postgresql://mydrive:mydrive_secret@127.0.0.1:5432/mydrive
"""

import argparse
import sqlite3
from pathlib import Path


def migrate(storage_root: str, pg_url: str):
    try:
        import psycopg2
    except ImportError:
        raise SystemExit("Please install psycopg2-binary first: pip install psycopg2-binary")

    db_dir = Path(storage_root) / "db"
    pg = psycopg2.connect(pg_url)
    pg.autocommit = False
    cur = pg.cursor()

    jobs = [
        ("index.sqlite", "files"),
        ("index.sqlite", "chunks"),
        ("index.sqlite", "file_chunks"),
        ("index.sqlite", "fast_lookup"),
        ("index.sqlite", "chunk_tiers"),
        ("commercial.sqlite", "user_memberships"),
        ("commercial.sqlite", "redeem_codes"),
        ("commercial.sqlite", "payment_orders"),
        ("commercial.sqlite", "blocked_cids"),
        ("audit.sqlite", "audit_logs"),
    ]

    for db_name, table in jobs:
        sqlite_path = db_dir / db_name
        if not sqlite_path.exists():
            continue
        sc = sqlite3.connect(str(sqlite_path))
        try:
            rows = sc.execute(f"SELECT * FROM {table}").fetchall()
            cols = [d[0] for d in sc.execute(f"SELECT * FROM {table} LIMIT 0").description]
        except sqlite3.OperationalError:
            sc.close()
            continue
        sc.close()
        if not rows:
            print(f"  skip {table} (0 rows)")
            continue
        placeholders = ",".join(["%s"] * len(cols))
        col_list = ",".join(cols)
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        cur.executemany(sql, rows)
        pg.commit()
        print(f"  migrated {table}: {len(rows)} rows")

    cur.close()
    pg.close()
    print("Migration complete.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--storage-root", default="./storage_data")
    p.add_argument("--pg-url", required=True)
    args = p.parse_args()
    migrate(args.storage_root, args.pg_url)
