"""
load_env.py — layered config so you set durable values ONCE.

Loads .env (everything that never changes), then overlays a profile file
(.env.test or .env.deploy) that holds only the few lines that differ.

Pick the profile with the PROFILE env var:
    PROFILE=test   python email_sequencer.py   -> Resend API, sends to yourself
    PROFILE=deploy python email_sequencer.py   -> Resend API, sends to the list

SAFETY: if PROFILE is unset, it defaults to 'test'. A bare
`python email_sequencer.py` can therefore NEVER blast the real list by
accident — the cannon only fires when you explicitly type PROFILE=deploy.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

def load_layered() -> str:
    base = Path(__file__).resolve().parent

    # Load .env files only if they exist. On Railway (and other cloud platforms)
    # env vars are injected directly into the environment — no .env files needed.
    env_file = base / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    profile = os.getenv("PROFILE", "test").lower()  # default = test (safe)
    pfile = base / f".env.{profile}"
    if pfile.exists():
        load_dotenv(pfile, override=True)           # overlay the differing lines

    return profile
