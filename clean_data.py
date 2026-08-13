"""
clean_data.py — Step 1: Individual file cleaning for ConsultBae merge project.

This script reads three raw CSV files (Naukri applicants, gig workers, CBNexus contacts),
cleans each one independently, and writes the results to /output as:
    source1_clean.csv, source2_clean.csv, source3_clean.csv

What it does per file:
  Source 1 (naukri_applicants): normalizes phone/email/city, fixes CTC units,
    parses 4+ date formats, normalizes skills, deduplicates two known duplicates.
  Source 2 (gig_workers): drops blank row and column-shifted corrupt row,
    normalizes email/city/status, splits rate into value+unit, normalizes skills.
  Source 3 (cbnexus_contacts): removes embedded header row, keeps ambiguous
    Arjun Mehta duplicates but logs a warning, normalizes phone/city/verified/name.

Does NOT:
  - Modify or overwrite original source files.
  - Attempt entity matching/merging across files (that is a later step).
  - Write anything to a database.
"""

import os
import re
import pandas as pd

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(BASE_DIR, "source")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── Shared helpers ───────────────────────────────────────────────────────────

# City synonym map: maps every known variant (lowercased & stripped) → canonical spelling.
# Applied AFTER lowercasing + stripping the raw value so matching is case-insensitive.
CITY_SYNONYMS = {
    "gurgaon":    "Gurgaon",
    "gurugram":   "Gurgaon",   # Official rename; we standardise on the market-familiar name
    "bangalore":  "Bengaluru", # Old romanisation still common in raw data
    "bengaluru":  "Bengaluru",
    "delhi":      "Delhi",
    "new delhi":  "Delhi",
    "delhi ncr":  "Delhi",     # Vague metro label — collapse to Delhi for clean matching
    "noida":      "Noida",
    "pune":       "Pune",
}

def normalize_city(raw: str) -> str:
    """Strip whitespace, lowercase, look up synonym, fall back to title-case."""
    if pd.isna(raw):
        return raw
    stripped = str(raw).strip().lower()
    return CITY_SYNONYMS.get(stripped, str(raw).strip().title())


def normalize_phone(raw: str) -> str:
    """
    Strip every non-digit character (+, -, spaces, parentheses) then keep only
    the last 10 digits.  This handles:
      - bare 10-digit:  9000000254
      - 0-prefixed 11-digit: 09000000254
      - +91-prefixed 12-digit: +919000000254
      - 91-prefixed 12-digit (no plus): 919000000254
      - +91- with dash: +91-9000000131
    We never auto-infer phone columns as int (which would silently strip leading
    zeros / + signs) — always read as str first.
    """
    if pd.isna(raw):
        return raw
    digits_only = re.sub(r"\D", "", str(raw))   # remove all non-digit chars
    return digits_only[-10:] if len(digits_only) >= 10 else digits_only


def normalize_skills(raw: str) -> str:
    """
    Lower-case every individual skill and strip surrounding whitespace, then
    rejoin with ', ' so the column is directly comparable across sources.
    """
    if pd.isna(raw):
        return raw
    skills = [s.strip().lower() for s in str(raw).split(",") if s.strip()]
    return ", ".join(skills)


# ─── Source 1: Naukri Applicants ─────────────────────────────────────────────

