"""
build_segment.py — turn the 1.2M-row FL bulk file into a clean, targeted,
deduped 'Pending' segment that the sequencer can send.

Pipeline: read (chunked) -> strip Excel ="..." guards -> filter by line of
authority + recency + residency + status -> dedupe by person -> (optional) drop
dead email domains via MX check -> write data/leads_segment.csv in the
rookie_list.csv schema.

Edit the CONFIG block, then:  python build_segment.py path/to/AllValidLicensesIndividual.csv
"""
from __future__ import annotations
import re, sys
from pathlib import Path
import pandas as pd

# ===================== CONFIG — edit these knobs =====================
# Line of authority: keep rows whose 'License TYCL Desc' contains ANY of these
# (case-insensitive). "LIFE" catches Life, Life & Health, Life+Variable Annuity.
LOA_KEYWORDS = ["LIFE"]

# Recency: only keep licenses issued within this many months. None = no limit.
MONTHS_BACK = 12

# Residency: subset of {"Resident", "Non-Resident"}. Both = national reach.
RESIDENCY = {"Resident", "Non-Resident"}

# License status to keep.
STATUS_KEEP = {"VALID"}

# Drop emails whose domain has no MX record (cheap dead-domain filter).
# Requires: pip install dnspython. If unavailable, it's skipped automatically.
DO_MX_CHECK = True

OUTPUT = "data/leads_segment.csv"
# ====================================================================

GUARD = re.compile(r'^="?(.*?)"?$')

def unguard(v: str) -> str:
    """Strip Excel text-guard: ="2394037420" -> 2394037420."""
    v = (v or "").strip()
    m = GUARD.match(v)
    return m.group(1) if m else v

def norm_phone(v: str) -> str:
    d = re.sub(r"\D", "", unguard(v))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"+1{d}" if len(d) == 10 else ""

def title(v: str) -> str:
    return (v or "").strip().title()

# --- column names from the FL file ---
C_FIRST, C_LAST = "First Name", "Last Name"
C_EMAIL, C_PHONE = "Email Address", "Business Phone"
C_DESC, C_STATUS = "License TYCL Desc", "License Status"
C_RES, C_DATE = "Residency Type", "License Issue Date"
C_NPN, C_LICNO = "NPN Number", "License Number"
C_CITY, C_COUNTY = "Business City", "Business County"


def matches_loa(desc: str) -> bool:
    d = (desc or "").upper()
    return any(k.upper() in d for k in LOA_KEYWORDS)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/AllValidLicensesIndividual.csv"
    cutoff = (pd.Timestamp.now() - pd.DateOffset(months=MONTHS_BACK)) if MONTHS_BACK else None

    kept = {}          # person_key -> output row (dedupe)
    seen_rows = matched = 0

    for chunk in pd.read_csv(path, dtype=str, chunksize=100_000).__iter__():
        chunk = chunk.fillna("")
        seen_rows += len(chunk)

        # filter: status
        c = chunk[chunk[C_STATUS].str.strip().str.upper().isin({s.upper() for s in STATUS_KEEP})]
        # filter: residency
        c = c[c[C_RES].str.strip().isin(RESIDENCY)]
        # filter: line of authority
        c = c[c[C_DESC].map(matches_loa)]
        if c.empty:
            continue

        # filter: recency
        if cutoff is not None:
            d = pd.to_datetime(c[C_DATE], format="mixed", errors="coerce")
            c = c[d >= cutoff]
        if c.empty:
            continue

        for _, r in c.iterrows():
            email = unguard(r[C_EMAIL]).strip().lower()
            if "@" not in email:
                continue
            key = unguard(r[C_NPN]) or unguard(r[C_LICNO]) or email
            if key in kept:
                continue
            matched += 1
            kept[key] = {
                "First Name": title(r[C_FIRST]),
                "Last Name": title(r[C_LAST]),
                "Email": email,
                "Business Phone": norm_phone(r[C_PHONE]),
                "NPN": unguard(r[C_NPN]),
                "License Desc": r[C_DESC].strip(),
                "Business City": title(r[C_CITY]),
                "Business County": title(r[C_COUNTY]),
                "License Issue Date": r[C_DATE].strip(),
                "Status": "Pending",
            }

    out = pd.DataFrame(list(kept.values()))
    print(f"Scanned {seen_rows:,} rows -> {matched:,} matched -> {len(out):,} unique people.")

    if DO_MX_CHECK and not out.empty:
        out = mx_filter(out)

    Path(OUTPUT).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUTPUT, index=False)
    print(f"Wrote {len(out):,} leads -> {OUTPUT}")
    print("Point the sequencer at it:  set LEADS_CSV=" + OUTPUT + "  in .env  (or copy over rookie_list.csv)")


def mx_filter(df: pd.DataFrame) -> pd.DataFrame:
    try:
        import dns.resolver
    except ImportError:
        print("dnspython not installed; skipping MX check (pip install dnspython to enable).")
        return df
    cache: dict[str, bool] = {}
    def has_mx(email: str) -> bool:
        dom = email.rsplit("@", 1)[-1]
        if dom not in cache:
            try:
                cache[dom] = bool(dns.resolver.resolve(dom, "MX", lifetime=5))
            except Exception:
                cache[dom] = False
        return cache[dom]
    before = len(df)
    mask = df["Email"].map(has_mx)
    df = df[mask]
    print(f"MX check: dropped {before - len(df):,} dead-domain emails "
          f"({len(cache):,} unique domains checked).")
    return df


if __name__ == "__main__":
    main()
