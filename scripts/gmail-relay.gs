// Google Apps Script email relay for the Petrobind WhatsApp gateway.
// Deploy: Deploy -> New deployment -> Web app -> Execute as: Me ->
// Who has access: Anyone -> Deploy. Copy the resulting /exec URL into
// GMAIL_RELAY_URL, and the SHARED_SECRET below into GMAIL_RELAY_SECRET.
//
// Security notes:
//   - The recipient is HARDCODED below, never taken from the caller. This
//     is the single most important control: even if SHARED_SECRET leaks,
//     the relay can only ever email FIXED_RECIPIENT, it cannot be turned
//     into an open relay to spam arbitrary addresses from this Gmail
//     account.
//   - SHARED_SECRET must be a long random string (not a real word/phrase),
//     generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
//   - Subject/body length are capped to block abuse even with a valid secret.
//   - A simple per-day send counter blocks runaway abuse if the secret does
//     leak, without needing paid infrastructure.

var SHARED_SECRET = 'REPLACE_WITH_THE_NEW_SECRET';
var FIXED_RECIPIENT = 'milly.phua@petrobindglobal.com';
var MAX_SUBJECT_LEN = 200;
var MAX_BODY_LEN = 20000;
var MAX_SENDS_PER_DAY = 200; // generous for real lead volume, blocks abuse

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);

    if (!timingSafeEquals_(String(payload.secret || ''), SHARED_SECRET)) {
      return jsonOutput_({ ok: false, error: 'unauthorized' });
    }

    var subject = String(payload.subject || '');
    var htmlBody = String(payload.html_body || '');

    if (!subject || !htmlBody) {
      return jsonOutput_({ ok: false, error: 'missing subject/html_body' });
    }
    if (subject.length > MAX_SUBJECT_LEN || htmlBody.length > MAX_BODY_LEN) {
      return jsonOutput_({ ok: false, error: 'subject/body too long' });
    }

    if (!checkAndIncrementDailyQuota_()) {
      return jsonOutput_({ ok: false, error: 'daily send limit reached' });
    }

    // FIXED_RECIPIENT only — the caller's `to` field (if any) is ignored,
    // this is intentional and is the main security control.
    GmailApp.sendEmail(FIXED_RECIPIENT, subject, '', { htmlBody: htmlBody });

    return jsonOutput_({ ok: true });

  } catch (err) {
    return jsonOutput_({ ok: false, error: String(err) });
  }
}

function jsonOutput_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Constant-time-ish string comparison so a leaked timing side-channel can't
// help brute-force the secret. Apps Script has no built-in timing-safe
// compare; this walks the full length regardless of where a mismatch is
// found, which is good enough for this threat model.
function timingSafeEquals_(a, b) {
  if (a.length !== b.length) {
    // Still do a dummy comparison so length mismatches don't return faster.
    var dummy = 0;
    for (var i = 0; i < a.length; i++) dummy |= a.charCodeAt(i) ^ (b.charCodeAt(i % b.length) || 0);
    return false;
  }
  var diff = 0;
  for (var j = 0; j < a.length; j++) {
    diff |= a.charCodeAt(j) ^ b.charCodeAt(j);
  }
  return diff === 0;
}

function checkAndIncrementDailyQuota_() {
  var props = PropertiesService.getScriptProperties();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key = 'sends_' + today;
  var count = parseInt(props.getProperty(key) || '0', 10);
  if (count >= MAX_SENDS_PER_DAY) {
    return false;
  }
  props.setProperty(key, String(count + 1));
  return true;
}