def clean_source1(path: str) -> pd.DataFrame:
    """
    Clean source1_naukri_applicants.csv.

    Issues addressed:
      1. Phone read as str to preserve leading zeros / + signs.
      2. Phone normalized to bare 10-digit string.
      3. Email lowercased + whitespace stripped.
      4. City: whitespace strip + synonym normalization.
      5. Current CTC: mixed units (lakhs vs raw rupees) split into ctc_lakhs.
      6. Applied Date: 4+ mixed date formats parsed explicitly to YYYY-MM-DD.
      7. Skills: lowercased and whitespace-stripped per tag.
      8. Duplicate: Nikhil Chopra appears twice (alt. email vs canonical) — keep canonical.
      9. Duplicate: R. Verma == Rohit Verma (same email+phone, abbreviated name) — keep full name.
    """
    print("\n" + "="*60)
    print("SOURCE 1: naukri_applicants")
    print("="*60)

    # Read phone as str — hard requirement; never let pandas cast it to int.
    df = pd.read_csv(path, dtype={"Phone": str})
    rows_in = len(df)
    print(f"  Rows read: {rows_in}")

    # ── Rename columns to snake_case for consistency ──
    df.rename(columns={
        "Full Name":          "full_name",
        "Email":              "email",
        "Phone":              "phone",
        "City":               "city",
        "Experience (Years)": "experience_years",
        "Current CTC":        "ctc_raw",
        "Applied Date":       "applied_date_raw",
        "Skills":             "skills_raw",
    }, inplace=True)

    # ── 1. Email: lowercase + strip ──
    before = df["email"].head(3).tolist()
    df["email"] = df["email"].str.strip().str.lower()
    after = df["email"].head(3).tolist()
    print(f"  [email] Normalized to lowercase+strip. Examples: {list(zip(before, after))}")

    # ── 2. Phone: normalize to 10-digit string ──
    before_phone = df["phone"].head(5).tolist()
    df["phone_normalized"] = df["phone"].apply(normalize_phone)
    after_phone = df["phone_normalized"].head(5).tolist()
    print(f"  [phone] Normalized. Examples: {list(zip(before_phone, after_phone))}")

    # ── 3. City: strip + synonym mapping ──
    before_city = df["city"].unique().tolist()
    df["city_normalized"] = df["city"].apply(normalize_city)
    after_city = df["city_normalized"].unique().tolist()
    print(f"  [city] Before unique values: {sorted(set(str(c) for c in before_city))}")
    print(f"  [city] After  unique values: {sorted(set(str(c) for c in after_city))}")

    # ── 4. CTC: split mixed units into ctc_lakhs ──
    # ASSUMPTION: values < 20 are already in lakhs per annum (e.g. 4.2 LPA, 8.3 LPA).
    # Values >= 20 are treated as raw annual rupees and divided by 100_000 to get lakhs.
    # Threshold of 20 is a heuristic — in practice no real CTC in this dataset falls
    # between 20 and ~300 so the boundary is unambiguous here, but it COULD fail for
    # very low-paid roles (< ₹20k/yr) or very high lakhs (≥ 20 LPA quoted in lakhs).
    # Flag: if this assumption is wrong, only the unit-conversion rows are affected.
    df["ctc_raw"] = pd.to_numeric(df["ctc_raw"], errors="coerce")
    ctc_converted = df[df["ctc_raw"] >= 20]["ctc_raw"].count()
    df["ctc_lakhs"] = df["ctc_raw"].apply(
        lambda v: round(v / 100_000, 4) if pd.notna(v) and v >= 20 else v
    )
    print(f"  [ctc]  {ctc_converted} rows had raw-rupee CTC and were divided by 100,000 to convert to lakhs.")
    print(f"         ASSUMPTION: values <20 treated as already in lakhs; >=20 treated as raw rupees.")

    # ── 5. Applied Date: parse 4 known formats explicitly ──
    # We do NOT use pandas infer_datetime_format=True because it mis-parses ambiguous
    # dates (e.g. 07/13/2026 could be MM/DD or DD/MM; only explicit formats are safe).
    DATE_FORMATS = [
        "%d-%m-%Y",   # 24-07-2026
        "%Y-%m-%d",   # 2026-08-08
        "%d %b %Y",   # 7 Jul 2026  (day without leading zero is also handled by %d)
        "%m/%d/%Y",   # 07/13/2026  (US format; checked AFTER DD-MM to avoid ambiguity)
    ]

    def parse_date(raw):
        if pd.isna(raw):
            return None
        raw = str(raw).strip()
        for fmt in DATE_FORMATS:
            try:
                return pd.to_datetime(raw, format=fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # If nothing matched, log and return None so we don't silently coerce
        print(f"  [date] WARNING: could not parse date value '{raw}' — stored as NaT")
        return None

    df["applied_date"] = df["applied_date_raw"].apply(parse_date)
    failed_dates = df["applied_date"].isna().sum()
    if failed_dates:
        print(f"  [date] {failed_dates} rows failed date parsing.")
    else:
        print(f"  [date] All dates parsed successfully.")

    # ── 6. Skills: lowercase + strip per tag ──
    df["skills_normalized"] = df["skills_raw"].apply(normalize_skills)
    print(f"  [skills] Normalized (lowercased + stripped each tag).")

    # ── 7. Dedup: Nikhil Chopra — alt. email vs canonical email ──
    # Two rows exist for Nikhil Chopra: phone 9000000103, city NOIDA.
    # One uses 'alt.nikhil.chopra70@example.com' and one uses 'nikhil.chopra70@example.com'.
    # Same person with a secondary email address — keep the canonical (non-alt.) version.
    alt_mask = df["email"] == "alt.nikhil.chopra70@example.com"
    alt_row = df[alt_mask]
    if not alt_row.empty:
        print(f"  [dedup] Dropping 1 Nikhil Chopra row with alt. email "
              f"'alt.nikhil.chopra70@example.com' (phone {alt_row['phone_normalized'].values[0]}) "
              f"— keeping canonical email version.")
        df = df[~alt_mask].copy()

    # ── 8. Dedup: R. Verma == Rohit Verma — same email + phone, abbreviated name ──
    # Row 'R. Verma' and 'Rohit Verma' share identical email and phone: same person.
    # Keep full name ('Rohit Verma'), drop abbreviated name row.
    rverma_mask = df["full_name"].str.strip() == "R. Verma"
    rverma_row = df[rverma_mask]
    if not rverma_row.empty:
        print(f"  [dedup] Dropping 'R. Verma' row (email: {rverma_row['email'].values[0]}, "
              f"phone: {rverma_row['phone_normalized'].values[0]}) "
              f"— same person as 'Rohit Verma', abbreviated name kept aside.")
        df = df[~rverma_mask].copy()

    # ── 9. Select and rename final output columns ──
    df_out = df[[
        "full_name", "email", "phone_normalized", "city_normalized",
        "experience_years", "ctc_lakhs", "applied_date", "skills_normalized",
    ]].copy()

    rows_out = len(df_out)
    print(f"\n  Summary: {rows_in} rows in → {rows_out} rows out "
          f"({rows_in - rows_out} duplicates dropped)")
    return df_out


# ─── Source 2: Gig Workers ───────────────────────────────────────────────────

def clean_source2(path: str) -> pd.DataFrame:
    """
    Clean source2_gig_workers.csv.

    Issues addressed:
      1. One fully blank row — dropped.
      2. One column-shifted corrupt row (email_id field contains skill tags, not email)
         — this is a corrupt duplicate of a valid Isha Chopra row, dropped.
      3. Email (email_id) lowercased + whitespace stripped.
      4. Location: same city synonym normalization as Source 1.
      5. Status: normalize to lowercase canonical (active / inactive / paused).
      6. Rate: split 'Xk/month' and 'X/hr' into rate_value (numeric) + rate_unit.
      7. skill_tags: strip whitespace per tag, lowercase already — standardize format.
    """
    print("\n" + "="*60)
    print("SOURCE 2: gig_workers")
    print("="*60)

    # email_id is a phone-equivalent column — read as str to be safe.
    df = pd.read_csv(path, dtype={"email_id": str})
    rows_in = len(df)
    print(f"  Rows read: {rows_in}")

    # ── 1. Drop the fully blank row ──
    # The raw file has a row where all 6 columns are empty (just commas).
    blank_mask = df.isnull().all(axis=1)
    blank_count = blank_mask.sum()
    if blank_count:
        print(f"  [blank] Dropping {blank_count} fully blank row(s).")
        df = df[~blank_mask].copy()

    # ── 2. Drop the column-shifted corrupt row ──
    # In row 20 of the raw file, the data is shifted by one column:
    #   email_id = "react, javascript, mysql"   ← should be skill_tags
    #   worker_name = "ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG"  ← should be email_id
    # We detect corruption by checking: if email_id does NOT contain '@', it is
    # not a real email — the row is corrupt.
    # This is a duplicate of the valid Isha Chopra row (row 7 of raw file),
    # so we safely drop rather than attempt to un-shift.
    shifted_mask = df["email_id"].notna() & ~df["email_id"].str.contains("@", na=False)
    shifted_count = shifted_mask.sum()
    if shifted_count:
        print(f"  [shifted] Dropping {shifted_count} column-shifted corrupt row(s):")
        for _, row in df[shifted_mask].iterrows():
            print(f"    → email_id='{row['email_id']}' (does not contain '@')")
        df = df[~shifted_mask].copy()

    # ── 3. Email: lowercase + strip ──
    before_email = df["email_id"].head(3).tolist()
    df["email"] = df["email_id"].str.strip().str.lower()
    after_email = df["email"].head(3).tolist()
    print(f"  [email] Normalized to lowercase+strip. Examples: {list(zip(before_email, after_email))}")

    # ── 4. Location: strip + synonym mapping ──
    before_loc = df["location"].unique().tolist()
    df["city_normalized"] = df["location"].apply(normalize_city)
    after_loc = df["city_normalized"].unique().tolist()
    print(f"  [location] Before: {sorted(set(str(c) for c in before_loc))}")
    print(f"  [location] After : {sorted(set(str(c) for c in after_loc))}")

    # ── 5. Status: normalize casing only ──
    # Raw values: 'Active', 'ACTIVE', 'active' → 'active'
    #             'Inactive' → 'inactive'
    #             'paused' → 'paused'   (genuinely distinct 3rd state — do NOT merge)
    STATUS_MAP = {
        "active":   "active",
        "inactive": "inactive",
        "paused":   "paused",
    }
    before_status = df["status"].unique().tolist()
    df["status_normalized"] = df["status"].str.strip().str.lower().map(STATUS_MAP)
    unmapped = df["status_normalized"].isna().sum()
    if unmapped:
        print(f"  [status] WARNING: {unmapped} rows have unrecognized status values.")
    after_status = df["status_normalized"].unique().tolist()
    print(f"  [status] Before: {before_status} → After: {after_status}")

    # ── 6. Rate: split into rate_value (numeric) + rate_unit (hourly / monthly) ──
    # Raw format: '1415/hr' (hourly) or '15k/month' (monthly, value in thousands).
    # We do NOT convert hourly→monthly here; that's a later step.
    def parse_rate(raw: str):
        if pd.isna(raw):
            return None, None
        raw = str(raw).strip().lower()
        # Monthly: e.g. '15k/month' → value=15000, unit='monthly'
        m = re.match(r"(\d+(?:\.\d+)?)k/month$", raw)
        if m:
            return float(m.group(1)) * 1000, "monthly"
        # Hourly: e.g. '1415/hr' → value=1415, unit='hourly'
        m = re.match(r"(\d+(?:\.\d+)?)/hr$", raw)
        if m:
            return float(m.group(1)), "hourly"
        print(f"  [rate]  WARNING: unrecognized rate format '{raw}'")
        return None, None

    rate_parsed = df["rate"].apply(parse_rate)
    df["rate_value"] = [v for v, _ in rate_parsed]
    df["rate_unit"]  = [u for _, u in rate_parsed]
    print(f"  [rate]  Split 'rate' into rate_value + rate_unit.")
    print(f"          rate_unit distribution: {df['rate_unit'].value_counts().to_dict()}")

    # ── 7. skill_tags: strip whitespace per tag (already lowercase in this file) ──
    df["skills_normalized"] = df["skill_tags"].apply(normalize_skills)
    print(f"  [skills] Normalized skill_tags (stripped whitespace per tag).")

    # ── Output columns ──
    df_out = df[[
        "email", "worker_name", "rate_value", "rate_unit",
        "city_normalized", "status_normalized", "skills_normalized",
    ]].copy()

    rows_out = len(df_out)
    print(f"\n  Summary: {rows_in} rows in → {rows_out} rows out "
          f"({rows_in - rows_out} rows dropped: {blank_count} blank, {shifted_count} shifted)")
    return df_out


# ─── Source 3: CBNexus Contacts ───────────────────────────────────────────────

def clean_source3(path: str) -> pd.DataFrame:
    """
    Clean source3_cbnexus_contacts.csv.

    Issues addressed:
      1. Embedded header row in the middle of the file — dropped.
      2. 'Arjun Mehta' appears twice with DIFFERENT phone numbers — kept both but logged.
      3. Phone Number: same mixed-format normalization as Source 1.
      4. City: same city synonym normalization.
      5. Verified: 5 raw values (Y/Yes/yes/N/No) → Python bool.
      6. Name: mixed ALL-CAPS + normal case → title-case.
    """
    print("\n" + "="*60)
    print("SOURCE 3: cbnexus_contacts")
    print("="*60)

    # Phone Number must be read as str — see hard requirement #3.
    df = pd.read_csv(path, dtype={"Phone Number": str})
    rows_in = len(df)
    print(f"  Rows read: {rows_in}")

    # ── 1. Drop embedded header row ──
    # The raw file has 'Name,Phone Number,City,Verified,Projects Completed' appearing
    # again as a data row mid-file.  Detect by: Name column value literally equals "Name".
    header_mask = df["Name"].str.strip() == "Name"
    header_count = header_mask.sum()
    if header_count:
        print(f"  [header] Dropping {header_count} embedded header row(s) "
              f"(Name == 'Name' is a literal header, not a real contact).")
        df = df[~header_mask].copy()

    # ── 2. Name: normalize to title-case ──
    # Source has mixed ALL-CAPS (e.g. 'RITU SHARMA') and normal-case names.
    # Title-case is the safe, consistent target.
    before_names = df["Name"].tolist()
    df["full_name"] = df["Name"].str.strip().str.title()
    changed_names = [(b, a) for b, a in zip(before_names, df["full_name"].tolist()) if b != a]
    print(f"  [name]  Title-cased {len(changed_names)} names. Examples: {changed_names[:4]}")

    # ── 3. Phone: normalize to 10-digit string ──
    before_phone = df["Phone Number"].head(5).tolist()
    df["phone_normalized"] = df["Phone Number"].apply(normalize_phone)
    after_phone = df["phone_normalized"].head(5).tolist()
    print(f"  [phone] Normalized. Examples: {list(zip(before_phone, after_phone))}")

    # ── 4. City: strip + synonym mapping ──
    before_city = df["City"].unique().tolist()
    df["city_normalized"] = df["City"].apply(normalize_city)
    after_city = df["city_normalized"].unique().tolist()
    print(f"  [city]  Before: {sorted(set(str(c) for c in before_city))}")
    print(f"  [city]  After : {sorted(set(str(c) for c in after_city))}")

    # ── 5. Verified: map 5 raw values to boolean ──
    # Y, Yes, yes → True; N, No → False.
    VERIFIED_MAP = {
        "y": True, "yes": True,
        "n": False, "no": False,
    }
    before_verified = df["Verified"].unique().tolist()
    df["verified"] = df["Verified"].str.strip().str.lower().map(VERIFIED_MAP)
    unmapped_v = df["verified"].isna().sum()
    if unmapped_v:
        print(f"  [verified] WARNING: {unmapped_v} rows have unrecognized Verified values.")
    after_verified = df["verified"].unique().tolist()
    print(f"  [verified] Before: {before_verified} → After: {after_verified}")

    # ── 6. Flag Arjun Mehta duplicates (DO NOT deduplicate) ──
    # 'Arjun Mehta' appears twice with DIFFERENT phone numbers (9000000131, 9000000272)
    # and different Verified/Projects Completed values.  These COULD be two different people
    # sharing a name, or one person with a data entry error.  We keep both rows and raise a
    # warning so the entity-matching step can resolve this with more context.
    mehta_mask = df["full_name"].str.strip() == "Arjun Mehta"
    mehta_rows = df[mehta_mask]
    if len(mehta_rows) > 1:
        phones = mehta_rows["phone_normalized"].tolist()
        print(f"\n  ⚠️  WARNING — name collision: 'Arjun Mehta' appears {len(mehta_rows)} times "
              f"with DIFFERENT phone numbers: {phones}.")
        print(f"     Keeping both rows — manual review required in the entity-matching step.")

    # ── 7. Select final output columns ──
    df_out = df[[
        "full_name", "phone_normalized", "city_normalized",
        "verified", "Projects Completed",
    ]].rename(columns={"Projects Completed": "projects_completed"}).copy()

    rows_out = len(df_out)
    dropped = rows_in - rows_out
    print(f"\n  Summary: {rows_in} rows in → {rows_out} rows out "
          f"({dropped} embedded header row(s) dropped)")
    return df_out


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "█"*60)
    print("  ConsultBae — Step 1: Data Cleaning")
    print("█"*60)

    # ── Run cleaning ──
    df1 = clean_source1(os.path.join(SOURCE_DIR, "source1_naukri_applicants.csv"))
    df2 = clean_source2(os.path.join(SOURCE_DIR, "source2_gig_workers.csv"))
    df3 = clean_source3(os.path.join(SOURCE_DIR, "source3_cbnexus_contacts.csv"))

    # ── Write outputs (never overwrite originals) ──
    out1 = os.path.join(OUTPUT_DIR, "source1_clean.csv")
    out2 = os.path.join(OUTPUT_DIR, "source2_clean.csv")
    out3 = os.path.join(OUTPUT_DIR, "source3_clean.csv")

    df1.to_csv(out1, index=False)
    df2.to_csv(out2, index=False)
    df3.to_csv(out3, index=False)

    # ── Final summary table ──
    print("\n" + "─"*60)
    print("  FINAL SUMMARY")
    print("─"*60)
    # Recount from originals vs cleaned to give accurate row-in numbers
    raw_counts = {
        "source1": len(pd.read_csv(os.path.join(SOURCE_DIR, "source1_naukri_applicants.csv"), dtype={"Phone": str})),
        "source2": len(pd.read_csv(os.path.join(SOURCE_DIR, "source2_gig_workers.csv"), dtype={"email_id": str})),
        "source3": len(pd.read_csv(os.path.join(SOURCE_DIR, "source3_cbnexus_contacts.csv"), dtype={"Phone Number": str})),
    }
    rows = [
        ("source1_naukri_applicants.csv", raw_counts["source1"], len(df1),
         "2 duplicates dropped (Nikhil Chopra alt-email, R. Verma abbreviated name)"),
        ("source2_gig_workers.csv",       raw_counts["source2"], len(df2),
         "2 rows dropped (1 blank, 1 column-shifted corrupt duplicate)"),
        ("source3_cbnexus_contacts.csv",  raw_counts["source3"], len(df3),
         "1 embedded header row dropped; Arjun Mehta collision flagged for review"),
    ]
    for fname, r_in, r_out, note in rows:
        print(f"  {fname:<38} : {r_in:3d} rows in → {r_out:3d} rows out  ({note})")

    print("\n  Output files written to:", OUTPUT_DIR)
    print("─"*60 + "\n")


if __name__ == "__main__":
    main()
