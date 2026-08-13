"""
merge_data.py — Step 2: Entity matching and PostgreSQL loading for ConsultBae merge project.

Reads the three cleaned CSVs produced by clean_data.py and:
  1. Connects to a hosted PostgreSQL database (Render).
  2. Creates one raw table per source for traceability.
  3. Runs Option-C entity matching (phone OR email) to identify the same person
     across files — no single ID is shared, so we infer identity from normalized keys.
  4. Builds a `persons` master table — one row per real person.
  5. Builds a `person_sources` link table recording which source(s) contributed.

Switched from SQLite → PostgreSQL because:
  - SQLite is a local file; it cannot be shared between a local pipeline and a
    deployed FastAPI app on Render without committing the file to git (fragile).
  - PostgreSQL on Render is a hosted server — both this script (local) and the
    Task 3 FastAPI app (Render) connect to it via the same DATABASE_URL.
  - PostgreSQL handles concurrent writes (multiple audio submissions at once)
    without the single-writer lock that SQLite has.

Key syntax changes from SQLite:
  - AUTOINCREMENT → SERIAL (Postgres auto-increment)
  - INTEGER (0/1 booleans) → BOOLEAN (native Postgres type)
  - Placeholder ? → %s  (psycopg2 uses %s, not ?)
  - executescript() → individual execute() calls (psycopg2 has no executescript)
  - PRAGMA → removed (SQLite-only directive)
  - pandas .to_sql() → requires SQLAlchemy engine, not a raw psycopg2 connection

DATABASE_URL is read from the DATABASE_URL environment variable first.
Falls back to the hardcoded Render URL for local dev convenience.
In production / CI, always set the env var — never hardcode credentials in code.
"""

import os
import psycopg2
import pandas as pd
from sqlalchemy import create_engine
from dotenv import load_dotenv

# Load variables from .env file when running locally.
# On Render (or any CI server), env vars are set in the dashboard — load_dotenv()
# is a no-op when the variable is already set in the environment, so this is safe
# to call unconditionally.
load_dotenv()

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

S1_PATH = os.path.join(OUTPUT_DIR, "source1_clean.csv")
S2_PATH = os.path.join(OUTPUT_DIR, "source2_clean.csv")
S3_PATH = os.path.join(OUTPUT_DIR, "source3_clean.csv")

# ─── Database URL ─────────────────────────────────────────────────────────────
# Loaded from .env locally (via load_dotenv above) or from the Render dashboard
# in production. Never hardcoded — if this raises, set DATABASE_URL in your .env.
_RAW_URL = os.environ["DATABASE_URL"]   # raises KeyError if not set — intentional
# Render external URLs require SSL
DATABASE_URL = _RAW_URL if "sslmode" in _RAW_URL else _RAW_URL + "?sslmode=require"

# SQLAlchemy needs the driver spelled out explicitly for psycopg2
# Replace "postgresql://" with "postgresql+psycopg2://" for SQLAlchemy
SQLALCHEMY_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def merge_skills(*skill_strings) -> str:
    """
    Union skill tags from multiple sources into one deduplicated, sorted string.
    Each input is a ', '-separated string (or None/NaN).
    """
    combined = set()
    for s in skill_strings:
        if s and str(s).strip():
            for tag in str(s).split(","):
                tag = tag.strip()
                if tag:
                    combined.add(tag)
    return ", ".join(sorted(combined))


