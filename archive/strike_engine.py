import os
import time

import pandas as pd
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
CALENDLY_LINK = os.getenv("CALENDLY_LINK", "[Your Calendly Link]")

FILE_PATH = "rookie_strike_list_clean.csv"
BATCH_SIZE = 100
STRIKE_2_DELAY_SECONDS = 1800  # 30 minutes


def format_phone(raw_phone: str) -> str:
    digits = "".join(c for c in str(raw_phone) if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ValueError(f"Invalid phone number: {raw_phone}")


missing = [
    name
    for name, value in {
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_PHONE_NUMBER": TWILIO_PHONE_NUMBER,
    }.items()
    if not value
]
if missing:
    raise SystemExit(f"Missing required env vars in .env: {', '.join(missing)}")

print("Booting local sequencer...")
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
df = pd.read_csv(FILE_PATH)

if "Status" not in df.columns:
    df["Status"] = "Pending"

df_pending = df[df["Status"] == "Pending"].head(BATCH_SIZE)

if df_pending.empty:
    print("Zero pending targets found. The CSV is fully extracted.")
    raise SystemExit(0)

print(f"Locked in {len(df_pending)} targets. Executing Strike 1 (The Hook)...")

# PHASE 1: FIRE THE HOOK
for index, row in df_pending.iterrows():
    target_phone = format_phone(row["Business Phone"])
    try:
        message = client.messages.create(
            body=(
                f"hey {row['First Name']}, saw you got your life license a few months back. "
                "assuming you're stuck at a captive agency on a 50% split. you open to looking "
                "at the math on an open-architecture model?"
            ),
            from_=TWILIO_PHONE_NUMBER,
            to=target_phone,
        )
        print(f"Hook fired at: {row['First Name']} (SID: {message.sid})")
    except Exception as e:
        print(f"Network error on {row['First Name']}: {e}")
    time.sleep(1)

# PHASE 2: THE MAC SLEEP CYCLE
print("\nStrike 1 Complete. Initiating 30-minute compression zone...")
time.sleep(STRIKE_2_DELAY_SECONDS)

print("\nCompression complete. Executing Strike 2 (The Pitch)...")

# PHASE 3: FIRE THE PITCH & LINK
for index, row in df_pending.iterrows():
    target_phone = format_phone(row["Business Phone"])
    try:
        client.messages.create(
            body=(
                "following up. we start operators at 60%, scale to 90% on volume, and offer "
                f"up to 40% in passive overrides to build your own agency. grab 15 mins for an "
                f"operational audit here: {CALENDLY_LINK}"
            ),
            from_=TWILIO_PHONE_NUMBER,
            to=target_phone,
        )
        print(f"Link dropped for: {row['First Name']}")
        df.at[index, "Status"] = "Completed"
    except Exception as e:
        print(f"Network error on {row['First Name']}: {e}")
    time.sleep(1)

# PHASE 4: UPDATE THE DATABASE
df.to_csv(FILE_PATH, index=False)
print("\nStrike 2 Complete. Database updated. Engine shutting down.")
