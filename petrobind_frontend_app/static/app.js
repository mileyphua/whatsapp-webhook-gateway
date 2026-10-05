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
    //           alive (30 min TTL, renewed every 30s while this chat is open and
    //           surviving chat switching); if nobody renews it, AI resumes by itself.
    // ------------------------------------------------------------------
    const HUMAN_TTL = 1800;
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
      if (lockedBy) {
        setClaimBannerVisible(true, lockedBy);
        setClaimStatus(`Another admin (${lockedBy.held_by || "unknown"}) is replying. Read-only.`, { sendEnabled: false });
      } else if (humanMode) {
        setClaimBannerVisible(false);
        setClaimStatus("You are replying. AI is paused for this chat. Switch back to AI when done.", { sendEnabled: true });
      } else {
        setClaimBannerVisible(false);
        setClaimStatus("AI is answering this chat (RAG + LLM). Switch to Human to take over.", { sendEnabled: false });
      }
      const ta = document.getElementById("human-text-input");
      if (ta && !lockedBy && !humanMode) ta.placeholder = "AI is replying. Switch to Human to type a reply.";
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
    const bannerDismissBtn = document.getElementById("claim-banner-dismiss");
    if (bannerDismissBtn) bannerDismissBtn.addEventListener("click", () => setClaimBannerVisible(false));

    // Initial state comes from the server (default = AI); then renew / re-check every 30s.
    applyMode();
    loadMode();
    setInterval(function () { if (humanMode) goHuman(); else loadMode(); }, 30 * 1000);

    function setClaimStatus(text, { sendEnabled = true } = {}) {
      const el = document.getElementById("claim-status-text");
      const sendBtn = document.getElementById("btn-send-submit");
      const textarea = document.getElementById("human-text-input");
      if (el) el.textContent = text;
      if (sendBtn) {
        sendBtn.disabled = !sendEnabled;
        sendBtn.title = sendEnabled ? "" : "Switch to Human mode to reply.";
      }
      if (textarea) {
        textarea.disabled = !sendEnabled;
        if (sendEnabled) textarea.placeholder = `Type your reply to ${E164}… (Shift+Enter for newline, Enter to send)`;
      }
    }

    function setClaimBannerVisible(visible, heldInfo = null) {
      const banner = document.getElementById("claim-banner");
      const txt = document.getElementById("claim-banner-text");
      if (!banner) return;
      banner.classList.toggle("hidden", !visible);
      if (visible && heldInfo) {
        txt.textContent =
          `⚠ Another admin (${heldInfo.held_by || "unknown"}) is replying to this chat` +
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

    // Populate the send form hx-headers with a real bearer. The render-time
    // placeholder __INBOX_ADMIN_TOKEN_PLACEHOLDER__ can't contain the secret
    // since it'd be in view-source. HTMX lets us override at submit time.
    // Instead we listen to submit and convert HTMX form to manual fetch so we
    // can inject the bearer + session id headers cleanly, append bubble to UI.
    if (sendForm) {
      sendForm.setAttribute("hx-post", "");          // disable HTMX native
      sendForm.removeAttribute("hx-swap");
      sendForm.removeAttribute("hx-headers");
      sendForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        if (!textarea) return;
        const text = (textarea.value || "").trim();
        if (!text) return;
        if (sendBtn && sendBtn.disabled) {
          // claimed by other → flash
          const banner = document.getElementById("claim-banner");
          if (banner) { banner.classList.remove("hidden"); banner.animate(
            [{ transform: "translateY(-8px)" }, { transform: "translateY(0)" }],
            { duration: 250, easing: "ease-out" }); }
          return;
        }
        // Append optimistic bubble
        appendBubble({ direction: "human", text, held_by: ctx.admin_name, created_at: new Date().toISOString(), _optimistic: true });
        textarea.value = "";
        textarea.focus();
        try {
          const resp = await inboxFetch(`/api/inbox/chats/${encodeURIComponent(E164)}/messages`, {
            method: "POST",
            body: { text, reply_to_wamid: (ctx.last_buyer_wamid || null) },
          });
          const data = await resp.json().catch(() => ({}));
          if (resp.ok && data && data.success) {
            // all good — bubble already there
            if (sugMetaEl) sugMetaEl.textContent = `✓ Human reply sent.`;
          } else {
            // failure → append a small error bubble
            appendBubble({ direction: "system",
              text: `[Human send FAILED: ${(data && data.detail) || resp.status || "unknown"}]`,
              created_at: new Date().toISOString(), _optimistic: true, errored: true });
          }
        } catch (err) {
          appendBubble({ direction: "system",
            text: `[Human send FAILED (network): ${err && err.message ? err.message : err}]`,
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
      btnAccept.addEventListener("click", function () {
        if (!currentSuggestionText || !textarea) return;
        textarea.value = currentSuggestionText;
        if (sendForm) sendForm.requestSubmit();
      });
    }
    if (btnEdit) {
      btnEdit.addEventListener("click", function () {
        if (!currentSuggestionText || !textarea) return;
        textarea.value = currentSuggestionText;
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
    function appendBubble({ direction, text, held_by = null, created_at = null, _optimistic = false, errored = false, id = null, wamid = "" }) {
      if (!stack) return;
      const wrap = document.createElement("div");
      if (id != null) wrap.setAttribute("data-msg-id", String(id));
      wrap.setAttribute("data-direction", direction || "");
      wrap.setAttribute("data-text", text || "");
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
      bubble.innerHTML =
        label +
        `<div class="whitespace-pre-wrap break-words">${escapeHtml(text || "")}</div>` +
        erroredHtml +
        `<div class="mt-1 flex items-center justify-end gap-2">
           <div class="text-[10px] text-gray-400"><time>${timeStr}</time></div>
         </div>`;
      wrap.appendChild(bubble);
      stack.appendChild(wrap);
      stack.scrollTop = stack.scrollHeight;
      if (direction === "ai") decorateFeedback(wrap);
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
      appendBubble({ direction: m.direction, text: m.text, held_by: m.held_by, created_at: m.created_at, id: m.id, wamid: m.wamid || "" });
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


    // ---------- Feedback on AI replies: 👍 / 👎 (+ tags, note, better reply) feeds the Learning page ----------
    const FB_TAGS = ["Inaccurate", "Too long", "Sounds like a bot", "Repeats itself", "Wrong tone", "Should have handed to a human"];
    const fbState = {};   // ai_text -> "up" | "down"
    function mk(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
    function precedingBuyerText(wrap) {
      let n = wrap.previousElementSibling;
      while (n) {
        if (n.getAttribute && n.getAttribute("data-direction") === "buyer") { const t = n.querySelector(".whitespace-pre-wrap"); return t ? t.textContent : ""; }
        n = n.previousElementSibling;
      }
      return "";
    }
    function paintFeedback(wrap, rating) {
      const st = wrap.querySelector(".fb-status"), up = wrap.querySelector(".fb-up"), dn = wrap.querySelector(".fb-down");
      if (!st) return;
      up.setAttribute("aria-pressed", String(rating === "up")); dn.setAttribute("aria-pressed", String(rating === "down"));
      up.className = "fb-up px-1.5 py-0.5 rounded " + (rating === "up" ? "bg-green-600 text-white" : "hover:bg-black/5");
      dn.className = "fb-down px-1.5 py-0.5 rounded " + (rating === "down" ? "bg-red-600 text-white" : "hover:bg-black/5");
      st.textContent = rating === "up" ? "Marked good" : rating === "down" ? "Marked for improvement" : "";
    }
    async function sendFeedback(wrap, payload) {
      const text = wrap.querySelector(".whitespace-pre-wrap").textContent;
      const body = Object.assign({ e164: E164, ai_text: text, buyer_text: precedingBuyerText(wrap) }, payload);
      const st = wrap.querySelector(".fb-status");
      try {
        const r = await inboxFetch("/api/inbox/feedback", { method: "POST", body: body });
        if (!r.ok) throw new Error("HTTP " + r.status);
        fbState[text] = payload.rating; paintFeedback(wrap, payload.rating); return true;
      } catch (e) { if (st) st.textContent = "Could not save feedback"; return false; }
    }
    function openFeedbackForm(wrap) {
      if (wrap.querySelector(".fb-form")) return;
      const form = mk("div", "fb-form mt-2 pt-2 border-t border-black/10 space-y-1.5");
      form.appendChild(mk("div", "text-[11px] font-semibold", "What was wrong?"));
      const chips = mk("div", "flex flex-wrap gap-1");
      const picked = new Set();
      FB_TAGS.forEach(function (t) {
        const b = mk("button", "px-2 py-0.5 rounded-full border border-black/20 text-[11px] bg-white/70", t); b.type = "button"; b.setAttribute("aria-pressed", "false");
        b.addEventListener("click", function () { if (picked.has(t)) { picked.delete(t); b.setAttribute("aria-pressed", "false"); b.className = "px-2 py-0.5 rounded-full border border-black/20 text-[11px] bg-white/70"; } else { picked.add(t); b.setAttribute("aria-pressed", "true"); b.className = "px-2 py-0.5 rounded-full border border-red-600 text-[11px] bg-red-600 text-white"; } });
        chips.appendChild(b);
      });
      const note = mk("textarea", "w-full rounded border border-black/20 bg-white px-2 py-1 text-xs"); note.rows = 2; note.maxLength = 1000; note.placeholder = "Tell it what to do differently (optional)"; note.setAttribute("aria-label", "What was wrong");
      const better = mk("textarea", "w-full rounded border border-black/20 bg-white px-2 py-1 text-xs"); better.rows = 2; better.maxLength = 1500; better.placeholder = "How would you have replied? (optional, the best way to teach it)"; better.setAttribute("aria-label", "A better reply");
      const row = mk("div", "flex justify-end gap-2");
      const cancel = mk("button", "px-2.5 py-1 rounded border border-black/20 text-xs bg-white", "Cancel"); cancel.type = "button";
      const save = mk("button", "px-2.5 py-1 rounded bg-red-600 text-white text-xs font-medium", "Send feedback"); save.type = "button";
      cancel.addEventListener("click", function () { form.remove(); if (!fbState[wrap.querySelector(".whitespace-pre-wrap").textContent]) paintFeedback(wrap, null); });
      save.addEventListener("click", async function () {
        save.disabled = true;
        const ok = await sendFeedback(wrap, { rating: "down", tags: Array.from(picked), note: note.value.trim(), better_reply: better.value.trim() });
        if (ok) form.remove(); else save.disabled = false;
      });
      row.appendChild(cancel); row.appendChild(save);
      [chips, note, better, row].forEach(function (n) { form.appendChild(n); });
      wrap.querySelector(".message-bubble").appendChild(form);
      note.focus();
    }
    function decorateFeedback(wrap) {
      if (!wrap || wrap.querySelector(".fb-row") || wrap.getAttribute("data-direction") !== "ai") return;
      const bubble = wrap.querySelector(".message-bubble"), textEl = wrap.querySelector(".whitespace-pre-wrap");
      if (!bubble || !textEl || !textEl.textContent.trim()) return;
      const row = mk("div", "fb-row mt-1.5 flex items-center gap-1 text-xs text-gray-600");
      const up = mk("button", "fb-up px-1.5 py-0.5 rounded hover:bg-black/5", "👍"); up.type = "button"; up.setAttribute("aria-label", "Good reply"); up.title = "Good reply";
      const dn = mk("button", "fb-down px-1.5 py-0.5 rounded hover:bg-black/5", "👎"); dn.type = "button"; dn.setAttribute("aria-label", "Needs improvement"); dn.title = "Needs improvement: tell it why";
      const st = mk("span", "fb-status ml-1 text-[11px]"); st.setAttribute("aria-live", "polite");
      up.addEventListener("click", function () { const f = wrap.querySelector(".fb-form"); if (f) f.remove(); sendFeedback(wrap, { rating: "up", tags: [], note: "", better_reply: "" }); });
      dn.addEventListener("click", function () { openFeedbackForm(wrap); });
      row.appendChild(mk("span", "text-[11px] text-gray-500 mr-1", "Was this reply good?")); row.appendChild(up); row.appendChild(dn); row.appendChild(st);
      bubble.appendChild(row);
      if (fbState[textEl.textContent]) paintFeedback(wrap, fbState[textEl.textContent]);
    }
    function decorateAllFeedback() { stack.querySelectorAll('[data-direction="ai"]').forEach(decorateFeedback); }
    (async function loadFeedbackState() {
      try {
        const r = await inboxFetch("/api/inbox/feedback?e164=" + encodeURIComponent(E164));
        if (r.ok) { (await r.json()).feedback.forEach(function (f) { fbState[f.ai_text] = f.rating; }); }
      } catch (_) {}
      decorateAllFeedback();
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
        const canType = humanMode && !lockedBy;   // typing only in Human mode
        if (textarea) { textarea.disabled = !canType; textarea.style.opacity = "1"; }
        if (sendBtn) sendBtn.disabled = !canType;
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

  function switchModalTab(tab) {
    const tf = document.getElementById("tab-freeform");
    const tt = document.getElementById("tab-template");
    const pf = document.getElementById("tab-pane-freeform");
    const pt = document.getElementById("tab-pane-template");
    if (!tf || !tt) return;
    const isFree = tab === "freeform";
    tf.className = "tab-btn px-4 py-2 text-sm font-medium border-b-2 " + (isFree ? "border-pb-navy text-pb-navy" : "border-transparent text-gray-500 hover:text-gray-700");
    tt.className = "tab-btn px-4 py-2 text-sm font-medium border-b-2 " + (isFree ? "border-transparent text-gray-500 hover:text-gray-700" : "border-pb-navy text-pb-navy");
    if (pf) pf.classList.toggle("hidden", !isFree);
    if (pt) pt.classList.toggle("hidden", isFree);
  }

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

  function extractTemplateParams(template) {
    const params = [];
    try {
      const components = (template && template.components) || [];
      const bodyComp = components.find(c => c && c.type === "BODY");
      if (bodyComp && bodyComp.parameters && Array.isArray(bodyComp.parameters)) {
        bodyComp.parameters.forEach((p, i) => {
          params.push({
            index: i,
            name: p && p.key ? p.key : `${i + 1}`,
            type: (p && p.type) || "text",
            label: p && p.key ? p.key : `Param ${i + 1}`,
          });
        });
      }
      const bodyText = bodyComp && bodyComp.text ? bodyComp.text : "";
      const placeholders = bodyText.match(/\{\{(\d+)\}\}/g);
      if (placeholders && placeholders.length > params.length) {
        for (let i = params.length; i < placeholders.length; i++) {
          params.push({ index: i, name: `${i + 1}`, type: "text", label: `Param ${i + 1}` });
        }
      }
    } catch (_) {}
    return params;
  }

  function buildTemplateParamInputs(template) {
    const container = document.getElementById("template-params");
    if (!container) return;
    const params = extractTemplateParams(template);
    if (params.length === 0) {
      container.innerHTML = `<div class="text-xs text-gray-500 italic p-2 rounded bg-gray-50 border border-gray-100">This template has no parameters — just fill in the recipient number above.</div>`;
      return;
    }
    let html = `<div class="space-y-2">
                  <div class="text-xs font-medium text-gray-700">Template parameters</div>`;
    params.forEach(p => {
      html += `<div>
                 <label class="block text-xs font-medium text-gray-600 mb-1">${escapeHtml(p.label)} <span class="text-gray-400 font-normal">({{${p.name}}})</span></label>
                 <input type="text" name="param_${escapeHtml(p.name)}" data-param-index="${p.index}" data-param-name="${escapeHtml(p.name)}"
                        class="tpl-param w-full rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-pb-green/30 focus:border-pb-green">
               </div>`;
    });
    html += "</div>";
    container.innerHTML = html;
  }

  function populateTemplateDropdown(templates) {
    const sel = document.getElementById("template-select");
    if (!sel) return;
    sel.innerHTML = "";
    if (!templates || templates.length === 0) {
      sel.innerHTML = `<option value="">— No templates available —</option>`;
      return;
    }
    const opt0 = document.createElement("option");
    opt0.value = ""; opt0.textContent = "— Pick a template —";
    sel.appendChild(opt0);
    templates.forEach(t => {
      const name = t.name || "(unnamed)";
      const cat = t.category || "UTILITY";
      const lang = (t.language && (typeof t.language === "string" ? t.language : t.language.code)) || "en";
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = `${name} (${cat}, ${lang})`;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", function () {
      const val = sel.value;
      const infoBox = document.getElementById("template-info");
      if (!val) { if (infoBox) infoBox.classList.add("hidden"); buildTemplateParamInputs(null); return; }
      const t = templates.find(x => x.name === val);
      if (!t) return;
      const cat = t.category || "UTILITY";
      const lang = (t.language && (typeof t.language === "string" ? t.language : t.language.code)) || "en";
      const comps = (t.components || []).map(c => c.type || "?").join(", ");
      const tc = document.getElementById("tpl-category");
      const tl = document.getElementById("tpl-language");
      const ts = document.getElementById("tpl-summary");
      if (tc) tc.textContent = cat;
      if (tl) tl.textContent = lang;
      if (ts) ts.textContent = comps || "—";
      if (infoBox) infoBox.classList.remove("hidden");
      buildTemplateParamInputs(t);
    });
  }

  async function ensureTemplates(force) {
    if (_cachedTemplates && !force) return _cachedTemplates;
    try {
      const resp = await inboxFetch("/api/inbox/templates");
      if (!resp.ok) return null;
      const data = await resp.json().catch(() => null);
      const list = (data && data.templates) || (data && Array.isArray(data) ? data : []);
      _cachedTemplates = list;
      return list;
    } catch (_) {
      return null;
    }
  }

  window.openNewConversationModal = async function (initialTab, prefille164) {
    const modal = document.getElementById("new-conversation-modal");
    if (!modal) {
      alert("New Conversation modal not present on this page. Please go to the Chats list.");
      return;
    }
    switchModalTab(initialTab || "freeform");
    hideConvInfoBanner();
    const fs = document.getElementById("freeform-status");
    const ts = document.getElementById("template-status");
    if (fs) fs.textContent = "";
    if (ts) ts.textContent = "";

    if (prefille164) {
      const fe = document.getElementById("freeform-e164");
      const te = document.getElementById("template-e164");
      if (fe) fe.value = prefille164;
      if (te) te.value = prefille164;
    }

    if (!modal._listenersAttached) {
      modal._listenersAttached = true;
      const closeBtn = document.getElementById("modal-close-btn");
      if (closeBtn) closeBtn.addEventListener("click", () => modal.close());
      const fc = document.getElementById("btn-freeform-cancel");
      if (fc) fc.addEventListener("click", () => modal.close());
      const tc = document.getElementById("btn-template-cancel");
      if (tc) tc.addEventListener("click", () => modal.close());
      const tabFree = document.getElementById("tab-freeform");
      const tabTpl = document.getElementById("tab-template");
      if (tabFree) tabFree.addEventListener("click", () => { switchModalTab("freeform"); hideConvInfoBanner(); });
      if (tabTpl) tabTpl.addEventListener("click", () => { switchModalTab("template"); hideConvInfoBanner(); });

      const cw = document.getElementById("btn-check-window");
      if (cw) cw.addEventListener("click", async () => {
        const inp = document.getElementById("freeform-e164");
        const e164 = inp ? (inp.value || "").trim() : "";
        if (!e164) { showConvInfoBanner("Enter a WhatsApp number first.", "warn"); return; }
        const d = await fetchWindow(e164);
        if (!d) { showConvInfoBanner("Could not check window for this number.", "error"); return; }
        if (d.inside_24h_window) {
          const s = d.window_closes_at_unix_ts ? Math.max(0, d.window_closes_at_unix_ts - Math.floor(Date.now()/1000)) : 0;
          showConvInfoBanner(`✅ Inside 24h window — closes in ${s ? formatDuration(s) : "~24h"}. Freeform send is permitted.`, "ok");
        } else {
          showConvInfoBanner("⚠ Outside 24h window — freeform will be rejected. Switch to Approved Meta Template tab.", "warn");
          setTimeout(() => switchModalTab("template"), 700);
        }
      });

      const sendFree = document.getElementById("btn-freeform-send");
      if (sendFree) sendFree.addEventListener("click", async () => {
        const einp = document.getElementById("freeform-e164");
        const tinp = document.getElementById("freeform-text");
        const e164 = einp ? (einp.value || "").trim() : "";
        const text = tinp ? (tinp.value || "").trim() : "";
        if (!e164) { showConvInfoBanner("WhatsApp number required.", "warn"); return; }
        if (!text) { showConvInfoBanner("Message text required.", "warn"); return; }
        sendFree.disabled = true;
        if (fs) fs.textContent = "Sending…";
        try {
          const resp = await inboxFetch("/api/inbox/new-conversation", {
            method: "POST",
            body: { mode: "auto", e164, text },
          });
          const data = await resp.json().catch(() => ({}));
          if (resp.status === 422 && data && data.inside_24h_window === false) {
            showConvInfoBanner("Outside 24h window — please pick a template instead.", "warn");
            const te164 = document.getElementById("template-e164");
            if (te164) te164.value = e164;
            switchModalTab("template");
          } else if (resp.ok && data && data.success) {
            if (fs) fs.textContent = "✓ Sent";
            const tplE164 = data.e164 || e164;
            modal.close();
            window.location.href = `/inbox/chats#${encodeURIComponent(tplE164)}`; if (window.location.pathname === "/inbox/chats") window.location.reload();
          } else {
            showConvInfoBanner(`Failed: ${data && data.detail ? data.detail : resp.status}`, "error");
          }
        } catch (err) {
          showConvInfoBanner(`Network error: ${err && err.message ? err.message : err}`, "error");
        } finally {
          sendFree.disabled = false;
        }
      });

      const sendTpl = document.getElementById("btn-template-send");
      if (sendTpl) sendTpl.addEventListener("click", async () => {
        const sel = document.getElementById("template-select");
        const einp = document.getElementById("template-e164");
        const templateName = sel ? sel.value : "";
        const e164 = einp ? (einp.value || "").trim() : "";
        if (!templateName) { showConvInfoBanner("Pick a template first.", "warn"); return; }
        if (!e164) { showConvInfoBanner("WhatsApp number required.", "warn"); return; }
        const tpl = (_cachedTemplates || []).find(t => t.name === templateName);
        const lang = (tpl && tpl.language && (typeof tpl.language === "string" ? tpl.language : tpl.language.code)) || "en";
        const paramMap = {};
        const inputs = document.querySelectorAll("#template-params .tpl-param");
        inputs.forEach(inp => {
          const name = inp.getAttribute("data-param-name") || inp.name || "";
          const key = name.replace(/^param_/, "");
          paramMap[key] = (inp.value || "").trim();
        });
        sendTpl.disabled = true;
        if (ts) ts.textContent = "Sending template…";
        try {
          const resp = await inboxFetch("/api/inbox/send-template", {
            method: "POST",
            body: { template_name: templateName, language: lang, e164, params: paramMap },
          });
          const data = await resp.json().catch(() => ({}));
          if (resp.ok && data && data.success) {
            if (ts) ts.textContent = "✓ Template sent";
            if (typeof window.appendTemplateBubble === "function") {
              window.appendTemplateBubble(e164, templateName, lang, paramMap, "human");
            }
            modal.close();
            if (document.getElementById("message-stack")) {
            } else {
              window.location.href = `/inbox/chats#${encodeURIComponent(e164)}`; if (window.location.pathname === "/inbox/chats") window.location.reload();
            }
          } else {
            showConvInfoBanner(`Failed: ${data && data.detail ? data.detail : resp.status}`, "error");
          }
        } catch (err) {
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

    const tpl = await ensureTemplates(false);
    populateTemplateDropdown(tpl || []);

    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.setAttribute("open", "");
      modal.style.display = "block";
    }
  };

  // Make fetchWindow available globally for any page that needs it
  if (typeof fetchWindow === "function") window.fetchWindow = fetchWindow;
})();
