"""Read-only client for the Tredict Personal API (https://www.tredict.com/skills/setup.md)."""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.tredict.com/api/oauth/v2/"

# Exit code for "token rejected". Tredict deactivates personal tokens after
# 2 weeks without a visit to the web UI; CI uses this code to say so.
EXIT_TREDICT_AUTH = 3


def get(path: str, **params) -> dict:
    token = os.environ.get("TREDICT_TOKEN")
    if not token:
        sys.exit("Set TREDICT_TOKEN in .env")
    resp = requests.get(
        BASE_URL + path,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        print(f"TREDICT AUTH ERROR: {resp.status_code} on {path}", file=sys.stderr)
        sys.exit(EXIT_TREDICT_AUTH)
    resp.raise_for_status()
    return resp.json()
