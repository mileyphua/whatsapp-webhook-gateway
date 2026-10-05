/* =========================================================================
   Petrobind Shared Inbox — browser JS
   Plain vanilla JS + HTMX 2.0; no build step.

   Responsibilities (3 functions per the architecture spec):
   1. CLAIM HEARTBEAT: every 15s POST /api/inbox/chats/{e164}/claim so the
      human's tab doesn't time out mid-composition. Acquires on initial load.
   2. TAB CLOSE: DELETE /api/inbox/chats/{e164}/claim on beforeunload so we
      don't leave stale claims. Falls back to navigator.sendBeacon.
   3. SUGGESTION PILL HANDLERS: Accept / Edit / Discard for the AI RAG reply.
   ========================================================================== */

(function () {
  "use strict";

  // ---------- Helpers ----------
  function getCtx() {
    return (window.__INBOX_CTX__ || {});
  }

  function getCookie(name) {
    const v = "; " + document.cookie;
    const parts = v.split("; " + name + "=");
    if (parts.length === 2) return parts.pop().split(";").shift();
    return null;
  }

  // Read the inbox_sid cookie (non-HttpOnly; set at login time).
  function getSessionId() {
    return getCookie("inbox_sid") || (getCtx().session_id) || "";
  }

  // The BEARER TOKEN is intentionally NOT exposed to JS for security.
  // Instead we send REST calls through the COOKIE auth by using a lightweight
  // proxy pattern: for now we use the INBOX_ADMIN_TOKEN embedded via the
  // /inbox/* pages' internal meta tag. Better: use cookie-based auth for REST.
  function getBearerToken() {
    // V1 reads the server-set meta we write via window.__INBOX_CTX__ not possible
    // because we never put the token there. Instead: page-to-api auth uses the
    // session cookie for HTML but REST uses BEARER. The bearer is NOT in JS
    // global in chat_thread.html; we rely on XHR to same-origin which carries
    // cookies but REST endpoints are Bearer-gated.
    //
    // Simple workaround V1: store the token briefly in sessionStorage after
    // the user enters it on login (there's no better way in a single-password
    // flow without implementing session-token translation endpoints.)
    //
    // FALLBACK: fetch a translated bearer from a cookie → if user has a valid
    // inbox_session cookie, the frontend calls /api/inbox/token. But we didn't
    // build that endpoint. So for V1 we read sessionStorage['_inbox_bearer']
    // which we populate on login by capturing the password before submit.
    return sessionStorage.getItem("_inbox_bearer") || "";
  }

  function inboxFetch(path, { method = "GET", body = null, headers = {}, json = true } = {}) {
    const token = getBearerToken();
    const sid = getSessionId();
    const h = new Headers({
      "Accept": "application/json",
      ...headers,
    });
    if (token) h.set("Authorization", "Bearer " + token);
    if (sid)   h.set("X-Inbox-Session-Id", sid);
    if (body != null && json && typeof body !== "string") {
      h.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    return fetch(path, { method, headers: h, body, credentials: "same-origin" });
  }

  // ---------- Capture login password as bearer (workaround V1) ----------
  if (location.pathname === "/inbox/login") {
    const form = document.querySelector('form[action="/inbox/login"]');
    const pw = form && form.querySelector('input[name="password"]');
    if (form && pw) {
      form.addEventListener("submit", function () {
        try { sessionStorage.setItem("_inbox_bearer", pw.value || ""); } catch (_) {}
      });
    }
  }

  // ---------- Chat thread page only ----------
  const ctx = getCtx();
  if (ctx && ctx.e164) {
    const E164 = ctx.e164;
    const stack = document.getElementById("message-stack");
    if (stack) {
      // Scroll to bottom on first paint
      requestAnimationFrame(() => { stack.scrollTop = stack.scrollHeight; });
    }

    // ------------------------------------------------------------------
    // Fn 1. Reply-mode switch: AI (default) <-> Human.
    //   AI    = no claim; RAG+LLM answers the buyer.
    //   Human = this admin holds the claim; AI is paused. The claim is kept
    //           alive (5 min TTL, renewed every 30s while this chat is open);
    //           once nobody renews it the AI resumes by itself within minutes.
    // ------------------------------------------------------------------
    const HUMAN_TTL = 300;
    let humanMode = false;
    let lockedBy = null;
    const claimUrl = `/api/inbox/chats/${encodeURIComponent(E164)}/claim`;
    const modeSwitch = document.getElementById("mode-switch");
    const modeLabel = document.getElementById("mode-label");
    const modeKnob = document.getElementById("mode-knob");

    // One on/off switch: OFF = AI replies (green), ON = Human replies (navy).
    function paintToggle() {
      if (!modeSwitch) return;
      const base = "inline-flex items-center gap-2 rounded-full border pl-1 pr-3 py-1 text-sm font-medium transition ";
      modeSwitch.setAttribute("aria-checked", String(humanMode));
      modeSwitch.disabled = !!lockedBy;
      if (lockedBy) { modeSwitch.className = base + "border-gray-300 bg-gray-100 text-gray-500 cursor-not-allowed"; modeLabel.textContent = "Locked by " + (lockedBy.held_by || "another admin"); modeKnob.style.transform = "none"; }
      else if (humanMode) { modeSwitch.className = base + "border-pb-navy bg-pb-navy text-white"; modeLabel.textContent = "Human replying"; modeKnob.style.transform = "none"; }
      else { modeSwitch.className = base + "border-pb-green bg-pb-green text-white"; modeLabel.textContent = "AI replying"; modeKnob.style.transform = "none"; }
    }
    function applyMode() {
      paintToggle();
      const rel = document.getElementById("release-lock");
      if (rel) rel.hidden = !(lockedBy && ctx.is_admin);       // only the admin can break someone's lock
      if (lockedBy) {
        setClaimBannerVisible(true, lockedBy);
        setClaimStatus(`${lockedBy.held_by || "A colleague"} is replying to this chat. Read-only.`, { sendEnabled: false });
      } else if (humanMode) {
        setClaimBannerVisible(false);
        setClaimStatus("You are replying. AI is paused for this chat. Switch back to AI when done.", { sendEnabled: true });
      } else {
        setClaimBannerVisible(false);
        setClaimStatus("AI is answering this chat (RAG + LLM). Switch to Human, or just start typing, to take over.", { sendEnabled: false, typeEnabled: true });
      }
    }
    async function loadMode() {
      try {
        const r = await inboxFetch(claimUrl);
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { applyMode(); return; }
        if (d.held && d.mine) { humanMode = true; lockedBy = null; }
        else if (d.held) { humanMode = false; lockedBy = { held_by: d.held_by, expires_in_secs: d.expires_in_secs }; }
        else { humanMode = false; lockedBy = null; }
      } catch (_) { /* keep last state */ }
      applyMode();
    }
    async function goHuman() {
      try {
        const r = await inboxFetch(claimUrl, { method: "POST", body: { ttl_seconds: HUMAN_TTL } });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.acquired) { humanMode = true; lockedBy = null; }
        else if (r.status === 409) { humanMode = false; lockedBy = { held_by: d.held_by, expires_in_secs: d.expires_in_secs }; }
      } catch (_) {}
      applyMode();
    }
    async function goAI() {
      try { await inboxFetch(claimUrl, { method: "DELETE" }); } catch (_) {}
      humanMode = false; applyMode();
    }
    if (modeSwitch) modeSwitch.addEventListener("click", function () { if (humanMode) goAI(); else goHuman(); });
    const releaseLockBtn = document.getElementById("release-lock");
    if (releaseLockBtn) releaseLockBtn.addEventListener("click", async function () {
      try { await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/claim?force=1`, { method: "DELETE" }); } catch (_) {}
      await loadMode();
    });
    const bannerDismissBtn = document.getElementById("claim-banner-dismiss");
    if (bannerDismissBtn) bannerDismissBtn.addEventListener("click", () => setClaimBannerVisible(false));

    // Initial state comes from the server (default = AI); then renew / re-check every 30s.
    applyMode();
    loadMode();
    // Check who holds the chat every 5 s (so a colleague's lock, or its release, shows up quickly). The lock is only
    // renewed (every ~30 s) while the server still says it is ours, so an admin's release is never silently undone.
    let lockTick = 0;
    setInterval(async function () {
      await loadMode();
      if (humanMode && ++lockTick % 6 === 0) goHuman();
    }, 5000);

    // sendEnabled: the Send button works (Human mode). typeEnabled: the box accepts text; in AI mode typing
    // is allowed and simply takes over from the AI, so the box is never a dead end.
    function setClaimStatus(text, { sendEnabled = true, typeEnabled = sendEnabled } = {}) {
      const el = document.getElementById("claim-status-text");
      const sendBtn = document.getElementById("btn-send-submit");
      const textarea = document.getElementById("human-text-input");
      if (el) el.textContent = text;
      if (sendBtn) {
        sendBtn.disabled = !sendEnabled;
        sendBtn.title = sendEnabled ? "" : (typeEnabled ? "Starting your reply pauses the AI for this chat." : "Someone else is replying.");
      }
      if (textarea) {
        textarea.disabled = !typeEnabled;
        textarea.placeholder = !typeEnabled ? "A colleague is replying to this chat. Read-only."
          : sendEnabled ? `Type your reply to ${E164}… (Shift+Enter for newline, Enter to send)`
          : "The AI is replying. Start typing to take over and reply yourself…";
      }
      syncAttach();
    }

    function setClaimBannerVisible(visible, heldInfo = null) {
      const banner = document.getElementById("claim-banner");
      const txt = document.getElementById("claim-banner-text");
      if (!banner) return;
      banner.classList.toggle("hidden", !visible);
      if (visible && heldInfo) {
        txt.textContent =
          `⚠ ${heldInfo.held_by || "A colleague"} is replying to this chat` +
          (heldInfo.expires_in_secs ? ` (about ${heldInfo.expires_in_secs}s left)` : "") +
          `. You can read but not send.`;
      }
    }

    // ------------------------------------------------------------------
    // Fn 3. AI suggestion pill — Accept / Edit / Discard
    // ------------------------------------------------------------------
    const pillEl       = document.getElementById("suggestion-pill");
    const sugTextEl    = document.getElementById("suggestion-text");
    const sugMetaEl    = document.getElementById("suggestion-meta");
    const btnAccept    = document.getElementById("btn-suggestion-accept");
    const btnEdit      = document.getElementById("btn-suggestion-edit");
    const btnDiscard   = document.getElementById("btn-suggestion-discard");
    const btnReload    = document.getElementById("btn-suggestion-reload-inline");
    const textarea     = document.getElementById("human-text-input");
    const sendForm     = document.getElementById("human-send-form");
    const sendBtn      = document.getElementById("btn-send-submit");

    let currentSuggestionText = "";

    async function loadSuggestion(forceFresh = false) {
      if (!pillEl) return;
      pillEl.classList.remove("hidden");
      if (sugTextEl) {
        sugTextEl.textContent = forceFresh ? "" : (sugTextEl.textContent || "");
        sugTextEl.classList.add("loading");
      }
      if (sugMetaEl) sugMetaEl.textContent = forceFresh ? "fresh call…" : "calling LLM draft_only pipeline…";
      if (forceFresh) {
        try { await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/suggestion`, { method: "DELETE" }); } catch (_) {}
      }
      try {
        const t0 = performance.now();
        const resp = await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/suggestion`);
        const ms = Math.round(performance.now() - t0);
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data && data.suggestion_text) {
          currentSuggestionText = data.suggestion_text;
          if (sugTextEl) {
            sugTextEl.classList.remove("loading");
            sugTextEl.textContent = currentSuggestionText;
          }
          if (sugMetaEl) {
            const cacheInfo = data.cached ? `from cache (${data.age_secs || 0}s old) ` : "";
            sugMetaEl.textContent = `${cacheInfo}${ms}ms · ${currentSuggestionText.length} chars · draft_only=True (zero side effects)`;
          }
        } else {
          if (sugTextEl) sugTextEl.classList.remove("loading");
          if (sugTextEl) sugTextEl.textContent = `(No suggestion available${data && data.detail ? ": " + data.detail : ""})`;
          if (sugMetaEl) sugMetaEl.textContent = `${ms}ms`;
        }
      } catch (err) {
        if (sugTextEl) {
          sugTextEl.classList.remove("loading");
          sugTextEl.textContent = `Error loading suggestion: ${err && err.message ? err.message : err}`;
        }
      }
    }

    // The reply form is handled only here (it carries no hx-* attributes, so HTMX never binds to it):
    // a plain fetch lets us send the session id + auth, show the bubble at once and report real errors.
    if (sendForm) {
      sendForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        if (!textarea) return;
        const text = (textarea.value || "").trim();
        if (!text && !pendingFile) return;
        if (sendBtn && sendBtn.disabled) {
          // claimed by other → flash
          const banner = document.getElementById("claim-banner");
          if (banner) { banner.classList.remove("hidden"); banner.animate(
            [{ transform: "translateY(-8px)" }, { transform: "translateY(0)" }],
            { duration: 250, easing: "ease-out" }); }
          return;
        }
        // Quote a message only if the admin chose "Reply" on it (like WhatsApp), never automatically.
        const quoted = pendingQuote;
        clearQuote();
        const file = pendingFile;                       // photo/document chosen with the paperclip (sent with the text as its caption)
        const fileKind = file ? (/\.(jpe?g|png)$/i.test(file.name) ? "image" : "document") : null;
        const previewUrl = file && fileKind === "image" ? URL.createObjectURL(file) : null;
        clearAttachment(true);
        // Append optimistic bubble
        appendBubble({ direction: "human", text, held_by: ctx.admin_name, created_at: new Date().toISOString(), _optimistic: true, quote: quoted ? quoted.text : null,
                       attachment: file ? { kind: fileKind, name: file.name, previewUrl: previewUrl } : null,
                       matchText: file && !text ? "📎 " + file.name : null });
        textarea.value = "";
        textarea.focus();
        // If the send fails, drop the unsent bubble and put the text back so it can be retried.
        function undoUnsent() {
          const mine = Array.from(stack.querySelectorAll('[data-optimistic][data-direction="human"]')).reverse().find(function (n) { return n.getAttribute("data-text") === text; });
          if (mine) mine.remove();
          if (!textarea.value) { textarea.value = text; autoGrowComposer(); }
          if (quoted) setQuote(quoted);
          if (file) setAttachment(file);
        }
        try {
          let resp;
          if (file) {
            const fd = new FormData();
            fd.append("file", file);
            fd.append("caption", text);
            if (quoted) fd.append("reply_to_wamid", quoted.wamid);
            resp = await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/attachments`, { method: "POST", body: fd, json: false });
          } else {
            resp = await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/messages`, {
              method: "POST",
              body: { text, reply_to_wamid: quoted ? quoted.wamid : null },
            });
          }
          const data = await resp.json().catch(() => ({}));
          if (resp.ok && data && data.success) {
            // all good — bubble already there
            if (sugMetaEl) sugMetaEl.textContent = `✓ Human reply sent.`;
          } else {
            // failure → append a small error bubble
            undoUnsent();
            appendBubble({ direction: "system",
              text: `[Not sent: ${(data && (data.detail || data.error)) || ("error " + resp.status)}]`,
              created_at: new Date().toISOString(), _optimistic: true, errored: true });
          }
        } catch (err) {
          undoUnsent();
          appendBubble({ direction: "system",
            text: `[Not sent (network problem): ${err && err.message ? err.message : err}]`,
            created_at: new Date().toISOString(), _optimistic: true });
        }
      });
    }

    // Shift+Enter = newline, Enter (no shift) = submit form from textarea
    if (textarea) {
      textarea.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          if (sendForm) sendForm.requestSubmit();
        }
      });
    }

    if (btnAccept) {
      btnAccept.addEventListener("click", async function () {
        if (!currentSuggestionText || !textarea) return;
        if (!humanMode) await goHuman();            // sending is a human action: take over from the AI first
        if (!humanMode) return;                     // a colleague holds the chat
        textarea.value = currentSuggestionText;
        if (sendForm) sendForm.requestSubmit();
      });
    }
    if (btnEdit) {
      btnEdit.addEventListener("click", async function () {
        if (!currentSuggestionText || !textarea) return;
        if (!humanMode) await goHuman();            // editing means you are replying: pause the AI so Send works
        textarea.value = currentSuggestionText;
        autoGrowComposer();
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        // hide pill to reduce clutter (they requested manual edit)
        if (pillEl) pillEl.classList.add("hidden");
      });
    }
    if (btnDiscard) {
      btnDiscard.addEventListener("click", async function () {
        try { await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/suggestion`, { method: "DELETE" }); } catch (_) {}
        if (pillEl) pillEl.classList.add("hidden");
        currentSuggestionText = "";
      });
    }
    if (btnReload) {
      btnReload.addEventListener("click", function () { loadSuggestion(true); });
    }

    // Auto-load a suggestion if there's a last buyer message (the RAG context is there).
    if (ctx.last_buyer_wamid) {
      // Delay 400ms so the page paints first
      setTimeout(() => loadSuggestion(false), 400);
    }

    // ---------- Append bubble helper (for optimistic human sends) ----------
    function appendBubble({ direction, text, held_by = null, created_at = null, _optimistic = false, errored = false, id = null, wamid = "", reply_to_wamid = "", quote = null, attachment = null, matchText = null }) {
      if (!stack) return;
      const wrap = document.createElement("div");
      if (id != null) wrap.setAttribute("data-msg-id", String(id));
      wrap.setAttribute("data-direction", direction || "");
      wrap.setAttribute("data-text", matchText != null ? matchText : (text || ""));
      if (wamid) wrap.setAttribute("data-wamid", wamid);
      wrap.className = "flex " + (direction === "buyer" ? "justify-start" : "justify-end");
      if (_optimistic) wrap.setAttribute("data-optimistic", "1");
      const bubble = document.createElement("div");
      const classMap = {
        buyer:  "bg-white text-gray-900 rounded-tl-sm border border-gray-100",
        ai:     "bg-pb-aiBubble text-gray-900 rounded-tr-sm border border-green-100",
        human:  "bg-pb-humanBubble text-gray-900 rounded-tr-sm border border-blue-100",
        system: "bg-pb-systemBubble text-amber-900 rounded-tr-sm border border-amber-200 italic text-sm",
      };
      bubble.className =
        "message-bubble max-w-[80%] md:max-w-[70%] px-3.5 py-2 rounded-2xl text-[15px] leading-relaxed shadow-sm " +
        (classMap[direction] || "bg-white");
      const labelMap = {
        ai: `<div class="text-[10px] font-bold uppercase tracking-wider text-pb-greenDark/80 mb-0.5">AI · Jane Tan</div>`,
        human: `<div class="text-[10px] font-bold uppercase tracking-wider text-blue-700/80 mb-0.5">${held_by || "Petrobind Admin"} · human</div>`,
        system: `<div class="text-[10px] font-bold uppercase tracking-wider text-amber-700/80 mb-0.5">system note</div>`,
      };
      const label = labelMap[direction] || "";
      const ts = created_at ? new Date(created_at) : new Date();
      const timeStr = ts.toISOString().replace("T", " ").slice(0, 19);
      const erroredHtml = errored
        ? `<div class="mt-1.5 text-[11px] text-red-700 bg-red-50 rounded px-2 py-1 border border-red-100">⚠ Send failed</div>`
        : "";
      let quoteText = quote;
      if (!quoteText && reply_to_wamid) {
        const q = Array.from(stack.querySelectorAll("[data-wamid]")).find(function (n) { return n.getAttribute("data-wamid") === reply_to_wamid; });
        const qt = q && q.querySelector(".whitespace-pre-wrap");
        quoteText = qt ? qt.textContent : null;
      }
      const quoteHtml = quoteText
        ? `<div class="mb-1 pl-2 border-l-4 border-black/20 text-xs text-gray-600 line-clamp-2 break-words">${escapeHtml(String(quoteText).slice(0, 200))}</div>`
        : "";
      const attHtml = attachment
        ? `<div class="attachment-chip mt-1 inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-white/70 border border-black/10 text-xs text-gray-800"><span aria-hidden="true">${attachment.kind === "image" ? "🖼" : "📄"}</span><span class="break-all">${escapeHtml(attachment.name || "")}</span></div>` +
          (attachment.previewUrl ? `<img src="${attachment.previewUrl}" alt="" class="mt-1 max-h-48 rounded-lg">` : "")
        : "";
      bubble.innerHTML =
        label +
        quoteHtml +
        `<div class="whitespace-pre-wrap break-words">${escapeHtml(text || "")}</div>` +
        attHtml +
        erroredHtml +
        `<div class="mt-1 flex items-center justify-end gap-2">
           <div class="text-[10px] text-gray-400"><time>${timeStr}</time></div>
         </div>`;
      wrap.appendChild(bubble);
      stack.appendChild(wrap);
      stack.scrollTop = stack.scrollHeight;
      if (direction === "ai") decorateFeedback(wrap);
      decorateReply(wrap);
    }

    function escapeHtml(s) {
      return (s + "").replace(/[&<>"']/g, function (c) {
        return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
      });
    }

    // ---------- Supabase Realtime optional (poll fallback always available) ----------
    if (ctx.supa_url && ctx.supa_anon_key && window.supabase) {
      try {
        const sbClient = window.supabase.createClient(ctx.supa_url, ctx.supa_anon_key, {
          realtime: { params: { eventsPerSecond: 10 } }
        });
        const ch = sbClient
          .channel(`inbox-thread-${E164}`)
          .on("postgres_changes", {
            event: "INSERT",
            schema: "public",
            table: "messages",
            filter: `e164=eq.${E164}`,
          }, (payload) => {
            const m = (payload && payload.new) || null;
            if (!m || m._optimistic) return;
            showServerMessage(m);
          })
          .subscribe();
        console.debug("[realtime] subscribed to messages for", E164);
      } catch (err) {
        console.warn("[realtime] setup failed — falling back to manual refresh", err);
      }
    }


    // ---------- Live thread: show every buyer / AI (RAG) / human message once ----------
    // Polls every 3s (works with or without Supabase Realtime); de-dupes by message id
    // and swaps a just-sent optimistic bubble for its server copy instead of doubling it.
    function showServerMessage(m) {
      if (m == null || m.id == null) return;
      const id = String(m.id);
      const known = Array.from(stack.querySelectorAll("[data-msg-id]")).some(function (n) { return n.getAttribute("data-msg-id") === id; });
      if (known) return;
      if (m.direction === "human") {
        const opt = Array.from(stack.querySelectorAll("[data-optimistic]")).find(function (n) { return n.getAttribute("data-text") === (m.text || ""); });
        if (opt) opt.remove();
      }
      appendBubble({ direction: m.direction, text: m.text, held_by: m.held_by, created_at: m.created_at, id: m.id, wamid: m.wamid || "", reply_to_wamid: m.reply_to_wamid || "",
        attachment: m.media_type ? { kind: m.media_type, name: m.filename || (m.media_type === "image" ? "Photo" : "Document") } : null });
      if (m.direction === "buyer") pollWindowStatus(E164);
    }
    async function pollThread() {
      try {
        const resp = await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/messages?limit=300`);
        if (!resp.ok) return;
        const data = await resp.json();
        (data.messages || []).forEach(showServerMessage);
      } catch (_) { /* next tick retries */ }
    }
    setInterval(function () { if (!document.hidden) pollThread(); }, 3000);



    // ---------- Composer: grows with the text (up to half the screen), can be dragged taller, typing takes over ----------
    function autoGrowComposer() {
      const ta = document.getElementById("human-text-input"); if (!ta) return;
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight + 2, Math.round(window.innerHeight * 0.5)) + "px";
    }
    (function wireComposer() {
      const ta = document.getElementById("human-text-input"); if (!ta) return;
      ta.addEventListener("input", function () {
        autoGrowComposer();
        if (!humanMode && !lockedBy && ta.value.trim()) goHuman();   // first keystroke pauses the AI for this chat
      });
      autoGrowComposer();
    })();


    // ---------- Quote-reply (like WhatsApp): the admin picks a message to reply to; nothing is quoted otherwise ----------
    let pendingQuote = null;   // { wamid, text, who }
    function setQuote(q) {
      pendingQuote = q;
      const box = document.getElementById("quote-preview"); if (!box) return;
      document.getElementById("quote-who").textContent = "Replying to " + q.who;
      document.getElementById("quote-text").textContent = q.text;
      box.hidden = false;
    }
    function clearQuote() {
      pendingQuote = null;
      const box = document.getElementById("quote-preview"); if (box) box.hidden = true;
    }
    const quoteClearBtn = document.getElementById("quote-clear");
    if (quoteClearBtn) quoteClearBtn.addEventListener("click", clearQuote);
    function decorateReply(wrap) {
      if (!wrap || wrap.querySelector(".reply-btn")) return;
      const wamid = wrap.getAttribute("data-wamid");
      const dir = wrap.getAttribute("data-direction");
      if (!wamid || (dir !== "buyer" && dir !== "ai" && dir !== "human")) return;
      const bubble = wrap.querySelector(".message-bubble"), textEl = wrap.querySelector(".whitespace-pre-wrap");
      if (!bubble || !textEl || !textEl.textContent.trim()) return;
      const b = document.createElement("button");
      b.type = "button"; b.className = "reply-btn mt-1 text-[11px] text-gray-500 hover:text-gray-900 underline";
      b.textContent = "↩ Reply"; b.setAttribute("aria-label", "Reply to this message");
      b.addEventListener("click", function () {
        setQuote({ wamid: wamid, text: textEl.textContent.slice(0, 200), who: dir === "buyer" ? "the buyer" : dir === "ai" ? "the AI" : "your message" });
        if (!humanMode && !lockedBy) goHuman();     // replying is a human action: take over from the AI
        const ta = document.getElementById("human-text-input"); if (ta && !ta.disabled) ta.focus();
      });
      bubble.appendChild(b);
    }
    function decorateAllReplies() { stack.querySelectorAll("[data-direction]").forEach(decorateReply); }
    decorateAllReplies();


    // ---------- Attachments: photo / document sent with the reply (caption = the text box) ----------
    let pendingFile = null;
    const ATTACH_MAX_IMAGE = 5 * 1024 * 1024, ATTACH_MAX_DOC = 16 * 1024 * 1024;
    const attachBtn = document.getElementById("attach-btn"), attachInput = document.getElementById("attach-input");
    function fmtBytes(n) { return n < 1024 * 1024 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
    function attachError(msg) { const st = document.getElementById("claim-status-text"); if (st) st.textContent = msg; }
    function clearAttachment(keepUrl) {
      pendingFile = null;
      const box = document.getElementById("attach-preview"); if (box) box.hidden = true;
      const th = document.getElementById("attach-thumb"); if (th) { th.hidden = true; if (!keepUrl && th.src) URL.revokeObjectURL(th.src); th.removeAttribute("src"); }
      if (attachInput) attachInput.value = "";
    }
    function setAttachment(f) {
      const isImage = /\.(jpe?g|png)$/i.test(f.name), isDoc = /\.(pdf|docx?|xlsx?|pptx?|txt)$/i.test(f.name);
      if (!isImage && !isDoc) { attachError("This file type can't be sent. Use JPG, PNG, PDF, Word, Excel, PowerPoint or TXT."); return; }
      if (f.size > (isImage ? ATTACH_MAX_IMAGE : ATTACH_MAX_DOC)) { attachError("That file is too large (images up to 5 MB, documents up to 16 MB)."); return; }
      if (f.size === 0) { attachError("That file is empty."); return; }
      pendingFile = f;
      document.getElementById("attach-name").textContent = f.name;
      document.getElementById("attach-size").textContent = (isImage ? "Photo" : "Document") + " · " + fmtBytes(f.size) + " · add a caption in the box, then Send";
      const th = document.getElementById("attach-thumb");
      if (isImage) { th.src = URL.createObjectURL(f); th.hidden = false; } else { th.hidden = true; th.removeAttribute("src"); }
      document.getElementById("attach-preview").hidden = false;
      if (!humanMode && !lockedBy) goHuman();          // sending a file is a human action: take over from the AI first
      const ta = document.getElementById("human-text-input"); if (ta && !ta.disabled) ta.focus();
    }
    function syncAttach() { const b = document.getElementById("attach-btn"), ta = document.getElementById("human-text-input"); if (b && ta) b.disabled = ta.disabled; }
    if (attachBtn && attachInput) {
      attachBtn.addEventListener("click", function () { if (!attachBtn.disabled) attachInput.click(); });
      attachInput.addEventListener("change", function () { if (attachInput.files && attachInput.files[0]) setAttachment(attachInput.files[0]); });
    }
    const attachClearBtn = document.getElementById("attach-clear");
    if (attachClearBtn) attachClearBtn.addEventListener("click", function () { clearAttachment(false); });
    syncAttach();

    // ---------- Feedback on AI replies (ChatGPT-style): good / bad under every AI message ----------
    // Bad opens a dialog where the admin says why; the server turns that into a suggested skill.
    const FB_REASONS = ["Wrong or inaccurate information", "Didn't answer the question", "Too long", "Sounds like a bot or repeats itself", "Wrong tone", "Should have handed over to a human"];
    const fbState = {};   // ai_text -> "up" | "down"
    const SVG_UP = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 10v11"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 1.9 2.5l-1.8 7A2 2 0 0 1 18 21H7V10l4-8a2.5 2.5 0 0 1 4 3.9Z"/></svg>';
    const SVG_DOWN = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 14V3"/><path d="m9 18.1 1-4.1H4.2a2 2 0 0 1-1.9-2.5l1.8-7A2 2 0 0 1 6 3h11v11l-4 8a2.5 2.5 0 0 1-4-3.9Z"/></svg>';
    function mk(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
    function precedingBuyerText(wrap) {
      let n = wrap.previousElementSibling;
      while (n) {
        if (n.getAttribute && n.getAttribute("data-direction") === "buyer") { const t = n.querySelector(".whitespace-pre-wrap"); return t ? t.textContent : ""; }
        n = n.previousElementSibling;
      }
      return "";
    }
    function aiTextOf(wrap) { const t = wrap.querySelector(".whitespace-pre-wrap"); return t ? t.textContent : ""; }
    let fbToastTimer = null;
    function fbToast(msg, linkText) {
      let t = document.getElementById("fb-toast");
      if (!t) { t = mk("div", "fixed left-1/2 -translate-x-1/2 bottom-6 z-50 max-w-[92vw] rounded-lg bg-gray-900 text-white text-sm px-4 py-2.5 shadow-xl"); t.id = "fb-toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
      t.textContent = msg + " ";
      if (linkText) { const a = mk("a", "underline font-medium", linkText); a.href = "/inbox/learning"; a.target = "_top"; t.appendChild(a); }
      t.hidden = false; clearTimeout(fbToastTimer); fbToastTimer = setTimeout(function () { t.hidden = true; }, 7000);
    }
    function paintFeedback(wrap, rating) {
      const up = wrap.querySelector(".fb-up"), dn = wrap.querySelector(".fb-down"); if (!up || !dn) return;
      const base = "fb-btn inline-flex items-center justify-center w-7 h-7 rounded-md transition ";
      up.setAttribute("aria-pressed", String(rating === "up")); dn.setAttribute("aria-pressed", String(rating === "down"));
      up.className = "fb-up " + base + (rating === "up" ? "text-green-700 bg-green-100" : "text-gray-500 hover:text-gray-900 hover:bg-black/5");
      dn.className = "fb-down " + base + (rating === "down" ? "text-red-700 bg-red-100" : "text-gray-500 hover:text-gray-900 hover:bg-black/5");
    }
    async function sendFeedback(wrap, payload) {
      const body = Object.assign({ e164: E164, ai_text: aiTextOf(wrap), buyer_text: precedingBuyerText(wrap) }, payload);
      const r = await inboxFetch("/api/inbox/feedback", { method: "POST", body: body });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      fbState[body.ai_text] = payload.rating; paintFeedback(wrap, payload.rating);
      return j;
    }
    function feedbackDialog() {
      let d = document.getElementById("fb-dialog"); if (d) return d;
      d = mk("dialog", "rounded-2xl p-0 w-[92vw] max-w-lg border-0 shadow-2xl"); d.id = "fb-dialog"; d.setAttribute("aria-labelledby", "fb-title");
      const box = mk("div", "p-5 space-y-3");
      box.appendChild(Object.assign(mk("h2", "text-lg font-semibold text-gray-900", "What went wrong with this reply?"), { id: "fb-title" }));
      box.appendChild(Object.assign(mk("blockquote", "text-sm text-gray-600 bg-gray-50 border-l-4 border-gray-300 rounded px-3 py-2 max-h-24 overflow-y-auto whitespace-pre-wrap"), { id: "fb-quote" }));
      const chips = mk("div", "flex flex-wrap gap-1.5"); chips.id = "fb-chips"; chips.setAttribute("role", "group"); chips.setAttribute("aria-label", "Reasons");
      box.appendChild(chips);
      const lab1 = mk("label", "block"); lab1.appendChild(mk("span", "block text-xs font-medium text-gray-700 mb-0.5", "Tell the AI what to do differently"));
      const note = mk("textarea", "w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"); note.id = "fb-note"; note.rows = 2; note.maxLength = 1000; note.placeholder = "e.g. Don't quote a price, ask for quantity and destination first"; lab1.appendChild(note); box.appendChild(lab1);
      const lab2 = mk("label", "block"); lab2.appendChild(mk("span", "block text-xs font-medium text-gray-700 mb-0.5", "How would you have replied? (the fastest way to teach it)"));
      const better = mk("textarea", "w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"); better.id = "fb-better"; better.rows = 3; better.maxLength = 1500; lab2.appendChild(better); box.appendChild(lab2);
      box.appendChild(Object.assign(mk("div", "text-xs text-red-600"), { id: "fb-err", hidden: true }));
      const row = mk("div", "flex items-center justify-between gap-3");
      row.appendChild(mk("span", "text-[11px] text-gray-500", "Your feedback is turned into a skill suggestion that you approve before the AI uses it."));
      const btns = mk("div", "flex gap-2 shrink-0");
      const cancel = mk("button", "px-4 py-2 rounded-full border border-gray-300 text-sm font-medium text-gray-700 hover:bg-gray-50", "Cancel"); cancel.type = "button"; cancel.id = "fb-cancel";
      const submit = mk("button", "px-4 py-2 rounded-full bg-gray-900 text-white text-sm font-medium disabled:opacity-40", "Submit"); submit.type = "button"; submit.id = "fb-submit";
      btns.appendChild(cancel); btns.appendChild(submit); row.appendChild(btns); box.appendChild(row);
      d.appendChild(box); document.body.appendChild(d);
      cancel.addEventListener("click", function () { d.close(); });
      return d;
    }
    function openFeedbackDialog(wrap) {
      const d = feedbackDialog(), chips = d.querySelector("#fb-chips"), note = d.querySelector("#fb-note"), better = d.querySelector("#fb-better"), submit = d.querySelector("#fb-submit"), err = d.querySelector("#fb-err");
      d.querySelector("#fb-quote").textContent = aiTextOf(wrap);
      note.value = ""; better.value = ""; err.hidden = true; chips.textContent = "";
      const picked = new Set();
      function refresh() { submit.disabled = !(picked.size || note.value.trim() || better.value.trim()); }
      FB_REASONS.forEach(function (t) {
        const b = mk("button", "px-3 py-1.5 rounded-full border border-gray-300 text-sm text-gray-700 hover:bg-gray-50", t); b.type = "button"; b.setAttribute("aria-pressed", "false");
        b.addEventListener("click", function () {
          if (picked.has(t)) { picked.delete(t); b.setAttribute("aria-pressed", "false"); b.className = "px-3 py-1.5 rounded-full border border-gray-300 text-sm text-gray-700 hover:bg-gray-50"; }
          else { picked.add(t); b.setAttribute("aria-pressed", "true"); b.className = "px-3 py-1.5 rounded-full border border-gray-900 bg-gray-900 text-sm text-white"; }
          refresh();
        });
        chips.appendChild(b);
      });
      note.oninput = refresh; better.oninput = refresh; refresh();
      submit.onclick = async function () {
        submit.disabled = true; submit.textContent = "Sending…"; err.hidden = true;
        try {
          const j = await sendFeedback(wrap, { rating: "down", tags: Array.from(picked), note: note.value.trim(), better_reply: better.value.trim() });
          d.close();
          fbToast(j.learning_started ? "Thanks. The AI is turning this into a skill suggestion." : "Thanks, feedback saved.", j.learning_started ? "Review it in Skills & learning →" : "");
        } catch (e) { err.textContent = "Could not save feedback. Please try again."; err.hidden = false; }
        finally { submit.textContent = "Submit"; refresh(); }
      };
      d.showModal(); note.focus();
    }
    function decorateFeedback(wrap) {
      if (!wrap || wrap.querySelector(".fb-row") || wrap.getAttribute("data-direction") !== "ai") return;
      const bubble = wrap.querySelector(".message-bubble");
      if (!bubble || !aiTextOf(wrap).trim()) return;
      const row = mk("div", "fb-row mt-1 -mb-0.5 flex items-center gap-0.5"); row.setAttribute("role", "group"); row.setAttribute("aria-label", "Rate this reply");
      const up = mk("button", "fb-up"); up.type = "button"; up.innerHTML = SVG_UP; up.setAttribute("aria-label", "Good response"); up.title = "Good response";
      const dn = mk("button", "fb-down"); dn.type = "button"; dn.innerHTML = SVG_DOWN; dn.setAttribute("aria-label", "Bad response"); dn.title = "Bad response";
      up.addEventListener("click", async function () {
        try { await sendFeedback(wrap, { rating: "up", tags: [], note: "", better_reply: "" }); fbToast("Thanks for the feedback."); } catch (_) { fbToast("Could not save feedback. Please try again."); }
      });
      dn.addEventListener("click", function () { openFeedbackDialog(wrap); });
      row.appendChild(up); row.appendChild(dn); bubble.appendChild(row);
      paintFeedback(wrap, fbState[aiTextOf(wrap)] || null);
    }
    function decorateAllFeedback() { stack.querySelectorAll('[data-direction="ai"]').forEach(decorateFeedback); }
    decorateAllFeedback();   // buttons appear immediately; saved ratings are painted in once loaded
    (async function loadFeedbackState() {
      try {
        const r = await inboxFetch("/api/inbox/feedback?e164=" + encodeURIComponent(E164));
        if (r.ok) { (await r.json()).feedback.forEach(function (f) { fbState[f.ai_text] = f.rating; }); }
      } catch (_) {}
      stack.querySelectorAll('[data-direction="ai"]').forEach(function (w) { paintFeedback(w, fbState[aiTextOf(w)] || null); });
    })();

    // ---------- FR10: 24h window banner + send form gate ----------
    let windowPollTimer = null;
    let lastWindowState = { inside_24h_window: null, window_closes_at_unix_ts: null };

    function formatDuration(secondsLeft) {
      if (secondsLeft == null || isNaN(secondsLeft) || secondsLeft <= 0) return "0m";
      const h = Math.floor(secondsLeft / 3600);
      const m = Math.floor((secondsLeft % 3600) / 60);
      const s = Math.floor(secondsLeft % 60);
      if (h > 0) return `${h}h ${m}m`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }

    async function fetchWindow(e164) {
      try {
        const resp = await inboxFetch(`/api/inbox/window-check/${encodeURIComponent(e164)}`);
        if (!resp.ok) return null;
        return await resp.json().catch(() => null);
      } catch (_) {
        return null;
      }
    }

    function renderWindowBanner(isInside, closesAt) {
      if (!document.getElementById("window-status-banner")) return;
      const banner = document.getElementById("window-status-banner");
      const textarea = document.getElementById("human-text-input");
      const sendBtn = document.getElementById("btn-send-submit");
      const overlay = document.getElementById("send-form-disabled-overlay");
      const formWrap = document.getElementById("send-form-wrapper");
      const pickBtn = document.getElementById("btn-pick-template-inline");

      if (!banner) return;

      banner.classList.remove("banner-slideIn");
      void banner.offsetWidth;

      if (isInside) {
        const secondsLeft = closesAt ? Math.max(0, closesAt - Math.floor(Date.now() / 1000)) : 0;
        banner.className = "w-full sticky top-0 z-40 px-4 md:px-10 py-2.5 banner-slideIn";
        banner.innerHTML =
          `<div class="max-w-5xl mx-auto bg-green-50 border border-green-200 text-green-900 rounded-md px-3 py-2 flex justify-between items-center text-sm">
             <span class="flex items-center gap-2">
               <svg class="w-4 h-4 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
               <strong>24-hour reply window OPEN</strong> — expires in ${formatDuration(secondsLeft)}. Freeform messages allowed.
             </span>
             <span class="text-green-700 font-mono text-xs">${closesAt ? new Date(closesAt * 1000).toLocaleTimeString() : ""}</span>
           </div>`;
        if (textarea) { textarea.disabled = !!lockedBy; textarea.style.opacity = "1"; }
        syncAttach();
        if (sendBtn) sendBtn.disabled = !(humanMode && !lockedBy);   // Send needs Human mode
        if (overlay) overlay.classList.add("hidden");
      } else {
        banner.className = "w-full sticky top-0 z-40 px-4 md:px-10 py-2.5 banner-slideIn";
        banner.innerHTML =
          `<div class="max-w-5xl mx-auto bg-amber-50 border border-amber-200 text-amber-900 rounded-md px-3 py-2 flex justify-between items-center text-sm gap-3">
             <span class="flex items-center gap-2">
               <svg class="w-4 h-4 text-amber-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
               <strong>⚠ OUTSIDE 24-hour reply window.</strong> Sending switched to Meta-approved templates.
             </span>
             <button type="button" id="banner-pick-template" class="shrink-0 inline-flex items-center gap-1 px-3 py-1 rounded-md bg-amber-600 hover:bg-amber-700 text-white text-xs font-medium transition">
               Pick Template →
             </button>
           </div>`;
        const bPick = document.getElementById("banner-pick-template");
        if (bPick) bPick.addEventListener("click", () => {
          if (typeof window.openNewConversationModal === "function") window.openNewConversationModal("template", E164);
        });
        if (textarea) { textarea.disabled = true; textarea.style.opacity = "0.45"; }
        syncAttach();
        if (sendBtn) sendBtn.disabled = true;
        if (overlay && formWrap) {
          overlay.classList.remove("hidden");
          overlay.style.top = "1rem";
          overlay.style.left = formWrap.clientWidth > 0 ? "50%" : "1rem";
          overlay.style.right = formWrap.clientWidth > 0 ? "50%" : "1rem";
          overlay.style.transform = formWrap.clientWidth > 0 ? "translateX(-50%)" : "none";
          overlay.style.width = formWrap.clientWidth > 0 ? "min(100% - 2rem, 64rem)" : "calc(100% - 2rem)";
        }
      }

      if (pickBtn) {
        pickBtn.onclick = () => {
          if (typeof window.openNewConversationModal === "function") window.openNewConversationModal("template", E164);
        };
      }
    }

    async function pollWindowStatus(e164) {
      const data = await fetchWindow(e164);
      if (data) {
        lastWindowState = {
          inside_24h_window: !!data.inside_24h_window,
          window_closes_at_unix_ts: data.window_closes_at_unix_ts || null,
        };
        renderWindowBanner(lastWindowState.inside_24h_window, lastWindowState.window_closes_at_unix_ts);
        tickHeaderCountdown();
      }
    }

    function tickHeaderCountdown() {
      const el = document.getElementById("hdr-countdown");
      if (!el) return;
      const closes = lastWindowState.window_closes_at_unix_ts;
      if (!closes) { el.textContent = "⏱ no buyer message yet"; el.className = "text-xs font-mono font-semibold text-gray-400"; return; }
      const sec = Math.floor(closes - Date.now() / 1000);
      if (sec <= 0) { el.textContent = "⏱ window closed · templates only"; el.className = "text-xs font-mono font-semibold text-gray-500"; return; }
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = sec % 60;
      const p2 = (n) => (n < 10 ? "0" : "") + n;
      el.textContent = "⏱ " + p2(h) + ":" + p2(m) + ":" + p2(x) + " left to reply";
      el.className = "text-xs font-mono font-semibold " + (sec < 3600 ? "text-red-600" : sec < 6 * 3600 ? "text-amber-600" : "text-green-700");
    }
    setInterval(tickHeaderCountdown, 1000);

    pollWindowStatus(E164);
    windowPollTimer = setInterval(() => pollWindowStatus(E164), 60 * 1000);

    // ---------- appendTemplateBubble helper ----------
    window.appendTemplateBubble = function (e164, templateName, language, params, sentBy) {
      const tStack = document.getElementById("message-stack");
      const direction = (sentBy === "ai" || sentBy === "AI") ? "ai" : "human";
      const paramStr = params ? Object.values(params).join(" · ") : "";
      const previewText = `[Template: ${templateName} (${language})]${paramStr ? "\n" + paramStr : ""}`;
      const labelDiv = (direction === "human")
        ? `<div class="text-[10px] font-bold uppercase tracking-wider text-blue-700/80 mb-0.5 flex items-center justify-between">
             <span>${getCtx().admin_name || "Petrobind Admin"} · human</span>
             <span class="template-badge not-italic normal-case ml-2">📋 Template: ${escapeHtml(templateName)} (${escapeHtml(language)})</span>
           </div>`
        : `<div class="text-[10px] font-bold uppercase tracking-wider text-pb-greenDark/80 mb-0.5 flex items-center justify-between">
             <span>AI · Jane Tan</span>
             <span class="template-badge not-italic normal-case ml-2">📋 Template: ${escapeHtml(templateName)} (${escapeHtml(language)})</span>
           </div>`;

      if (typeof appendBubble === "function") {
        appendBubble({
          direction,
          text: previewText,
          held_by: (direction === "human" ? (getCtx().admin_name || "Petrobind Admin") : "AI · Jane Tan"),
          created_at: new Date().toISOString(),
          _optimistic: true,
        });
        const lastBubble = tStack ? tStack.querySelector(".message-bubble:last-child") : null;
        if (lastBubble) {
          lastBubble.classList.add("template-bubble");
          const firstDiv = lastBubble.querySelector("div");
          if (firstDiv && !firstDiv.querySelector(".template-badge")) {
            firstDiv.innerHTML = labelDiv.replace(/^<div[^>]*>|<\/div>$/g, "");
          }
        }
        return true;
      }

      if (!tStack) return false;
      const wrap = document.createElement("div");
      wrap.className = "flex justify-end";
      wrap.setAttribute("data-optimistic", "1");
      const bubble = document.createElement("div");
      const cls = (direction === "ai")
        ? "bg-pb-aiBubble text-gray-900 rounded-tr-sm border border-green-100"
        : "bg-pb-humanBubble text-gray-900 rounded-tr-sm border border-blue-100";
      bubble.className =
        "message-bubble template-bubble max-w-[80%] md:max-w-[70%] px-3.5 py-2 rounded-2xl text-[15px] leading-relaxed shadow-sm " + cls;
      const timeStr = new Date().toISOString().replace("T", " ").slice(0, 19);
      bubble.innerHTML =
        labelDiv +
        `<div class="whitespace-pre-wrap break-words">${escapeHtml(previewText)}</div>` +
        `<div class="mt-1 flex items-center justify-end gap-2">
           <div class="text-[10px] text-gray-400"><time>${timeStr}</time></div>
         </div>`;
      wrap.appendChild(bubble);
      tStack.appendChild(wrap);
      tStack.scrollTop = tStack.scrollHeight;
      return true;
    };
  }

  // ==================================================================
  // FR8: +New Conversation modal (works on chat_list + chat_thread)
  // ==================================================================

  let _cachedTemplates = null;

  function getInboxBearer() {
    return sessionStorage.getItem("_inbox_bearer") || "";
  }
  window.getInboxBearer = getInboxBearer;

  function showConvInfoBanner(msg, kind) {
    const info = document.getElementById("new-conv-info");
    if (!info) return;
    info.classList.remove("hidden");
    const kls = kind === "error" ? "bg-red-50 border-red-200 text-red-800"
                : kind === "warn" ? "bg-amber-50 border-amber-200 text-amber-800"
                : "bg-blue-50 border-blue-200 text-blue-800";
    info.className = `mx-5 mt-3 p-2.5 rounded border text-xs ${kls}`;
    info.textContent = msg;
  }
  function hideConvInfoBanner() {
    const info = document.getElementById("new-conv-info");
    if (info) info.classList.add("hidden");
  }

  // New conversations are template-only: WhatsApp lets a business message first only with an approved template.
  let _tplList = [];
  let _tplError = "";
  const tplEsc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const digitsOnly = (v) => String(v || "").replace(/\D+/g, "");
  const tplLang = (t) => (t && t.language && (typeof t.language === "string" ? t.language : t.language.code)) || "en_US";
  const tplBody = (t) => { const b = ((t && t.components) || []).find(c => c && c.type === "BODY"); return b || { text: "", parameters: [] }; };
  const tplVars = (t) => (tplBody(t).parameters || []).map(String);
  const currentTemplate = () => { const sel = document.getElementById("template-select"); return sel && sel.value !== "" ? _tplList[Number(sel.value)] : null; };

  function renderTemplatePreview() {
    const t = currentTemplate();
    const box = document.getElementById("tpl-summary");
    if (!t || !box) return;
    let text = tplBody(t).text || "";
    document.querySelectorAll("#template-params .tpl-param").forEach(inp => {
      const v = (inp.value || "").trim();
      text = text.split("{{" + inp.getAttribute("data-param-name") + "}}").join(v || "{{" + inp.getAttribute("data-param-name") + "}}");
    });
    box.textContent = text || "—";
  }

  function buildTemplateParamInputs(t) {
    const container = document.getElementById("template-params");
    if (!container) return;
    const vars = t ? tplVars(t) : [];
    if (!t || vars.length === 0) {
      container.innerHTML = t ? `<div class="text-xs text-gray-500 italic p-2 rounded bg-gray-50 border border-gray-100">This template has no variables — just enter the number above.</div>` : "";
      return;
    }
    container.innerHTML = `<div class="text-xs font-medium text-gray-700">Fill in the template variables</div>` + vars.map(n =>
      `<div><label class="block text-xs font-medium text-gray-600 mb-1">Variable {{${tplEsc(n)}}}</label>
         <input type="text" data-param-name="${tplEsc(n)}" class="tpl-param w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-pb-green/30 focus:border-pb-green"></div>`).join("");
    container.querySelectorAll(".tpl-param").forEach(inp => inp.addEventListener("input", renderTemplatePreview));
  }

  function populateTemplateDropdown() {
    const sel = document.getElementById("template-select");
    if (!sel) return;
    sel.innerHTML = "";
    const opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = _tplError ? "— Could not load templates —" : (_tplList.length ? "— Pick a template —" : "— No approved templates found —");
    sel.appendChild(opt0);
    _tplList.forEach((t, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = `${t.name} (${t.category || "—"}, ${tplLang(t)})` + (t.supported === false ? " — can't be sent here" : "");
      if (t.supported === false) opt.disabled = true;
      sel.appendChild(opt);
    });
    onTemplateChosen();
  }

  function onTemplateChosen() {
    const t = currentTemplate();
    const infoBox = document.getElementById("template-info");
    if (!t) { if (infoBox) infoBox.classList.add("hidden"); buildTemplateParamInputs(null); return; }
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("tpl-category", t.category || "—");
    set("tpl-language", tplLang(t));
    if (infoBox) infoBox.classList.remove("hidden");
    buildTemplateParamInputs(t);
    renderTemplatePreview();
  }

  async function ensureTemplates(force) {
    if (_tplList.length && !force) return;
    _tplError = "";
    try {
      const resp = await inboxFetch("/api/inbox/templates" + (force ? "?refresh=1" : ""));
      const data = await resp.json().catch(() => null);
      if (!resp.ok) { _tplError = (data && data.detail) || ("Could not load templates (error " + resp.status + ")"); _tplList = []; return; }
      _tplList = (data && data.templates) || [];
    } catch (err) {
      _tplError = "Could not load templates: " + (err && err.message ? err.message : err);
      _tplList = [];
    }
  }

  window.openNewConversationModal = async function (_initialTab, prefille164) {
    const modal = document.getElementById("new-conversation-modal");
    if (!modal) {
      // inside the split-view thread frame the modal lives in the parent page
      try { if (window.parent && window.parent !== window && typeof window.parent.openNewConversationModal === "function") { return window.parent.openNewConversationModal(_initialTab, prefille164); } } catch (_) {}
      alert("New Conversation modal not present on this page. Please go to the Chats list.");
      return;
    }
    hideConvInfoBanner();
    const ts = document.getElementById("template-status");
    if (ts) ts.textContent = "";
    if (prefille164) { const te = document.getElementById("template-e164"); if (te) te.value = prefille164; }

    if (!modal._listenersAttached) {
      modal._listenersAttached = true;
      const closeBtn = document.getElementById("modal-close-btn");
      if (closeBtn) closeBtn.addEventListener("click", () => modal.close());
      const tc = document.getElementById("btn-template-cancel");
      if (tc) tc.addEventListener("click", () => modal.close());
      const sel = document.getElementById("template-select");
      if (sel) sel.addEventListener("change", () => { hideConvInfoBanner(); onTemplateChosen(); });

      const refreshBtn = document.getElementById("btn-template-refresh");
      if (refreshBtn) refreshBtn.addEventListener("click", async () => {
        refreshBtn.disabled = true; if (ts) ts.textContent = "Syncing templates from Meta…";
        await ensureTemplates(true);
        populateTemplateDropdown();
        refreshBtn.disabled = false;
        if (_tplError) { showConvInfoBanner(_tplError, "error"); if (ts) ts.textContent = ""; }
        else { hideConvInfoBanner(); if (ts) ts.textContent = `✓ ${_tplList.length} approved template${_tplList.length === 1 ? "" : "s"} synced`; }
      });

      const sendTpl = document.getElementById("btn-template-send");
      if (sendTpl) sendTpl.addEventListener("click", async () => {
        const einp = document.getElementById("template-e164");
        const t = currentTemplate();
        const e164 = digitsOnly(einp ? einp.value : "");
        if (!t) { showConvInfoBanner("Pick a template first.", "warn"); return; }
        if (e164.length < 8) { showConvInfoBanner("Enter the WhatsApp number with its country code, e.g. +65 9123 4567.", "warn"); return; }
        const paramMap = {};
        let missing = false;
        document.querySelectorAll("#template-params .tpl-param").forEach(inp => {
          const v = (inp.value || "").trim();
          if (!v) missing = true;
          paramMap[inp.getAttribute("data-param-name")] = v;
        });
        if (missing) { showConvInfoBanner("Fill in every template variable.", "warn"); return; }
        hideConvInfoBanner();
        sendTpl.disabled = true;
        if (ts) ts.textContent = "Sending template…";
        try {
          const resp = await inboxFetch("/api/inbox/send-template", {
            method: "POST",
            body: { template_name: t.name, language: tplLang(t), e164, params: paramMap },
          });
          const data = await resp.json().catch(() => ({}));
          if (resp.ok && data && data.success) {
            if (ts) ts.textContent = "✓ Template sent";
            if (typeof window.appendTemplateBubble === "function") {
              window.appendTemplateBubble(e164, t.name, tplLang(t), paramMap, "human");
            }
            modal.close();
            if (!document.getElementById("message-stack")) {
              // the chat was saved before this answer came back, so it is in the list now: open it
              window.location.hash = encodeURIComponent(e164);
              window.location.reload();
            }
          } else {
            if (ts) ts.textContent = "";
            showConvInfoBanner(`Not sent: ${(data && (data.detail || data.error)) || ("error " + resp.status)}`, "error");
          }
        } catch (err) {
          if (ts) ts.textContent = "";
          showConvInfoBanner(`Network error: ${err && err.message ? err.message : err}`, "error");
        } finally {
          sendTpl.disabled = false;
        }
      });

      modal.addEventListener("click", (ev) => {
        const rect = modal.getBoundingClientRect();
        const inDialog = (rect.top <= ev.clientY && ev.clientY <= rect.top + rect.height &&
                          rect.left <= ev.clientX && ev.clientX <= rect.left + rect.width);
        if (!inDialog) modal.close();
      });
    }

    if (typeof modal.showModal === "function") {
      if (!modal.open) modal.showModal();
    } else {
      modal.setAttribute("open", "");
      modal.style.display = "block";
    }
    await ensureTemplates(false);
    populateTemplateDropdown();
    if (_tplError) showConvInfoBanner(_tplError, "error");
  };

  // Make fetchWindow available globally for any page that needs it
  if (typeof fetchWindow === "function") window.fetchWindow = fetchWindow;
})();
