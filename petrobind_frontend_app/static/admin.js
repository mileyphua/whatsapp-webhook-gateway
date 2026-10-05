/* ======================================================
   PETROBIND OPS CONSOLE ADMIN.JS
   - Visualizes backend state via /api/inbox/admin/dashboard
   - Triggers admin actions (redis scan, evict cache, cron runs)
   - Same auth pattern as app.js (sessionStorage._inbox_bearer
     captured on login.html form submit).
   ====================================================== */
(function () {
  "use strict";
  const API_BASE = "/api/inbox/admin";
  const REFRESH_MS = 10000;

  // -------- helpers ---------------------------------------------------------
  function getBearer() {
    return sessionStorage.getItem("_inbox_bearer") || "";
  }
  function setBearer(tok) {
    sessionStorage.setItem("_inbox_bearer", tok || "");
  }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function fmtAgo(secAgo) {
    if (secAgo == null || isNaN(+secAgo)) return "—";
    const s = Math.max(0, Math.floor(+secAgo));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s/60) + "m " + (s%60) + "s";
    if (s < 86400) return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m";
    return Math.floor(s/86400) + "d " + Math.floor((s%86400)/3600) + "h";
  }
  function fmtIso(iso) {
    if (!iso) return "—";
    return window.PBTime.format(iso, { seconds: true }) + " GMT+8";
  }
  function tabnum(v) { return '<span class="tabnum">' + String(v) + "</span>"; }
  function $(sel) { return document.querySelector(sel); }
  function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

  async function adminFetch(path, opts) {
    opts = opts || {};
    const token = getBearer();
    const headers = new Headers(opts.headers || {});
    if (token) headers.set("Authorization", "Bearer " + token);
    if (opts.body != null && !(opts.body instanceof FormData) && (opts.json !== false)) {
      if (typeof opts.body === "string") headers.set("Content-Type", "application/json");
      else { headers.set("Content-Type", "application/json"); opts.body = JSON.stringify(opts.body); }
    }
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body,
    });
    let payload = null;
    try { payload = await res.json(); } catch { payload = { _raw: await res.text() }; }
    return { ok: res.ok, status: res.status, payload };
  }

  // -------- toast -----------------------------------------------------------
  let toastTimer = null;
  function toast(title, body, kind) {
    const el = $("#ops-toast");
    if (!el) return;
    el.className = "ops-toast show" + (kind ? " ops-toast--" + kind : "");
    el.innerHTML =
      '<div class="ops-toast-title">' +
      (kind === "ok" ? "✅ " : kind === "bad" ? "❌ " : kind === "warn" ? "⚠️ " : "ℹ️ ") +
      escapeHtml(title) +
      "</div>" +
      '<div class="ops-toast-body">' + escapeHtml(body || "") + "</div>";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("show"); }, 4500);
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // -------- auth: bearer from sessionStorage if present, else signed cookie --
  // The server accepts either. No native prompt() (unsupported in some embedded
  // browsers); a 401 in refreshAll() redirects to /inbox/login instead.
  function promptForBearerIfMissing() {
    return Promise.resolve(true);
  }

  // -------- Panels render ---------------------------------------------------
  function renderChecks(checks) {
    const grid = $("#checks-grid");
    if (!grid) return;
    if (!checks || typeof checks !== "object") {
      grid.innerHTML = '<div class="ops-check ops-check--bad"><div class="ops-check-name">checks payload</div><div class="ops-check-val">INVALID</div></div>';
      return;
    }
    const order = [
      "whatsapp_env", "llm_ready", "rag_index_ready", "supabase_inbox",
      "inbox_login_configured", "booking_configured", "email_configured",
      "redis_scan_available", "followups_configured",
    ];
    const nameMap = {
      whatsapp_env: "WhatsApp Cloud API Env",
      llm_ready: "LLM (OpenRouter gpt-5-mini)",
      rag_index_ready: "RAG Bitumen Index",
      supabase_inbox: "Supabase Inbox Mirror",
      inbox_login_configured: "Inbox Admin Login Config",
      booking_configured: "Cal.com Booking Link",
      email_configured: "Gmail Relay (Hand-off Emails)",
      redis_scan_available: "Redis Scan Hydrate Available",
      followups_configured: "Follow-ups Cron Token Set",
    };
    const keys = Object.keys(checks).length >= 7
      ? order.concat(Object.keys(checks).filter(function (k) { return order.indexOf(k) < 0; }))
      : Object.keys(checks);
    const html = keys.map(function (k) {
      const raw = checks[k];
      const isBool = typeof raw === "boolean";
      let cls = "ops-check--warn";
      let val = raw === true ? "ONLINE" : raw === false ? "OFFLINE" : escapeHtml(String(raw));
      if (isBool) { cls = raw ? "ops-check--ok" : "ops-check--bad"; }
      else if (raw == null) { cls = "ops-check--skeleton"; val = "—"; }
      return '<div class="ops-check ' + cls + '">' +
        '<div class="ops-check-name">' + escapeHtml(nameMap[k] || k) + '</div>' +
        '<div class="ops-check-val">' + val + '</div>' +
        '</div>';
    }).join("");
    grid.innerHTML = html || '<div class="ops-check ops-check--skeleton"><div class="ops-check-name">no checks</div><div class="ops-check-val">—</div></div>';
  }

  function renderSessions(sess, proc) {
    const elInproc = $("#sessions-inproc");
    const elDb = $("#sessions-db");
    const elRedis = $("#sessions-redis");
    const elUp = $("#proc-uptime");
    if (elInproc) elInproc.textContent = sess && (typeof sess.in_process_cache === "number") ? sess.in_process_cache : "0";
    if (elDb) elDb.textContent = sess && (typeof sess.supabase_db_mirror === "number") ? sess.supabase_db_mirror : "0";
    if (elRedis) elRedis.textContent = sess && typeof sess.redis_upstash_persisted === "number" ? sess.redis_upstash_persisted : "n/a";
    if (elUp) elUp.textContent = proc && typeof proc.uptime_seconds === "number" ? fmtAgo(proc.uptime_seconds) : "—";
  }

  function renderClaims(claims) {
    const tbl = $("#claims-table > tbody");
    const chip = $("#claims-count");
    const arr = Array.isArray(claims) ? claims : [];
    if (chip) chip.textContent = arr.length + (arr.length === 1 ? " held" : " held");
    if (!tbl) return;
    if (arr.length === 0) {
      tbl.innerHTML = '<tr><td colspan="5" class="ops-empty">No human claims currently held. Claim-mutex: AI auto-replies enabled for every thread.</td></tr>';
      return;
    }
    tbl.innerHTML = arr.map(function (r) {
      return "<tr>" +
        "<td>" + escapeHtml(r.e164 || "—") + "</td>" +
        "<td>" + escapeHtml(r.held_by || "Petrobind Admin") + "</td>" +
        "<td>" + fmtIso(r.acquired_at) + "</td>" +
        "<td>" + fmtIso(r.expires_at) + "</td>" +
        "<td><span class='ops-chip'>" + escapeHtml(String(r.session_id || "").slice(-10)) + "</span></td>" +
        "</tr>";
    }).join("");
  }

  function renderSchedules(os) {
    const counts = os && os.status_counts && typeof os.status_counts === "object" ? os.status_counts : {};
    const known = ["pending", "claimed_temp", "sent", "failed", "cancelled"];
    known.forEach(function (k) {
      const el = document.querySelector("#schedule-counts .ops-sc-num:nth-of-type(" + (known.indexOf(k) + 1) + ")");
      // better: iterate in DOM order:
    });
    // Render counts by replacing innerHTML of the counts div (simpler, safe):
    const box = $("#schedule-counts");
    if (box) {
      const pal = { pending: "#f4a261", claimed_temp: "#e9c46a", sent: "#2a9d8f", failed: "#ef476f", cancelled: "#94a3b8" };
      box.innerHTML = Object.keys(counts).filter(function (k) { return !k.startsWith("_"); }).map(function (k) {
        return '<div class="ops-sc"><span class="ops-sc-name">' + escapeHtml(k) + '</span><span class="ops-sc-num tabnum" style="color:' + (pal[k] || "#fff") + ';">' + counts[k] + '</span></div>';
      }).join("") || '<div class="ops-sc"><span class="ops-sc-name">pending</span><span class="ops-sc-num">0</span></div>';
    }
    const tbl = $("#schedule-pending-table > tbody");
    const arr = Array.isArray(os && os.pending_sample) ? os.pending_sample : [];
    if (!tbl) return;
    if (arr.length === 0) {
      tbl.innerHTML = '<tr><td colspan="8" class="ops-empty">0 pending / quiet-deferred rows — no follow-ups scheduled right now, or quiet-window scheduler has not run yet.</td></tr>';
      return;
    }
    tbl.innerHTML = arr.map(function (r) {
      const txt = r.plain_text == null ? "—" : (String(r.plain_text).length > 80 ? String(r.plain_text).slice(0,80) + "…" : String(r.plain_text));
      return "<tr>" +
        "<td>" + tabnum(r.id) + "</td>" +
        "<td>" + escapeHtml(r.e164 || "—") + "</td>" +
        "<td><span class='ops-chip'>" + escapeHtml(r.direction || "?") + "</span></td>" +
        "<td>" + fmtIso(r.scheduled_for) + "</td>" +
        "<td><span class='ops-chip'>" + escapeHtml(r.status || "pending") + "</span></td>" +
        "<td>" + escapeHtml(r.template_name || "—") + "</td>" +
        "<td>" + escapeHtml(txt) + "</td>" +
        "<td>" + escapeHtml(r.created_by || "—") + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderTemplates(t) {
    const meta = t && t.meta && typeof t.meta === "object" ? t.meta : {};
    const list = Array.isArray(t && t.list) ? t.list : [];
    const tplMeta = $("#tpl-meta");
    if (tplMeta) {
      const age = meta.cached ? ("age " + fmtAgo(meta.age_seconds) + " · ttl " + (meta.ttl_seconds || 300) + "s") : "cache: empty (cold)";
      tplMeta.textContent = age + " · count " + (list.length);
    }
    const tbl = $("#templates-table > tbody");
    if (!tbl) return;
    if (list.length === 0) {
      tbl.innerHTML = '<tr><td colspan="4" class="ops-empty">Templates cache cold — press EVICT TEMPLATES CACHE → REFRESH to fetch fresh approved list from Meta Graph v18.0.</td></tr>';
      return;
    }
    tbl.innerHTML = list.map(function (r) {
      return "<tr>" +
        "<td>" + escapeHtml(r.name || "—") + "</td>" +
        "<td><span class='ops-chip'>" + escapeHtml(r.category || "—") + "</span></td>" +
        "<td>" + escapeHtml(r.language || "—") + "</td>" +
        "<td>" + tabnum(r.params_n || 0) + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderScheduler(sc) {
    const counts = $("#sched-counts");
    if (counts) counts.textContent = (sc.country_tz_count || 0) + " TZs · " + (sc.country_weekend_count || 0) + " weekend profiles";
    const wk = $("#weekend-state");
    if (wk) {
      const on = !!sc.environment_followups_weekend_sends_ok;
      wk.textContent = on ? "ON · send on weekends, quiet 22-07 only" : "OFF · skip weekends, per-country (default Petrobind B2B)";
      wk.className = "ops-weekend-state " + (on ? "on" : "off");
    }
    const grid = $("#zone-grid");
    const arr = Array.isArray(sc.sample_prefix_local_now) ? sc.sample_prefix_local_now : [];
    if (!grid) return;
    if (arr.length === 0) { grid.innerHTML = '<div class="ops-zone ops-zone--skeleton">no zones</div>'; return; }
    grid.innerHTML = arr.map(function (r) {
      const quiet = !!r.inside_quiet_22_07;
      const wdName = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][r.weekday_idx_mon0 || 0];
      const weekend = r.weekend || "";
      const isWeekend = /Fri-Sat/.test(weekend) ? (r.weekday_idx_mon0 === 4 || r.weekday_idx_mon0 === 5)
                        : (r.weekday_idx_mon0 === 5 || r.weekday_idx_mon0 === 6);
      return '<div class="ops-zone">' +
        '<div class="ops-zone-head"><b>' + escapeHtml(r.prefix) + '</b><span>UTC' + (r.tz_offset_hours >= 0 ? "+" : "") + String(r.tz_offset_hours) + 'h</span></div>' +
        '<div class="ops-zone-time">' + escapeHtml(String(r.local_now_iso).split(" ")[1] || "—") + '</div>' +
        '<div class="ops-zone-meta">' +
          '<span class="ops-pill ' + (quiet ? "ops-pill--quiet" : "ops-pill--awake") + '">' + (quiet ? "QUIET 22–07" : "WORK HOURS") + '</span>' +
          (isWeekend ? '<span class="ops-pill ops-pill--weekend">' + wdName + " weekend</span>" : '<span class="ops-pill ops-pill--awake">' + wdName + " bizday</span>") +
        '</div>' +
        '<div class="ops-zone-meta" style="margin-top:2px;">' + escapeHtml(r.weekend || "") + '</div>' +
      '</div>';
    }).join("");
  }

  // -------- Main refresh driver --------------------------------------------
  let lastData = null;
  async function refreshAll(isManual) {
    const ok = await promptForBearerIfMissing();
    if (!ok) return;
    try {
      const res = await adminFetch(API_BASE + "/dashboard");
      if (res.status === 401) {
        toast("Token rejected", "The stored INBOX_ADMIN_TOKEN didn't match the server. Re-paste it at /inbox/login.", "bad");
        setBearer("");
        setTimeout(function () { location.href = "/inbox/login?next=%2Finbox%2Fadmin"; }, 1000);
        return;
      }
      if (!res.ok) {
        toast("Dashboard fetch failed", "HTTP " + res.status + " — " + JSON.stringify(res.payload || {}).slice(0, 160), "bad");
        return;
      }
      lastData = res.payload;
      const freshAt = $("#p-health-fresh");
      if (freshAt) freshAt.textContent = "refreshed " + window.PBTime.timeOnly(new Date(), { seconds: true }) + " GMT+8";
      renderChecks(lastData.checks);
      renderSessions(lastData.sessions, lastData.process);
      renderClaims(lastData.claims);
      renderSchedules(lastData.outbound_schedules);
      renderTemplates(lastData.templates);
      renderScheduler(lastData.scheduler);
      if (isManual) toast("Dashboard refreshed", "Fetched live backend state at " + window.PBTime.timeOnly(new Date(), { seconds: true }) + " GMT+8", "ok");
    } catch (err) {
      toast("Network error", String(err && err.message || err), "bad");
    }
  }

  // -------- Action buttons (toolbar 4 tiles) -------------------------------
  async function runAction(name) {
    const ok = await promptForBearerIfMissing();
    if (!ok) return;
    let ep, title, toastDone;
    if (name === "force-redis-scan") {
      ep = API_BASE + "/force-redis-scan";
      title = "Forcing Redis keyspace scan (bypass 3h guard)";
      toastDone = "Redis scan complete";
    } else if (name === "evict-templates") {
      ep = API_BASE + "/evict-templates-cache";
      title = "Evicting templates cache";
      toastDone = "Templates cache evicted";
    } else if (name === "run-followups") {
      ep = API_BASE + "/run-followups-scan";
      title = "Running follow-ups cron (30-45s typical)";
      toastDone = "Follow-ups scan finished";
    } else if (name === "flush-scheduled") {
      ep = API_BASE + "/flush-scheduled-sends";
      title = "Flushing pending scheduled outbound sends";
      toastDone = "Scheduled send flush complete";
    } else if (name === "import-history") {
      ep = API_BASE + "/import-history";
      title = "Importing chat history from Redis into Supabase";
      toastDone = "History import finished";
    } else {
      toast("Unknown action", name, "warn");
      return;
    }
    toast(title, "Request sent — waiting for server response …", "warn");
    try {
      const res = await adminFetch(ep, { method: "POST", body: {} });
      if (res.status === 401) {
        toast("Token rejected", "Bearer INBOX_ADMIN_TOKEN didn't match server.", "bad");
        setBearer("");
        setTimeout(function () { location.href = "/inbox/login?next=%2Finbox%2Fadmin"; }, 900);
        return;
      }
      if (!res.ok) {
        toast("Action failed: " + name, "HTTP " + res.status + " — " + JSON.stringify(res.payload || {}).slice(0, 240), "bad");
        return;
      }
      const summary = JSON.stringify(res.payload || {}).replace(/[{}"]/g, " ").replace(/\s+/g, " ").trim().slice(0, 200);
      toast(toastDone, summary || "ok (empty body)", "ok");
    } catch (err) {
      toast("Action network error", String(err && err.message || err), "bad");
    } finally {
      setTimeout(refreshAll, 500);
    }
  }

  // -------- Wire up UI ------------------------------------------------------
  function bindUI() {
    // Action tiles
    $$(".ops-act[data-action]").forEach(function (el) {
      el.addEventListener("click", function () { runAction(el.getAttribute("data-action")); });
    });
    // Topbar
    const rbtn = $("#ops-refresh");
    if (rbtn) rbtn.addEventListener("click", function () { refreshAll(true); });
    const tog = $("#ops-toggle-auto");
    const statusBox = $("#ops-auto");
    let paused = false;
    let timerId = null;
    function scheduleNext() {
      clearTimeout(timerId);
      if (paused) return;
      timerId = setTimeout(function () { refreshAll(false); scheduleNext(); }, REFRESH_MS);
    }
    if (tog) tog.addEventListener("click", function () {
      paused = !paused;
      tog.textContent = paused ? "RESUME" : "PAUSE";
      if (statusBox) {
        statusBox.classList.toggle("paused", paused);
        statusBox.querySelector(".ops-auto-text").textContent = paused ? "AUTO REFRESH · PAUSED" : "AUTO REFRESH 10s · ON";
      }
      if (!paused) scheduleNext();
    });
    // Logout
    const lo = $("#ops-logout");
    if (lo) lo.addEventListener("click", function () {
      // Delete cookie server-side: GET /inbox/logout → redirect login
      // Also clear sessionStorage bearer.
      sessionStorage.removeItem("_inbox_bearer");
      // Best-effort server-side:
      try { fetch("/inbox/logout", { method: "GET", credentials: "same-origin" }).finally(function () { location.href = "/inbox/login"; }); }
      catch { location.href = "/inbox/login"; }
    });
    // Clock in GMT+8 (the team's time), whatever timezone this computer is set to
    const utcBox = $("#server-utc");
    if (utcBox) {
      setInterval(function () { utcBox.textContent = window.PBTime.format(new Date(), { seconds: true }) + " GMT+8"; }, 1000);
    }
    scheduleNext();
  }

  // -------- Boot ------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    bindUI();
    refreshAll(true);
  });
})();
