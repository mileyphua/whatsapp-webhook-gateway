import os, sys
# allow scripts/ runner to resolve siblings without pip install
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

import asyncio
import notify

async def main():
    print("Sending a Petrobind-styled handoff test email ...")
    ok = await notify.send_handoff_email(
        phone_number="+6512345678",
        reason="[LOCAL SMTP VERIFICATION TEST] trigger from local dev — confirming email rendering, From/To headers, and Petrobind HTML template deliver OK.",
        partial_inquiry_summary=(
            "Buyer name: Milly Phua (LOCAL TEST)\n"
            "Product of interest: Bitumen 60/70 penetration\n"
            "Target quantity: ~50 MT / drums\n"
            "Destination port: Singapore\n"
            "Incoterms: CFR\n"
            "Notes: Please confirm — did this test handoff email land? Is the Petrobind HTML styling clean?"
        ),
    )
    print(f"\nsend_handoff_email returned: ok={ok}")
    if not ok:
        print("FAIL: notify.send_handoff_email returned False. Review logs above. If AUTH FAIL code=535, re-run scripts/_verify_env_smtp.py.")
    else:
        print("OK: send_handoff_email succeeded. Check milly.phua inbox in ~1 minute.")

asyncio.run(main())
