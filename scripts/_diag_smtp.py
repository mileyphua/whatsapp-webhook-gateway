from dotenv import load_dotenv
load_dotenv()

import os, smtplib, ssl

host = os.getenv("SMTP_HOST", "mail.privateemail.com")
user = os.getenv("SMTP_USERNAME", "")
pw = os.getenv("SMTP_PASSWORD", "")
BAD_CHARS = set(['"', "'", '`', ' '])

print(f"== Quick diagnostics ==")
print(f"user        = {user}")
print(f"pw_len      = {len(pw)}")
print(f"pw_has_Qmark = {('?' in pw)}")
print(f"pw_has_bad_chars (quote/space/backtick) = {any(c in BAD_CHARS for c in pw)}")
print()

ctx = ssl.create_default_context()

print("Trying STARTTLS port 587 ...")
try:
    s = smtplib.SMTP(host, 587, timeout=25)
    code_e, resp_e = s.ehlo("petrobindglobal.com")
    print(f"  ehlo code={code_e}")
    code_start, msg_start = s.docmd("STARTTLS")
    print(f"  STARTTLS code={code_start}")
    if code_start == 220:
        s.starttls(context=ctx)
        s.ehlo("petrobindglobal.com")
    code, resp = s.login(user, pw)
    s.quit()
    print(f"  587 LOGIN OK code={code}")
except smtplib.SMTPAuthenticationError as e:
    msg = e.smtp_error.decode() if hasattr(e.smtp_error, "decode") else str(e.smtp_error)
    print(f"  587 AUTH FAIL code={e.smtp_code} msg={msg[:200]}")
except Exception as e:
    print(f"  587 FAIL {type(e).__name__}: {str(e)[:250]}")

print()
print("Trying SSL implicit port 465 (default path)...")
try:
    s = smtplib.SMTP_SSL(host, 465, context=ctx, timeout=25)
    code, resp = s.login(user, pw)
    s.quit()
    print(f"  465 LOGIN OK code={code}")
except smtplib.SMTPAuthenticationError as e:
    msg = e.smtp_error.decode() if hasattr(e.smtp_error, "decode") else str(e.smtp_error)
    print(f"  465 AUTH FAIL code={e.smtp_code} msg={msg[:200]}")
except Exception as e:
    print(f"  465 FAIL {type(e).__name__}: {str(e)[:250]}")
