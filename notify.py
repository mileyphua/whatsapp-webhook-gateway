"""SMTP email notifications for sales leads / handoffs / booking confirmations.

Wired to llm_assistant.py's tool-calling loop: when the model chooses
capture_trade_inquiry or request_sales_handoff, we call into send_lead_email /
send_handoff_email; when Cal.com booking fires via /cal-webhook, send_booking_email.

Provider support (tested with configs below):
  - **Spacemail** by Namecheap (formerly "Private Email"):
      Mailbox admin portal:  https://www.spacemail.com/
      SMTP_HOST=mail.spacemail.com   (confirmed 2026-09-21 — the older
        mail.privateemail.com hostname returns 535 auth failures even with
        correct credentials; the portal's own SMTP settings page lists
        mail.spacemail.com)
      SMTP_PORT=465  (implicit TLS/SSL — RECOMMENDED)
      SMTP_PORT=587  (STARTTLS — also works)
      SMTP_USERNAME=sales@petrobindglobal.com  (FULL email, not just local part)
      SMTP_PASSWORD=<Spacemail portal → Mailboxes → Change Password>
        NOTE: This is the MAILBOX password, NOT your Namecheap billing password,
        and NOT a Gmail-style "App Password" (Spacemail does not use App Passwords).
  - Google Workspace / Gmail (with App Password):
      SMTP_HOST=smtp.gmail.com, SMTP_PORT=587
  - Any standard SMTP: set SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD.

To verify settings WITHOUT sending a real email, run from a shell:
  python3 - <<'PY'
  import smtplib, ssl, os
  from dotenv import load_dotenv; load_dotenv()
  host, port = os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])
  user, pw = os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"]
  ctx = ssl.create_default_context()
  if port == 465:
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
      s.login(user, pw); print("SMTP OK (SSL 465)")
  else:
    with smtplib.SMTP(host, port, timeout=20) as s:
      s.starttls(context=ctx); s.login(user, pw); print(f"SMTP OK (STARTTLS {port})")
  PY

Implementation notes:
  - Blocking smtplib wrapped in asyncio.to_thread so it doesn't hold the event loop.
  - All exceptions are caught and logged — never raise into the caller (webhook 200
    guarantee must be preserved even if SMTP is down).
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, Mapping, Optional

ENV_PREFIX = ""  # placeholder for future namespacing


@dataclass
class _SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    from_addr: str
    to_addr: str

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password and self.from_addr and self.to_addr)


def _load_config() -> _SMTPConfig:
    # Defaults tuned to Namecheap Private Email ("Spacemail"). Their docs:
    # https://www.namecheap.com/support/knowledgebase/article.aspx/9447/2175/private-email-nc-email-plans-general-settings-explained/
    # For Gmail/Google Workspace use SMTP_HOST=smtp.gmail.com SMTP_PORT=587.
    host_default = os.getenv("SMTP_HOST_PROVIDER", "spacemail").lower()
    if host_default == "gmail":
        default_host, default_port = "smtp.gmail.com", 587
    else:  # default: spacemail / privateemail
        default_host, default_port = "mail.spacemail.com", 465
    return _SMTPConfig(
        host=os.getenv("SMTP_HOST", default_host),
        port=int(os.getenv("SMTP_PORT", str(default_port))),
        username=os.getenv("SMTP_USERNAME", ""),
        password=os.getenv("SMTP_PASSWORD", ""),
        from_addr=os.getenv("NOTIFY_FROM_EMAIL", os.getenv("SMTP_USERNAME", "")),
        to_addr=os.getenv("SALES_EMAIL", ""),
    )


def _build_message(
    cfg: _SMTPConfig,
    *,
    subject: str,
    body_html: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to_addr
    msg["Reply-To"] = cfg.from_addr
    msg.set_content("")
    msg.add_alternative(body_html, subtype="html")
    return msg


_FIRST_CONFIG_LOGGED = False


def _ehlo_hostname(cfg: "_SMTPConfig") -> str:
    """Return a FQDN-style EHLO/HELO identity for the SMTP client.

    Spacemail / PrivateEmail occasionally drops the connection right after
    STARTTLS before AUTH when the client sends a generic `localhost` HELO
    (we observed this on port 587: ehlo=250, STARTTLS=220, then immediate
    SMTPServerDisconnected on the cipher-negotiated TLS socket). Using the
    mailbox domain as the HELO identity avoids that heuristic ban.
    """
    user = cfg.username or ""
    if "@" in user:
        return user.split("@", 1)[1].lower() or "petrobindglobal.com"
    # Fallback: derive from cfg.host if it is mail.<domain>.*
    host_parts = cfg.host.lstrip("mail.").lstrip("smtp.").split(".")
    if len(host_parts) >= 2:
        return ".".join(host_parts[-2:]).lower()
    return "petrobindglobal.com"


def _send_sync(cfg: _SMTPConfig, msg: EmailMessage) -> None:
    """Blocking SMTP send — always wrapped in to_thread by public APIs.

    Transport selection (keeps the same code working for all providers):
      - Port 465   → SMTP_SSL (implicit TLS on connect — Namecheap Spacemail default)
      - Port 25    → plain SMTP + STARTTLS when advertised (legacy, not recommended)
      - All others (587, 2525, 2587, …) → SMTP + STARTTLS upgrade
    If STARTTLS path fails with a server-disconnect before AUTH (observed on
    Spacemail port 587), we transparently retry once on port 465 with implicit
    TLS. This keeps the user-facing config single-entry (no need to fiddle with
    port numbers if one listener is having a bad day).
    """
    global _FIRST_CONFIG_LOGGED
    if not _FIRST_CONFIG_LOGGED:
        _FIRST_CONFIG_LOGGED = True
        print(
            f"[notify] SMTP config preview: host={cfg.host!r} port={cfg.port} "
            f"username={cfg.username!r} from={cfg.from_addr!r} to={cfg.to_addr!r} "
            f"(password is set? {bool(cfg.password)}) — logging ONCE per process"
        )
    ctx = ssl.create_default_context()
    ehlo = _ehlo_hostname(cfg)
    # Spacemail/PrivateEmail uses LetsEncrypt-issued certs (all trusted by macOS,
    # Debian/RHEL ca-certificates, Render base image). If you run into
    # CERTIFICATE_VERIFY_FAILED in a stripped container: install ca-certificates.
    def _try_send_via_ssl465() -> None:
        with smtplib.SMTP_SSL(
            cfg.host, 465, context=ctx, timeout=30, local_hostname=ehlo
        ) as s:
            s.login(cfg.username, cfg.password)
            s.send_message(msg)

    try:
        if cfg.port == 465:
            _try_send_via_ssl465()
            return
        # STARTTLS path (ports 587 / 2525 / etc). Some providers require EHLO
        # twice (once before STARTTLS, once after). smtplib does this implicitly
        # on login() if needed, but we explicitly starttls to keep it obvious.
        try:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30, local_hostname=ehlo) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls(context=ctx)
                    s.ehlo()
                s.login(cfg.username, cfg.password)
                s.send_message(msg)
            return
        except (
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            TimeoutError,
            ConnectionResetError,
        ):
            # Spacemail 587 listener has been observed dropping the socket
            # immediately after STARTTLS handshakes on certain IP ranges.
            # Retry once on 465 SSL before failing so the user doesn't need
            # to manually flip the port.
            if cfg.port != 465:
                print(
                    f"[notify] STARTTLS on {cfg.host}:{cfg.port} disconnected "
                    f"pre-AUTH; retrying via SSL 465 as fallback."
                )
                _try_send_via_ssl465()
                return
            raise
    except smtplib.SMTPAuthenticationError as exc:
        code, byts = getattr(exc, "smtp_code", None), getattr(exc, "smtp_error", None)
        msg_text = str(byts) if byts else str(exc)
        raise RuntimeError(
            f"SMTP auth failed (host={cfg.host} port={cfg.port} user={cfg.username!r}). "
            f"For Spacemail: (1) SMTP_HOST must be mail.spacemail.com — the older "
            f"mail.privateemail.com hostname returns 535 even with correct "
            f"credentials (confirmed 2026-09-21: same user/password authenticated "
            f"fine against mail.spacemail.com on both 465 and 587, and failed "
            f"every time against mail.privateemail.com). (2) SMTP_USERNAME must be "
            f"the FULL email address (sales@petrobindglobal.com, not just 'sales'). "
            f"(3) SMTP_PASSWORD is the mailbox password set via "
            f"https://www.spacemail.com/ → Mailboxes → (your mailbox) → Change "
            f"Password (NOT your Namecheap billing/account password; Spacemail "
            f"does not use 'App Passwords'). (4) If mailbox is brand new, log in "
            f"via https://www.spacemail.com/ webmail ONCE to finish activation. "
            f"(5) After a password change, wait 2-3 minutes for replication then "
            f"retry. Detail: code={code} server={msg_text}"
        ) from exc
    except ssl.SSLCertVerificationError as exc:
        raise RuntimeError(
            f"SSL cert verify failed talking to {cfg.host}:{cfg.port}. "
            f"Install ca-certificates on the host; if self-signed, consider port 587 "
            f"(STARTTLS) or set env SMTP_HOST / SMTP_PORT to alternate endpoints."
        ) from exc


def _fmt_rows(fields: Mapping[str, Any]) -> str:
    rows = []
    for k, v in fields.items():
        if v in (None, "", []):
            continue
        label = " ".join(p.capitalize() for p in str(k).replace("_", " ").split())
        rows.append(
            f"<tr><td style='padding:8px 12px;border-bottom:1px solid #eee;"
            f"font-weight:600;width:180px'>{label}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>{v}</td></tr>"
        )
    return "".join(rows)


def _format_snapshot_html(
    relationship_summary: Optional[str],
    recent_transcript: Optional[str],
) -> str:
    """Quick-read block for the sales team: how warm this contact is, and
    what was actually said, without needing to open WhatsApp first."""
    parts = []
    if relationship_summary:
        parts.append(
            f"<div style='display:inline-block;margin:0 0 16px;padding:6px 12px;"
            f"background:#e8f0fe;color:#1a56db;border-radius:14px;font-size:13px;"
            f"font-weight:600'>{relationship_summary}</div>"
        )
    if recent_transcript:
        escaped = (
            recent_transcript.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        parts.append(
            "<div style='margin:4px 0 20px'>"
            "<div style='font-weight:600;margin-bottom:6px;font-size:13px;color:#333'>"
            "Recent conversation</div>"
            f"<pre style='white-space:pre-wrap;font-family:-apple-system,BlinkMacSystemFont,"
            f"\"Segoe UI\",Roboto,Arial;font-size:13px;line-height:1.5;background:#fff;"
            f"border:1px solid #eee;border-radius:6px;padding:12px;margin:0;color:#222'>"
            f"{escaped}</pre></div>"
        )
    return "".join(parts)


def _wrap_html(title: str, lead_meta: str, table: str, extra: str = "") -> str:
    return f"""
    <div style='font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial;
         max-width:640px;margin:24px auto;color:#111'>
      <h2 style='margin:0 0 4px'>{title}</h2>
      <p style='margin:0 0 12px;color:#555'>{lead_meta}</p>
      {extra}
      <table style='width:100%;border-collapse:collapse;background:#fafafa;border-radius:6px'>
        {table}
      </table>
      <p style='margin-top:24px;color:#888;font-size:13px'>
        Sent at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} —
        Petrobind Global WhatsApp Assistant
      </p>
    </div>
    """


async def _send_if_configured(
    *,
    subject: str,
    html_body: str,
) -> bool:
    # NOTIFY_DRY_RUN=1 prints the email instead of sending it — use this for any
    # local/manual test run (curl scripts, python -c smoke tests, persona
    # testing, etc.) so test traffic never lands in the real sales inbox.
    # Set it in your local shell / .env, NOT on Render, where real emails
    # must actually go out.
    if os.getenv("NOTIFY_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        print(f"[notify] DRY RUN (NOTIFY_DRY_RUN set) — would have sent: {subject!r}")
        return True
    cfg = _load_config()
    if not cfg.is_configured:
        print(
            f"[notify] SKIP email (SMTP not configured) — subject={subject!r}. "
            "Set SMTP_USERNAME/SMTP_PASSWORD/NOTIFY_FROM_EMAIL/SALES_EMAIL env vars."
        )
        return False
    msg = _build_message(cfg, subject=subject, body_html=html_body)
    try:
        await asyncio.to_thread(_send_sync, cfg, msg)
        print(f"[notify] OK email sent: {subject!r} to {cfg.to_addr}")
        return True
    except Exception as exc:  # pragma: no cover - transient network
        print(f"[notify] FAIL email {subject!r}: {exc!r}")
        return False


# ------------------------- Public API -------------------------

async def send_lead_email(
    *,
    phone_number: str,
    company_name: Optional[str] = None,
    contact_name: Optional[str] = None,
    contact_position: Optional[str] = None,
    contact_email: Optional[str] = None,
    product: Optional[str] = None,
    quantity: Optional[str] = None,
    destination_port: Optional[str] = None,
    incoterm: Optional[str] = None,
    packaging: Optional[str] = None,
    additional_notes: Optional[str] = None,
    is_new_prospect: Optional[bool] = None,
    relationship_summary: Optional[str] = None,
    recent_transcript: Optional[str] = None,
) -> bool:
    """Called by llm_assistant when capture_trade_inquiry tool is invoked."""
    fields: Dict[str, Any] = {
        "whatsapp_number": f"+{phone_number.lstrip('+')}",
        "company_name": company_name,
        "contact_name": contact_name,
        "contact_position": contact_position,
        "contact_email": contact_email,
        "product": product,
        "quantity": quantity,
        "destination_port": destination_port,
        "incoterm": incoterm,
        "packaging": packaging,
        "additional_notes": additional_notes,
        "is_new_prospect": (
            "New prospect" if is_new_prospect is True
            else "Existing partner" if is_new_prospect is False
            else "Not yet qualified"
        ),
    }
    tag = (
        product or company_name or destination_port or incoterm or "WhatsApp"
    )
    subject = f"[Lead] {tag} inquiry from +{phone_number.lstrip('+')}"
    html = _wrap_html(
        title="New trade inquiry from WhatsApp",
        lead_meta=(
            "The Petrobind WhatsApp assistant captured this inquiry. "
            "Follow up with the buyer directly via WhatsApp or the phone number below."
        ),
        table=_fmt_rows(fields),
        extra=_format_snapshot_html(relationship_summary, recent_transcript),
    )
    return await _send_if_configured(subject=subject, html_body=html)


async def send_handoff_email(
    *,
    phone_number: str,
    reason: str,
    partial_inquiry_summary: str = "",
    relationship_summary: Optional[str] = None,
    recent_transcript: Optional[str] = None,
) -> bool:
    """Called when the model hits something outside the KB (zero-hallucination guard)."""
    fields: Dict[str, Any] = {
        "whatsapp_number": f"+{phone_number.lstrip('+')}",
        "reason": reason,
        "summary": partial_inquiry_summary or "(not provided by assistant)",
    }
    subject = f"[Handoff] Sales follow-up needed for +{phone_number.lstrip('+')}"
    html = _wrap_html(
        title="Sales handoff requested",
        lead_meta=(
            "The WhatsApp AI assistant could not confidently answer this "
            "buyer's question (or the inquiry is beyond the knowledge base). "
            "Please take over the conversation."
        ),
        table=_fmt_rows(fields),
        extra=_format_snapshot_html(relationship_summary, recent_transcript),
    )
    return await _send_if_configured(subject=subject, html_body=html)


async def send_booking_interest_email(
    *,
    phone_number: str,
    relationship_summary: Optional[str] = None,
    recent_transcript: Optional[str] = None,
) -> bool:
    """Called the moment the assistant shares the Cal.com link / the buyer
    says they want to book, BEFORE they've necessarily completed the actual
    Cal.com form. Distinct from send_booking_email (which fires only once
    Cal.com's BOOKING_CREATED webhook confirms a real slot) — without this,
    a buyer who says "yes I'd like to book" in chat but never finishes the
    external Cal.com flow would generate no notification at all."""
    fields: Dict[str, Any] = {
        "whatsapp_number": f"+{phone_number.lstrip('+')}",
    }
    subject = f"[Booking Interest] +{phone_number.lstrip('+')} wants to schedule a call"
    html = _wrap_html(
        title="Buyer wants to book a call",
        lead_meta=(
            "The WhatsApp assistant shared the Cal.com booking link with this "
            "buyer. They have NOT necessarily completed the booking yet, "
            "consider following up directly if no slot shows up soon."
        ),
        table=_fmt_rows(fields),
        extra=_format_snapshot_html(relationship_summary, recent_transcript),
    )
    return await _send_if_configured(subject=subject, html_body=html)


async def send_booking_email(
    *,
    phone_number: Optional[str] = None,
    cal_payload: Mapping[str, Any],
) -> bool:
    """Called by the Cal.com webhook route when BOOKING_CREATED fires."""
    title = cal_payload.get("title") or "A new call was booked"
    start = cal_payload.get("startTime") or cal_payload.get("start_time") or ""
    end = cal_payload.get("endTime") or cal_payload.get("end_time") or ""
    event = cal_payload.get("eventTitle") or cal_payload.get("event_type_title") or ""
    attendee = ""
    for a in cal_payload.get("attendees") or cal_payload.get("Attendees") or []:
        if isinstance(a, dict):
            attendee = f"{a.get('name','')} {a.get('email','')}".strip()
            break
    if not attendee:
        attendee = (
            cal_payload.get("organizer", {}).get("email")
            or cal_payload.get("booking", {}).get("primaryEmail")
            or cal_payload.get("primaryEmail")
            or ""
        )
    fields: Dict[str, Any] = {
        "title": title,
        "event_type": event,
        "start": start,
        "end": end,
        "attendee": attendee,
        "buyer_whatsapp": f"+{phone_number.lstrip('+')}" if phone_number else "(not linked)",
        "booking_url": cal_payload.get("bookingUrl") or cal_payload.get("metadata", {}).get("booking_url", ""),
        "notes": cal_payload.get("notes") or cal_payload.get("userResponses", {}),
    }
    subject = f"[Booking] {event or 'Cal.com'} call booked — {start[:16]}"
    html = _wrap_html(
        title="New booking via Cal.com",
        lead_meta=(
            "A prospect scheduled a call using the shared Cal.com booking link. "
            "Confirm availability and reach out via WhatsApp if needed."
        ),
        table=_fmt_rows(fields),
    )
    return await _send_if_configured(subject=subject, html_body=html)


__all__ = [
    "send_lead_email",
    "send_handoff_email",
    "send_booking_email",
]