def safe(val):
    """Return None for NaN/NaT/empty, otherwise the raw value. Keeps the DB clean."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


# ─── Database connection ──────────────────────────────────────────────────────

def get_conn():
    """
    Open a psycopg2 connection to the PostgreSQL database.
    sslmode=require is mandatory for Render's external hostname.
    """
    return psycopg2.connect(DATABASE_URL)


def get_engine():
    """
    Create a SQLAlchemy engine — required by pandas .to_sql().
    Raw psycopg2 connections are not accepted by pandas for Postgres.
    """
    return create_engine(SQLALCHEMY_URL)


# ─── Schema ───────────────────────────────────────────────────────────────────

def create_schema(conn):
    """
    Drop and recreate all tables so reruns are idempotent.

    PostgreSQL differences from the old SQLite schema:
      - SERIAL PRIMARY KEY  instead of  INTEGER PRIMARY KEY AUTOINCREMENT
      - BOOLEAN             instead of  INTEGER  for verified column
      - %s placeholders     instead of  ?
      - No PRAGMA (SQLite-only)
      - executescript() doesn't exist — each statement is a separate execute()
    """
    cur = conn.cursor()

    # Drop in reverse dependency order (person_sources references persons)
    drops = [
        "DROP TABLE IF EXISTS person_sources CASCADE",
        "DROP TABLE IF EXISTS persons CASCADE",
        "DROP TABLE IF EXISTS naukri_applicants CASCADE",
        "DROP TABLE IF EXISTS gig_workers CASCADE",
        "DROP TABLE IF EXISTS cbnexus_contacts CASCADE",
    ]
    for stmt in drops:
        cur.execute(stmt)

    # Source 1: Naukri applicants
    cur.execute("""
        CREATE TABLE naukri_applicants (
            id               SERIAL PRIMARY KEY,
            full_name        TEXT,
            email            TEXT,
            phone_normalized TEXT,
            city_normalized  TEXT,
            experience_years REAL,
            ctc_lakhs        REAL,
            applied_date     TEXT,
            skills_normalized TEXT
        )
    """)

    # Source 2: Gig workers (no phone column)
    cur.execute("""
        CREATE TABLE gig_workers (
            id                SERIAL PRIMARY KEY,
            email             TEXT,
            worker_name       TEXT,
            rate_value        REAL,
            rate_unit         TEXT,
            city_normalized   TEXT,
            status_normalized TEXT,
            skills_normalized TEXT
        )
    """)

    # Source 3: CBNexus contacts (no email column)
    # verified is BOOLEAN in Postgres — no need for 0/1 integer encoding
    cur.execute("""
        CREATE TABLE cbnexus_contacts (
            id                 SERIAL PRIMARY KEY,
            full_name          TEXT,
            phone_normalized   TEXT,
            city_normalized    TEXT,
            verified           BOOLEAN,
            projects_completed INTEGER
        )
    """)

    # Master merged persons table — one row per real person
    cur.execute("""
        CREATE TABLE persons (
            person_id          SERIAL PRIMARY KEY,
            full_name          TEXT,
            email              TEXT,
            phone              TEXT,
            city               TEXT,
            experience_years   REAL,
            ctc_lakhs          REAL,
            applied_date       TEXT,
            skills             TEXT,
            rate_value         REAL,
            rate_unit          TEXT,
            status             TEXT,
            verified           BOOLEAN,
            projects_completed INTEGER,
            sources            TEXT
        )
    """)

    # Link table — which source rows fed into each merged person
    cur.execute("""
        CREATE TABLE person_sources (
            id            SERIAL PRIMARY KEY,
            person_id     INTEGER NOT NULL REFERENCES persons(person_id),
            source_name   TEXT NOT NULL,
            source_row_id INTEGER,
            match_key     TEXT
        )
    """)

    conn.commit()
    cur.close()
    print("  [db] Schema created (5 tables) on PostgreSQL.")


# ─── Load source tables ───────────────────────────────────────────────────────

def load_source_tables(engine, df1, df2, df3):
    """
    Bulk-insert each cleaned DataFrame into its source table using SQLAlchemy.
    pandas .to_sql() requires a SQLAlchemy engine (not a raw psycopg2 connection).
    if_exists='append' preserves the SERIAL PKs assigned by Postgres.
    """
    df1.to_sql("naukri_applicants", engine, if_exists="append", index=False)
    df2.to_sql("gig_workers",       engine, if_exists="append", index=False)

    # Postgres BOOLEAN column accepts Python True/False directly — no 0/1 conversion needed
    df3.to_sql("cbnexus_contacts",  engine, if_exists="append", index=False)

    print(f"  [db] Source tables loaded: "
          f"{len(df1)} naukri | {len(df2)} gig_workers | {len(df3)} cbnexus")


# ─── Entity matching ──────────────────────────────────────────────────────────

def build_persons(conn, df1, df2, df3):
    """
    Core entity-matching loop — Option C (phone OR email union).
    Logic is identical to the SQLite version; only the INSERT syntax changes
    (%s placeholders instead of ?, and BOOLEAN values for verified).
    """
    phone_index: dict[str, int] = {}
    email_index: dict[str, int] = {}
    persons: dict[int, dict] = {}
    person_sources_rows: list[dict] = []
    next_pid = 1

    def find_person(phone, email):
        pid_phone = phone_index.get(phone) if phone else None
        pid_email = email_index.get(email) if email else None
        if pid_phone and pid_email:
            if pid_phone == pid_email:
                return pid_phone, "phone+email"
            else:
                print(f"  ⚠️  CONFLICT: phone→person {pid_phone} but email→person {pid_email}. "
                      f"Trusting phone. (phone={phone}, email={email})")
                return pid_phone, "phone"
        if pid_phone: return pid_phone, "phone"
        if pid_email: return pid_email, "email"
        return None, "new"

    def register_keys(pid, phone, email):
        if phone: phone_index[phone] = pid
        if email: email_index[email] = pid

    def merge_field(existing, new):
        return existing if existing is not None else new

    # ── Pass 1: Source 1 ──
    print("\n  [match] Processing Source 1 (naukri_applicants)...")
    s1_new = s1_matched = 0
    for row_idx, row in df1.iterrows():
        phone = safe(row.get("phone_normalized"))
        email = safe(row.get("email"))
        pid, key = find_person(phone, email)
        if pid is None:
            pid = next_pid; next_pid += 1
            persons[pid] = {
                "full_name": safe(row.get("full_name")), "email": email, "phone": phone,
                "city": safe(row.get("city_normalized")),
                "experience_years": safe(row.get("experience_years")),
                "ctc_lakhs": safe(row.get("ctc_lakhs")),
                "applied_date": safe(row.get("applied_date")),
                "skills": safe(row.get("skills_normalized")),
                "rate_value": None, "rate_unit": None, "status": None,
                "verified": None, "projects_completed": None, "sources": {"s1"},
            }
            register_keys(pid, phone, email); s1_new += 1
        else:
            p = persons[pid]
            p["full_name"] = merge_field(p["full_name"], safe(row.get("full_name")))
            p["email"]     = merge_field(p["email"], email)
            p["phone"]     = merge_field(p["phone"], phone)
            p["city"]      = merge_field(p["city"], safe(row.get("city_normalized")))
            p["experience_years"] = merge_field(p["experience_years"], safe(row.get("experience_years")))
            p["ctc_lakhs"] = merge_field(p["ctc_lakhs"], safe(row.get("ctc_lakhs")))
            p["applied_date"] = merge_field(p["applied_date"], safe(row.get("applied_date")))
            p["skills"] = merge_skills(p["skills"], safe(row.get("skills_normalized")))
            p["sources"].add("s1"); register_keys(pid, phone, email); s1_matched += 1
        person_sources_rows.append({"person_id": pid, "source_name": "s1",
                                    "source_row_id": row_idx + 1, "match_key": key})
    print(f"    → {s1_new} new, {s1_matched} matched")

    # ── Pass 2: Source 2 (email only — no phone) ──
    print("  [match] Processing Source 2 (gig_workers)...")
    s2_new = s2_matched = 0
    for row_idx, row in df2.iterrows():
        email = safe(row.get("email"))
        pid, key = find_person(None, email)
        if pid is None:
            pid = next_pid; next_pid += 1
            persons[pid] = {
                "full_name": safe(row.get("worker_name")), "email": email, "phone": None,
                "city": safe(row.get("city_normalized")), "experience_years": None,
                "ctc_lakhs": None, "applied_date": None,
                "skills": safe(row.get("skills_normalized")),
                "rate_value": safe(row.get("rate_value")), "rate_unit": safe(row.get("rate_unit")),
                "status": safe(row.get("status_normalized")),
                "verified": None, "projects_completed": None, "sources": {"s2"},
            }
            register_keys(pid, None, email); s2_new += 1
        else:
            p = persons[pid]
            p["rate_value"] = merge_field(p["rate_value"], safe(row.get("rate_value")))
            p["rate_unit"]  = merge_field(p["rate_unit"],  safe(row.get("rate_unit")))
            p["status"]     = merge_field(p["status"],     safe(row.get("status_normalized")))
            p["skills"]     = merge_skills(p["skills"],    safe(row.get("skills_normalized")))
            p["full_name"]  = merge_field(p["full_name"],  safe(row.get("worker_name")))
            p["city"]       = merge_field(p["city"],       safe(row.get("city_normalized")))
            p["sources"].add("s2"); register_keys(pid, None, email); s2_matched += 1
        person_sources_rows.append({"person_id": pid, "source_name": "s2",
                                    "source_row_id": row_idx + 1, "match_key": key})
    print(f"    → {s2_new} new, {s2_matched} matched by email")

    # ── Pass 3: Source 3 (phone only — no email) ──
    print("  [match] Processing Source 3 (cbnexus_contacts)...")
    s3_new = s3_matched = 0
    for row_idx, row in df3.iterrows():
        phone = safe(row.get("phone_normalized"))
        pid, key = find_person(phone, None)
        verified_raw = row.get("verified")
        # Convert pandas bool → Python bool (Postgres BOOLEAN accepts True/False directly)
        verified_val = bool(verified_raw) if verified_raw is not None and not pd.isna(verified_raw) else None
        if pid is None:
            pid = next_pid; next_pid += 1
            persons[pid] = {
                "full_name": safe(row.get("full_name")), "email": None, "phone": phone,
                "city": safe(row.get("city_normalized")), "experience_years": None,
                "ctc_lakhs": None, "applied_date": None, "skills": None,
                "rate_value": None, "rate_unit": None, "status": None,
                "verified": verified_val,
                "projects_completed": safe(row.get("projects_completed")),
                "sources": {"s3"},
            }
            register_keys(pid, phone, None); s3_new += 1
        else:
            p = persons[pid]
            p["verified"]           = merge_field(p["verified"], verified_val)
            p["projects_completed"] = merge_field(p["projects_completed"],
                                                   safe(row.get("projects_completed")))
            p["phone"]     = merge_field(p["phone"],     phone)
            p["city"]      = merge_field(p["city"],      safe(row.get("city_normalized")))
            p["full_name"] = merge_field(p["full_name"], safe(row.get("full_name")))
            p["sources"].add("s3"); register_keys(pid, phone, None); s3_matched += 1
        person_sources_rows.append({"person_id": pid, "source_name": "s3",
                                    "source_row_id": row_idx + 1, "match_key": key})
    print(f"    → {s3_new} new, {s3_matched} matched by phone")

    # ── Write to PostgreSQL ──
    cur = conn.cursor()
    persons_inserted = 0
    for pid, p in persons.items():
        # %s placeholders — psycopg2 syntax (NOT ? like SQLite)
        cur.execute("""
            INSERT INTO persons
              (person_id, full_name, email, phone, city,
               experience_years, ctc_lakhs, applied_date, skills,
               rate_value, rate_unit, status, verified, projects_completed, sources)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            pid, p["full_name"], p["email"], p["phone"], p["city"],
            p["experience_years"], p["ctc_lakhs"], p["applied_date"], p["skills"],
            p["rate_value"], p["rate_unit"], p["status"],
            p["verified"],   # True/False/None — Postgres BOOLEAN handles this natively
            p["projects_completed"],
            ",".join(sorted(p["sources"])),
        ))
        persons_inserted += 1

    for ps in person_sources_rows:
        cur.execute("""
            INSERT INTO person_sources (person_id, source_name, source_row_id, match_key)
            VALUES (%s,%s,%s,%s)
        """, (ps["person_id"], ps["source_name"], ps["source_row_id"], ps["match_key"]))

    conn.commit()
    cur.close()
    return persons_inserted, person_sources_rows


