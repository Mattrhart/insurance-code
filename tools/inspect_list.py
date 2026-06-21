"""
inspect_list.py — tell me exactly what's in the FL licensee CSV.

Handles million-row files by streaming in chunks (won't blow up memory).
Reports: every column, row count, per-column fill rate, and — most importantly —
how many rows actually carry a usable EMAIL, plus phone fill, date range, and the
top license types. Run:  python inspect_list.py path/to/your.csv
"""
from __future__ import annotations
import sys, re
from collections import Counter
import pandas as pd

PATH = sys.argv[1] if len(sys.argv) > 1 else "data/all_valid_individual.csv"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def looks_email(v: str) -> bool:
    return bool(EMAIL_RE.match((v or "").strip()))

def digits(v: str) -> str:
    return re.sub(r"\D", "", v or "")

# First pass: get header + a couple sample rows cheaply
head = pd.read_csv(PATH, dtype=str, nrows=5).fillna("")
cols = list(head.columns)
print("="*70)
print(f"FILE: {PATH}")
print(f"COLUMNS ({len(cols)}):")
for c in cols:
    print(f"   - {c}")

# Auto-detect columns of interest by fuzzy name match
def find(*subs):
    for c in cols:
        lc = c.lower()
        if any(s in lc for s in subs):
            return c
    return None

email_col = find("email", "e-mail")
phone_col = find("phone", "telephone", "mobile", "cell")
date_col  = find("issue", "effective", "orig", "license date", "date")
type_col  = find("class", "license type", "lictype", "loa", "type")
status_col= find("status")
county_col= find("county")
city_col  = find("city")

print("\nDETECTED KEY COLUMNS:")
for label, c in [("email", email_col), ("phone", phone_col), ("date", date_col),
                 ("license type", type_col), ("status", status_col),
                 ("county", county_col), ("city", city_col)]:
    print(f"   {label:14}: {c or '(none found)'}")

# Streaming pass for fill rates + email/phone validity + type counts
total = 0
nonnull = Counter()
email_ok = 0
phone_ok = 0
type_counts = Counter()
dates = []

for chunk in pd.read_csv(PATH, dtype=str, chunksize=100_000):
    chunk = chunk.fillna("")
    total += len(chunk)
    for c in cols:
        nonnull[c] += (chunk[c].str.strip() != "").sum()
    if email_col:
        email_ok += chunk[email_col].map(looks_email).sum()
    if phone_col:
        phone_ok += (chunk[phone_col].map(lambda v: len(digits(v)) >= 10)).sum()
    if type_col:
        type_counts.update(chunk[type_col].str.strip().replace("", "(blank)"))
    if date_col:
        dates.append(pd.to_datetime(chunk[date_col], errors="coerce"))

print("\n" + "="*70)
print(f"TOTAL ROWS: {total:,}")
print("\nFILL RATE PER COLUMN:")
for c in cols:
    pct = 100 * nonnull[c] / total if total else 0
    print(f"   {c:30} {pct:6.1f}%  ({nonnull[c]:,})")

print("\n" + "-"*70)
if email_col:
    print(f"USABLE EMAILS (valid format): {email_ok:,}  "
          f"({100*email_ok/total:.1f}% of rows)   <-- THE NUMBER THAT MATTERS")
else:
    print("NO EMAIL COLUMN FOUND -> enrichment required for every lead.")
if phone_col:
    print(f"USABLE PHONES (10+ digits):   {phone_ok:,}  ({100*phone_ok/total:.1f}%)")

if date_col and dates:
    alld = pd.concat(dates)
    valid = alld.dropna()
    if len(valid):
        cutoff6  = pd.Timestamp.now() - pd.DateOffset(months=6)
        cutoff12 = pd.Timestamp.now() - pd.DateOffset(months=12)
        print(f"\nDATE COLUMN '{date_col}': {valid.min().date()} -> {valid.max().date()}")
        print(f"   licensed in last  6 months: {(valid>=cutoff6).sum():,}")
        print(f"   licensed in last 12 months: {(valid>=cutoff12).sum():,}")

if type_col:
    print(f"\nTOP LICENSE TYPES ('{type_col}'):")
    for val, n in type_counts.most_common(12):
        print(f"   {val:30} {n:,}")

print("\nSAMPLE ROW:")
for c in cols:
    print(f"   {c:30} = {head.iloc[0][c]!r}")
print("="*70)
