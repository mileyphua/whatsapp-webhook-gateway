from dotenv import load_dotenv
load_dotenv()

import os, smtplib, ssl

host = os.getenv("SMTP_HOST", "mail.privateemail.com")
port = int(os.getenv("SMTP_PORT", "465"))
user = os.getenv("SMTP_USERNAME", "")
pw = os.getenv("SMTP_PASSWORD", "")
from_a = os.getenv("NOTIFY_FROM_EMAIL", "")
to_a = os.getenv("SALES_EMAIL", "")

print(f"=== SMTP verify ===")
print(f"host={host}:{port}")
print(f"user={user}  password_set={bool(pw)}")
print(f"NOTIFY_FROM_EMAIL = {from_a!r}")
print(f"SALES_EMAIL       = {to_a!r}")

if not (user and pw):
    print("FAIL — SMTP_USERNAME and/or SMTP_PASSWORD missing in .env")
    raise SystemExit(2)

ctx = ssl.create_default_context()
try:
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=25)
    else:
        s = smtplib.SMTP(host, port, timeout=25)
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
    code, resp = s.login(user, pw)
    s.quit()
    label = "SSL 465" if port == 465 else f"STARTTLS {port}"
    print(f"SMTP OK ({label}) login code={code}")
except smtplib.SMTPAuthenticationError as e:
    msg = e.smtp_error.decode() if hasattr(e.smtp_error, "decode") else str(e.smtp_error)
    print(f"AUTH FAIL code={e.smtp_code} msg={msg}")
    print("FIX for Spacemail (password set at https://www.spacemail.com/ → Mailboxes → Change Password):")
    print("  (1) SMTP_USERNAME must be FULL email e.g. sales@petrobindglobal.com not 'sales'.")
    print("  (2) SMTP_PASSWORD = Spacemail mailbox password (NOT Namecheap billing/account pw).")
    print("  (3) Spacemail does NOT use 'App Passwords' — use the real mailbox password.")
    print("  (4) ⚠️  NON-ALPHANUMERIC SYMBOLS IN PASSWORD = SILENT 535 REJECTION EVEN IF WEBMAIL WORKS.")
    print("      Most common cause: your password contains '?', '!', '@', '#', '$', '%', '&', '*', etc.")
    print("      FIX: Reset the mailbox password to ONLY A-Z a-z 0-9 characters (16+ chars, no symbols).")
    print("  (5) If mailbox brand new: log in via https://www.spacemail.com/ webmail ONCE to activate.")
    print("  (6) After pw change: wait 2-3 minutes (Spacemail replicates slowly) then retry.")
except Exception as e:
    print(f"FAIL {type(e).__name__}: {e}")

print()
print("=== Missing .env vars for go-live ===")
MUST_HAVE = [
    "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_API_VERSION",
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    "SMTP_HOST_PROVIDER", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
    "NOTIFY_FROM_EMAIL", "SALES_EMAIL",
]
for k in MUST_HAVE:
    v = os.getenv(k)
    mark = "OK" if v else "MISSING ❌"
    print(f"  {k:32s} -> {mark}")

OPTIONAL_IMPT = [
    "OPENAI_API_KEY",          # embeddings only (ok if index already built)
    "CAL_COM_BOOKING_LINK",    # assistant drops this; without it tool returns empty string
    "CAL_COM_API_KEY",         # optional; not used today
    "CAL_COM_WEBHOOK_SECRET",  # optional; blank = skip signature check (MVP OK)
    "FOLLOWUPS_CRON_TOKEN",    # optional; blank = warn each cron call (not blocking)
    "FOLLOWUPS_MAX_NUDGES_PER_RUN",
]
for k in OPTIONAL_IMPT:
    v = os.getenv(k)
    mark = "SET" if v else "UNSET — see GAP_ANALYSIS.md"
    print(f"  {k:32s} -> {mark}")