# ─── Summary ──────────────────────────────────────────────────────────────────

def print_summary(conn, persons_count, ps_rows):
    cur = conn.cursor()
    cur.execute("SELECT sources, COUNT(*) FROM persons GROUP BY sources ORDER BY sources")
    source_dist = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM persons WHERE email IS NOT NULL AND phone IS NOT NULL")
    both_keys = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM persons WHERE email IS NULL")
    no_email = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM persons WHERE phone IS NULL")
    no_phone = cur.fetchone()[0]
    cur.close()

    print("\n" + "─"*60)
    print("  MERGE SUMMARY")
    print("─"*60)
    print(f"  Total unique persons   : {persons_count}")
    print(f"  Source rows processed  : {len(ps_rows)}")
    print(f"  With BOTH email+phone  : {both_keys}")
    print(f"  No email (S3-only)     : {no_email}")
    print(f"  No phone (S2-only)     : {no_phone}")
    print()
    print("  Persons by source combination:")
    for sources_val, count in source_dist:
        print(f"    {sources_val:<12} : {count} persons")
    print("─"*60)
    print(f"  Database: Render PostgreSQL\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "█"*60)
    print("  ConsultBae — Step 2: Entity Matching & PostgreSQL Load")
    print("█"*60)

    print("\n  [io] Reading cleaned CSVs...")
    df1 = pd.read_csv(S1_PATH, dtype={"phone_normalized": str})
    df2 = pd.read_csv(S2_PATH)
    df3 = pd.read_csv(S3_PATH, dtype={"phone_normalized": str})
    print(f"       S1={len(df1)} rows, S2={len(df2)} rows, S3={len(df3)} rows")

    print("  [db] Connecting to PostgreSQL...")
    conn   = get_conn()
    engine = get_engine()
    print("       Connected.")

    create_schema(conn)
    load_source_tables(engine, df1, df2, df3)

    persons_count, ps_rows = build_persons(conn, df1, df2, df3)
    print_summary(conn, persons_count, ps_rows)

    conn.close()
    engine.dispose()


if __name__ == "__main__":
    main()
