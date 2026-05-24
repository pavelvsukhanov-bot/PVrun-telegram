"""
Run this ONCE locally to get the token string for GitHub Secret.

Usage:
    python setup_tokens.py
"""
from pathlib import Path
from dotenv import load_dotenv
from garmin_sync import login

load_dotenv()

TOKEN_DIR  = Path(__file__).parent / "garmin_tokens"
TOKEN_FILE = TOKEN_DIR / "garmin_tokens.json"
TOKEN_DIR.mkdir(exist_ok=True)

print("Logging in to Garmin (MFA code will be requested if needed)...")
login(tokenstore=str(TOKEN_DIR))
print("Login OK.\n")

if not TOKEN_FILE.exists():
    print("ERROR: token file not created. Try again.")
else:
    token_value = TOKEN_FILE.read_text(encoding="utf-8").strip()
    print("=" * 60)
    print("Add this as GitHub Secret named  GARMINTOKENS:")
    print("=" * 60)
    print(token_value)
    print("=" * 60)
