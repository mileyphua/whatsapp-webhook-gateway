// Opens the real inbox pages in jsdom (a browser stand-in), clicks the real buttons, and reports PASS/FAIL for each.
// Usage: JSDOM_PATH=/path/to/node_modules/jsdom node tests/ui_audit/audit.js http://127.0.0.1:8003
const { JSDOM, ResourceLoader, CookieJar } = require(process.env.JSDOM_PATH);
const BASE = process.argv[2];
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok: !!ok, detail: ok ? "" : String(detail || "") }); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const A = "60120000001", B = "60120000002";

class LocalOnly extends ResourceLoader {            // load our own scripts, never the CDNs (no network needed)
  fetch(url, o) { return url.startsWith(BASE) ? super.fetch(url, o) : Promise.resolve(Buffer.from("")); }
}

async function login(username, password) {
  const jar = new CookieJar();
  const r = await fetch(BASE + "/inbox/login", { method: "POST", redirect: "manual", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username, password }) });
  for (const c of r.headers.getSetCookie()) jar.setCookieSync(c, BASE);
  return jar;
}

async function openPage(path, jar) {
  const errors = [], net = [];
  const dom = await JSDOM.fromURL(BASE + path, {
    runScripts: "dangerously", resources: new LocalOnly(), cookieJar: jar, pretendToBeVisual: true,
    beforeParse(w) {
      const cookie = () => jar.getCookieStringSync(BASE);
      w.fetch = async (u, o = {}) => {
        const url = new URL(u, w.location.href).href;
        const headers = new Headers(o.headers && typeof o.headers.forEach === "function" ? Object.fromEntries([...(o.headers.entries ? o.headers.entries() : [])]) : o.headers || {});
        headers.set("cookie", cookie());
        let body = o.body;
        const res = await fetch(url, { method: o.method || "GET", headers, body, redirect: "manual" });
        net.push({ url: url.replace(BASE, ""), method: o.method || "GET", status: res.status });
        return res;
      };
      w.tailwind = { config: {} };                       // the CDN script is not loaded here
      w.FormData = FormData; w.Headers = Headers; w.File = File; w.Blob = Blob;
      w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
      w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
      w.HTMLElement.prototype.scrollIntoView = function () {}; w.Element.prototype.scrollTo = function () {};
      w.alert = (m) => errors.push("alert(): " + m); w.confirm = () => true;
      w.URL.createObjectURL = () => "blob:x"; w.URL.revokeObjectURL = () => {};
      w.matchMedia = w.matchMedia || (() => ({ matches: false, addEventListener() {}, addListener() {} }));
      w.addEventListener("error", (e) => errors.push("error: " + e.message));
      w.addEventListener("unhandledrejection", (e) => errors.push("rejection: " + (e.reason && e.reason.message || e.reason)));
    },
    virtualConsole: new (require(process.env.JSDOM_PATH).VirtualConsole)().on("jsdomError", (e) => {
      if (!/Not implemented|navigation|tailwind is not defined/i.test(e.message)) errors.push("jsdom: " + e.message.split("\n")[0]);
    }),
  });
  await sleep(700);
  return { dom, w: dom.window, d: dom.window.document, errors, net };
}
const $ = (p, s) => p.d.querySelector(s);
const click = (p, s) => { const el = typeof s === "string" ? $(p, s) : s; if (!el) throw new Error("no element " + s); el.dispatchEvent(new p.w.MouseEvent("click", { bubbles: true, cancelable: true })); };
const setVal = (p, s, v) => { const el = $(p, s); el.value = v; el.dispatchEvent(new p.w.Event("input", { bubbles: true })); el.dispatchEvent(new p.w.Event("change", { bubbles: true })); };
const calls = (p, re) => p.net.filter((n) => re.test(n.method + " " + n.url));
const sent = async () => (await (await fetch(BASE + "/_sent")).json());

async function pagesLoad(jar, who) {
  for (const [path, label] of [["/inbox/chats", "Inbox"], ["/inbox/learning", "Learning"], ["/inbox/stock", "Stock"], ["/inbox/logs", "Logs"], ["/inbox/team", "Team"], ["/inbox/architecture", "Architecture"], ["/inbox/admin", "Ops"], ["/inbox/guide", "Guide"]]) {
    const p = await openPage(path, jar);
    const bad = p.net.filter((n) => n.status >= 500 || (n.status >= 400 && n.status !== 401 && n.status !== 403 && n.status !== 404));
    check(`${who}: ${label} page opens with no script errors`, p.errors.length === 0 && bad.length === 0, p.errors.concat(bad.map((b) => b.method + b.url + " " + b.status)).join(" | "));
    p.w.close();
  }
}

async function crawl(path, label, jar) {
  const p = await openPage(path, jar);
  const btns = [...p.d.querySelectorAll("button:not([disabled]), [role=button]")].filter((b) => !/log ?out|sign out/i.test(b.textContent || ""));
  let clicked = 0;
  for (const b of btns) {
    if (!b.isConnected) continue;
    try { b.dispatchEvent(new p.w.MouseEvent("click", { bubbles: true, cancelable: true })); clicked++; } catch (e) { p.errors.push("click threw: " + e.message); }
    await sleep(120);
  }
  // 503 = a feature that needs a service this throwaway server does not have (e.g. import-history needs Supabase): a clear message, not a bug
  const bad = p.net.filter((n) => n.status >= 500 && n.status !== 503);
  check(`${label}: ${clicked} buttons clicked, no script errors and no server errors`, p.errors.length === 0 && bad.length === 0,
    p.errors.concat(bad.map((x) => x.method + " " + x.url + " " + x.status)).join(" | "));
  p.w.close();
}

(async () => {
  const admin = await login("", "audit-admin-pass-123456");
  await pagesLoad(admin, "admin");

  // ---------------- chat list ----------------
  let L = await openPage("/inbox/chats", admin);
  const rows = [...L.d.querySelectorAll("#chat-scroll li")];
  check("list: both chats are listed", rows.length === 2, rows.length);
  const timeText = ($(L, `li[data-e164="${A}"] time`) || {}).textContent || "";
  check("list: time is GMT+8 12-hour (no 24h clock)", /\d{4}-\d\d-\d\d \d{1,2}:\d\d (AM|PM)$/.test(timeText.trim()), timeText);
  const search = L.d.querySelector('input[type="search"], #chat-search, input[placeholder*="Search"]');
  if (search) { setVal(L, search.id ? "#" + search.id : 'input[placeholder*="Search"]', "0002"); await sleep(200);
    const vis = [...L.d.querySelectorAll("#chat-scroll li")].filter((li) => !li.hidden && li.style.display !== "none");
    check("list: search filters to the matching number", vis.length === 1 && vis[0].dataset.e164 === B, vis.map((v) => v.dataset.e164)); setVal(L, search.id ? "#" + search.id : 'input[placeholder*="Search"]', ""); }
  else check("list: search box exists", false, "not found");

  // open a chat
  click(L, `li[data-e164="${A}"] a.chat-item`); await sleep(400);
  const pane = $(L, "#thread-pane");
  check("list: clicking a chat opens it in the right pane", pane && /\/inbox\/chat\/60120000001/.test(pane.getAttribute("src") || ""), pane && pane.getAttribute("src"));

  // ⋮ menu: rename
  click(L, `li[data-e164="${A}"] .chat-menu-btn`); await sleep(100);
  check("menu: ⋮ opens the options", !$(L, "#chat-actions").hidden, "still hidden");
  click(L, "#act-rename"); await sleep(100);
  const nameInput = $(L, "#chat-menu-input, #chat-menu input[type=text]");
  check("menu: Set name shows a name box that STAYS open (what a person sees)", !!nameInput && !$(L, "#chat-menu").hidden, "popover hidden=" + $(L, "#chat-menu").hidden);
  if (nameInput) { nameInput.value = "Audit Buyer"; nameInput.dispatchEvent(new L.w.Event("input", { bubbles: true })); click(L, "#chat-menu-save"); await sleep(600);
    check("menu: Save stores the name", calls(L, /PUT \/api\/inbox\/chats\/60120000001\/name/).some((c) => c.status === 200), JSON.stringify(calls(L, /name/)));
    check("menu: the saved name shows in the chat list and the box closes", /Audit Buyer/.test(($(L, `li[data-e164="${A}"]`) || { textContent: "" }).textContent) && $(L, "#chat-menu").hidden, ($(L, `li[data-e164="${A}"]`) || { textContent: "" }).textContent.slice(0, 80) + " hidden=" + $(L, "#chat-menu").hidden); }
  // the list refreshes itself every few seconds: opening ⋮ then waiting must not break "Set name"
  click(L, `li[data-e164="${A}"] .chat-menu-btn`); await sleep(4600);
  click(L, "#act-rename"); await sleep(150);
  check("menu: Set name still works after the list refreshed in the background", !$(L, "#chat-menu").hidden && $(L, "#chat-menu-input").value === "Audit Buyer", "hidden=" + $(L, "#chat-menu").hidden + " value=" + $(L, "#chat-menu-input").value);
  click(L, "#chat-menu-cancel"); check("menu: Cancel closes the name box", $(L, "#chat-menu").hidden, "still open");
  // forget memory
  click(L, `li[data-e164="${A}"] .chat-menu-btn`); click(L, "#act-forget"); await sleep(100);
  check("menu: Forget opens its confirmation", $(L, "#forget-dialog").hasAttribute("open"), "dialog closed");
  click(L, "#forget-cancel"); check("menu: Forget → Cancel closes it", !$(L, "#forget-dialog").hasAttribute("open"), "still open");
  click(L, `li[data-e164="${A}"] .chat-menu-btn`); click(L, "#act-forget"); click(L, "#forget-confirm"); await sleep(700);
  check("menu: Forget → confirm succeeds", calls(L, /POST \/api\/inbox\/chats\/60120000001\/forget-memory/).some((c) => c.status === 200), JSON.stringify(calls(L, /forget/)));
  // delete (chat B is the throwaway one)
  click(L, `li[data-e164="${B}"] .chat-menu-btn`); click(L, "#act-delete"); await sleep(100);
  check("menu: Delete opens its confirmation", $(L, "#new-conversation-modal") && $(L, "dialog[open]") != null, "no dialog");
  click(L, "#del-cancel"); check("menu: Delete → Cancel keeps the chat", !!$(L, `li[data-e164="${B}"]`), "row gone");

  // new conversation
  click(L, "#btn-new-conversation"); await sleep(900);
  const modal = $(L, "#new-conversation-modal");
  check("new conversation: modal opens with no Freeform tab", modal.hasAttribute("open") && !$(L, "#tab-freeform"), "open=" + modal.hasAttribute("open"));
  const opts = [...L.d.querySelectorAll("#template-select option")];
  check("new conversation: approved templates are loaded", opts.length >= 4, opts.map((o) => o.textContent));
  check("new conversation: unsupported template is greyed out", opts.some((o) => /has_video/.test(o.textContent) && o.disabled), opts.map((o) => o.textContent + o.disabled));
  click(L, "#btn-template-refresh"); await sleep(600);
  check("new conversation: Sync from Meta works", calls(L, /GET \/api\/inbox\/templates\?refresh=1/).some((c) => c.status === 200), JSON.stringify(calls(L, /templates/)));
  const sel = $(L, "#template-select"); sel.value = String([...sel.options].findIndex((o) => /enquiry_followup/.test(o.textContent)) - 1); sel.dispatchEvent(new L.w.Event("change"));
  check("new conversation: picking a template shows variable boxes and a preview", L.d.querySelectorAll("#template-params .tpl-param").length === 2 && !$(L, "#template-info").classList.contains("hidden"), "params=" + L.d.querySelectorAll("#template-params .tpl-param").length);
  click(L, "#btn-template-send"); await sleep(200);
  check("new conversation: sending without a number is refused politely", !$(L, "#new-conv-info").classList.contains("hidden") && (await sent()).length === 0, $(L, "#new-conv-info").textContent);
  setVal(L, "#template-e164", "+60 12-999 8888");
  const ps = [...L.d.querySelectorAll("#template-params .tpl-param")]; ps[0].value = "Ahmed"; ps[0].dispatchEvent(new L.w.Event("input")); ps[1].value = "bitumen"; ps[1].dispatchEvent(new L.w.Event("input"));
  check("new conversation: live preview fills in the variables", /Hello Ahmed, following up on bitumen/.test($(L, "#tpl-summary").textContent), $(L, "#tpl-summary").textContent);
  click(L, "#btn-template-send"); await sleep(900);
  const s1 = await sent(); const tmsg = s1.filter((x) => x.kind === "message").pop();
  check("new conversation: Send template & open sends to the cleaned number with the variables", tmsg && tmsg.body.to === "60129998888" && tmsg.body.template.components[0].parameters.map((p) => p.text).join() === "Ahmed,bitumen", JSON.stringify(tmsg));
  click(L, "#btn-new-conversation"); await sleep(600);
  const sel2 = $(L, "#template-select"); sel2.value = String([...sel2.options].findIndex((o) => /introductory_follow_up/.test(o.textContent)) - 1); sel2.dispatchEvent(new L.w.Event("change"));
  check("new conversation: PDF-header template shows a file picker", !$(L, "#template-header").classList.contains("hidden"), "hidden");
  click(L, "#modal-close-btn"); check("new conversation: × closes the modal", !$(L, "#new-conversation-modal").hasAttribute("open"), "still open");
  check("list: no script errors during all of the above", L.errors.length === 0, L.errors.join(" | "));
  L.w.close();

  // ---------------- needs-human bar ----------------
  const H = await openPage("/inbox/chats", admin);
  click(H, "#ho-toggle"); await sleep(500);
  check("needs human: the pill opens the waiting list", !$(H, "#ho-panel").hidden && H.d.querySelectorAll("#ho-list li").length >= 1, "panel hidden=" + $(H, "#ho-panel").hidden);
  H.w.close();

  // ---------------- thread: open window chat A ----------------
  const T = await openPage(`/inbox/chat/${A}?embed=1`, admin);
  check("thread: message times are GMT+8 12-hour", /\d{1,2}:\d\d (AM|PM)/.test($(T, "#message-stack time").textContent) && !/\b1[3-9]:\d\d|2[0-3]:\d\d/.test($(T, "#message-stack time").textContent), $(T, "#message-stack time").textContent);
  check("thread: the buyer's photo is shown (not a placeholder)", !!$(T, `#message-stack img[src*="/api/inbox/media/IMGAUDIT123"]`), "no <img>");
  check("thread: countdown runs from the buyer's last message", /left to reply/.test($(T, "#hdr-countdown").textContent), $(T, "#hdr-countdown").textContent);
  check("thread: window banner says OPEN", /OPEN/.test($(T, "#window-status-banner").textContent), $(T, "#window-status-banner").textContent);
  check("thread: reply box and Attach are enabled while the window is open", !$(T, "#human-text-input").disabled && !$(T, "#attach-btn").disabled, "textarea.disabled=" + $(T, "#human-text-input").disabled);
  check("thread: 👍/👎 exist under the AI message", !!$(T, ".fb-up") && !!$(T, ".fb-down"), "missing");
  click(T, ".fb-up"); await sleep(600);
  check("thread: 👍 is recorded", calls(T, /POST \/api\/inbox\/feedback/).some((c) => c.status === 200), JSON.stringify(calls(T, /feedback/)));
  const rb = T.d.querySelector(".reply-btn"); check("thread: ↩ Reply buttons exist", !!rb, "none");
  if (rb) { click(T, rb); await sleep(100); check("thread: ↩ Reply shows the quote bar", !$(T, "#quote-preview").hidden, "hidden"); click(T, "#quote-clear"); check("thread: quote ✕ clears it", $(T, "#quote-preview").hidden, "still shown"); }
  check("thread: pressing ↩ Reply takes the chat for you (AI pauses)", /Human/i.test($(T, "#mode-label").textContent), $(T, "#mode-label").textContent);
  click(T, "#mode-switch"); await sleep(700);
  check("thread: the switch turns Human replying off again", /AI/i.test($(T, "#mode-label").textContent) && calls(T, /DELETE \/api\/inbox\/chats\/60120000001\/claim/).some((c) => c.status === 200), $(T, "#mode-label").textContent);
  click(T, "#mode-switch"); await sleep(700);
  check("thread: switch to Human replying takes the chat", /Human/i.test($(T, "#mode-label").textContent) && calls(T, /POST \/api\/inbox\/chats\/60120000001\/claim/).some((c) => c.status === 200), $(T, "#mode-label").textContent);
  setVal(T, "#human-text-input", "Hello from the team"); click(T, "#btn-send-submit"); await sleep(900);
  const s2 = await sent();
  check("thread: Send delivers the typed text", s2.some((x) => x.kind === "message" && x.body.text && x.body.text.body === "Hello from the team"), JSON.stringify(s2.slice(-2)));
  check("thread: the reply box is emptied after sending", $(T, "#human-text-input").value === "", $(T, "#human-text-input").value);
  // attachment
  const fileInput = $(T, "#attach-input"); const f = new File([Buffer.from("%PDF-1.7\n" + "x".repeat(200))], "specs.pdf", { type: "application/pdf" });
  Object.defineProperty(fileInput, "files", { value: [f], configurable: true }); fileInput.dispatchEvent(new T.w.Event("change", { bubbles: true })); await sleep(300);
  check("thread: choosing a file shows the preview chip", !$(T, "#attach-preview").hidden && /specs\.pdf/.test($(T, "#attach-name").textContent), $(T, "#attach-name").textContent);
  click(T, "#btn-send-submit"); await sleep(1200);
  check("thread: Send uploads the attachment", calls(T, /POST \/api\/inbox\/chats\/60120000001\/attachments/).some((c) => c.status === 200), JSON.stringify(calls(T, /attachments/)));
  check("thread: the attachment preview clears after sending", $(T, "#attach-preview").hidden, "still shown");
  click(T, "#mode-switch"); await sleep(600);
  check("thread: switching back to AI replying works", /AI/i.test($(T, "#mode-label").textContent), $(T, "#mode-label").textContent);
  const sg = await openPage(`/inbox/chat/${A}?embed=1`, admin); // AI suggestion pill, in a fresh page
  const sgBtn = sg.d.querySelector("#btn-suggestion-reload-inline"); if (sgBtn) { click(sg, sgBtn); await sleep(800); }
  check("thread: AI suggest loads a draft", /Audit suggested reply/.test(sg.d.body.textContent) || calls(sg, /suggestion/).some((c) => c.status === 200), JSON.stringify(calls(sg, /suggestion/)));
  sg.w.close();
  check("thread (open window): no script errors", T.errors.length === 0, T.errors.join(" | "));
  T.w.close();

  // ---------------- thread: closed window chat B ----------------
  const C = await openPage(`/inbox/chat/${B}?embed=1`, admin);
  check("closed chat: banner says OUTSIDE the window", /OUTSIDE/i.test($(C, "#window-status-banner").textContent), $(C, "#window-status-banner").textContent);
  check("closed chat: countdown says closed", /closed/.test($(C, "#hdr-countdown").textContent), $(C, "#hdr-countdown").textContent);
  check("closed chat: reply box and Attach are locked", $(C, "#human-text-input").disabled && $(C, "#attach-btn").disabled, "disabled=" + $(C, "#human-text-input").disabled);
  check("closed chat: Pick Template button exists", !!$(C, "#banner-pick-template, #btn-pick-template-inline"), "missing");
  const direct = await (await fetch(BASE + `/api/inbox/chats/${B}/messages`, { method: "POST", headers: { "content-type": "application/json", cookie: admin.getCookieStringSync(BASE) }, body: JSON.stringify({ text: "sneaky" }) })).json();
  check("closed chat: the server also refuses free text", direct.code === "window_closed", JSON.stringify(direct));
  check("closed chat: no script errors", C.errors.length === 0, C.errors.join(" | "));
  C.w.close();

  // ---------------- team member view ----------------
  const mei = await login("mei", "mei-password-1");
  const M = await openPage("/inbox/chats", mei);
  check("member: sees Delete chat but not Forget AI memory", !$(M, "#act-delete").hidden && $(M, "#act-forget").hidden, "delete.hidden=" + $(M, "#act-delete").hidden + " forget.hidden=" + $(M, "#act-forget").hidden);
  check("member: admin pages are not offered in the menu", ![...M.d.querySelectorAll("nav a")].some((a) => /team|admin|logs|learning/.test(a.getAttribute("href") || "")), [...M.d.querySelectorAll("nav a")].map((a) => a.getAttribute("href")));
  check("member: inbox opens with no script errors", M.errors.length === 0, M.errors.join(" | "));
  M.w.close();
  const lo = await fetch(BASE + "/inbox/logout", { redirect: "manual", headers: { cookie: mei.getCookieStringSync(BASE) } });
  check("logout: signs out and returns to the login page", lo.status === 302 && /login/.test(lo.headers.get("location") || ""), lo.status + " " + lo.headers.get("location"));

  // ---------------- Stock page ----------------
  const K = await openPage("/inbox/stock", admin); await sleep(800);
  const rowsK = [...K.d.querySelectorAll("#stock-families li")];
  check("stock: lists all 50 items in 5 families", rowsK.length === 50 && K.d.querySelectorAll("#stock-families section").length === 5, rowsK.length);
  check("stock: summary shows everything in stock", /50 of 50 in stock · 0 off/.test($(K, "#stock-summary").textContent), $(K, "#stock-summary").textContent);
  const sw = $(K, 'li[data-slug="products-bitumen-60-70"] button[role=switch]');
  check("stock: each item has an accessible switch that starts On", sw && sw.getAttribute("aria-checked") === "true", sw && sw.outerHTML.slice(0, 120));
  click(K, sw); await sleep(700);
  check("stock: clicking a switch saves it (PUT) and shows it Off", calls(K, /PUT \/api\/inbox\/catalog\/item\/products-bitumen-60-70/).some((c) => c.status === 200) && $(K, 'li[data-slug="products-bitumen-60-70"] button[role=switch]').getAttribute("aria-checked") === "false" && /49 of 50 in stock · 1 off/.test($(K, "#stock-summary").textContent), JSON.stringify(calls(K, /catalog/)));
  check("stock: an Off item says since when and by whom (GMT+8)", /Off since .* GMT\+8 by Petrobind Admin/.test($(K, 'li[data-slug="products-bitumen-60-70"]').textContent), $(K, 'li[data-slug="products-bitumen-60-70"]').textContent.slice(0, 160));
  setVal(K, "#stock-search", "css-1h"); await sleep(150);
  check("stock: search narrows the list", K.d.querySelectorAll("#stock-families li").length === 1 && /CSS-1h/.test($(K, "#stock-families").textContent), K.d.querySelectorAll("#stock-families li").length);
  setVal(K, "#stock-search", "");
  $(K, "#stock-filter-off").checked = true; $(K, "#stock-filter-off").dispatchEvent(new K.w.Event("change")); await sleep(150);
  check("stock: 'only Off' shows just the switched-off item", K.d.querySelectorAll("#stock-families li").length === 1 && /60\/70/.test($(K, "#stock-families").textContent), K.d.querySelectorAll("#stock-families li").length);
  K.w.close();
  const K2 = await openPage("/inbox/stock", admin); await sleep(700);
  check("stock: the switch is still Off after reloading the page", $(K2, 'li[data-slug="products-bitumen-60-70"] button[role=switch]').getAttribute("aria-checked") === "false", "reverted");
  const famBtns = [...K2.d.querySelectorAll("#stock-families section")][2].querySelectorAll("button:not([role=switch])");
  click(K2, famBtns[1]); await sleep(150);
  check("stock: 'All off' asks to confirm first and changes nothing yet", /Confirm/.test([...K2.d.querySelectorAll("#stock-families section")][2].textContent) && !calls(K2, /PUT \/api\/inbox\/catalog\/family/).length, "no confirm step");
  click(K2, [...K2.d.querySelectorAll("#stock-families section")][2].querySelectorAll("button:not([role=switch])")[1]); await sleep(900);
  check("stock: confirming switches the whole family off", calls(K2, /PUT \/api\/inbox\/catalog\/family\/oxidized/).some((c) => c.status === 200) && /0 of 19 in stock/.test([...K2.d.querySelectorAll("#stock-families section")][2].textContent), JSON.stringify(calls(K2, /family/)));
  click(K2, [...K2.d.querySelectorAll("#stock-families section")][2].querySelectorAll("button:not([role=switch])")[0]); await sleep(900);
  check("stock: 'All on' brings the family back", /19 of 19 in stock/.test([...K2.d.querySelectorAll("#stock-families section")][2].textContent), "not restored");
  const bk = await fetch(BASE + "/api/inbox/catalog/item/products-bitumen-60-70", { method: "PUT", headers: { "content-type": "application/json", cookie: admin.getCookieStringSync(BASE) }, body: JSON.stringify({ on: true }) });
  check("stock: (cleanup) item switched back on", bk.status === 200, bk.status);
  check("stock page: no script errors", K2.errors.length === 0, K2.errors.join(" | "));
  K2.w.close();
  const memS = await login("mei", "mei-password-1");
  const MS = await openPage("/inbox/stock", memS); await sleep(1200);
  check("stock (member): sees the availability list, read-only", MS.d.querySelectorAll("#stock-families li").length === 50 && !MS.d.querySelector("button[role=switch]") && /In stock|Out of stock/.test(MS.d.body.textContent), MS.d.querySelectorAll("#stock-families li").length);
  const mt = MS.d.querySelector("main").textContent;
  check("stock (member): no description, no on/off buttons, no admin details", !/Switch an item/.test(mt) && !/All on|All off/.test(mt) && !/Off since/.test(mt) && !MS.d.querySelector("#stock-links-warning"), ["Switch an item", "All on", "All off", "Off since"].filter((x) => mt.includes(x)).join(",") + (MS.d.querySelector("#stock-links-warning") ? " warning-box" : ""));
  check("stock (member): a broken website link is simply not offered", !MS.d.querySelector('li[data-slug="products-bitumen-80-100"] a') && !!MS.d.querySelector('li[data-slug="products-bitumen-60-70"] a[href="https://www.petrobindglobal.com/products/bitumen-60-70"]'), "link shown for a dead page / missing for a live one");
  check("stock (member): menu has the Stock link", !!MS.d.querySelector('nav a[href="/inbox/stock"]'), "missing");
  check("stock (member): cannot change anything", (await fetch(BASE + "/api/inbox/catalog/item/products-bitumen-60-70", { method: "PUT", headers: { "content-type": "application/json", cookie: memS.getCookieStringSync(BASE) }, body: JSON.stringify({ on: false }) })).status === 403, "allowed");
  MS.w.close();
  const KA = await openPage("/inbox/stock", admin); await sleep(1500);
  const warn = $(KA, "#stock-links-warning");
  check("stock (admin): a warning explains that product pages are not opening on the website", !warn.hidden && /49 product pages|of 49/.test(warn.textContent) && /re-deployed/.test(warn.textContent), warn.hidden + " " + warn.textContent.slice(0, 120));
  check("stock (admin): each dead page is marked, the live page keeps its link", /Page not opening on the website \(404\)/.test($(KA, 'li[data-slug="products-bitumen-80-100"]').textContent) && !!KA.d.querySelector('li[data-slug="products-bitumen-60-70"] a'), $(KA, 'li[data-slug="products-bitumen-80-100"]').textContent.slice(0, 120));
  KA.w.close();

  // ---------------- AI offline banner ----------------
  const N0 = await openPage("/inbox/chats", admin); await sleep(500);
  check("AI banner: hidden while the AI works", $(N0, "#ai-status-bar").hidden, $(N0, "#ai-status-bar").textContent);
  N0.w.close();
  await fetch(BASE + "/_ai_fail");
  const N1 = await openPage("/inbox/chats", admin); await sleep(700);
  const bar = $(N1, "#ai-status-bar");
  check("AI banner: shown to staff when OpenRouter credits are used up, with what to do", !bar.hidden && /credits are used up/.test(bar.textContent) && /openrouter\.ai\/settings\/credits/.test(bar.textContent) && /GMT\+8/.test(bar.textContent), bar.hidden + " " + bar.textContent);
  const ops = await openPage("/inbox/admin", admin); await sleep(1500);
  check("AI banner: also shown on the Ops page, and its check turns red", !$(ops, "#ai-status-bar").hidden && /LLM answering/.test(ops.d.body.textContent), "bar hidden=" + $(ops, "#ai-status-bar").hidden);
  ops.w.close();
  const memb = await openPage("/inbox/chats", await login("mei", "mei-password-1")); await sleep(600);
  check("AI banner: team members see it too", !$(memb, "#ai-status-bar").hidden, "hidden");
  memb.w.close();
  await fetch(BASE + "/_ai_ok");
  N1.w.close();
  const N2 = await openPage("/inbox/chats", admin); await sleep(500);
  check("AI banner: disappears once the AI answers again", $(N2, "#ai-status-bar").hidden, $(N2, "#ai-status-bar").textContent);
  N2.w.close();

  // ---------------- learning: which feedback is it learning from? ----------------
  const G = await openPage("/inbox/learning", admin); await sleep(600);
  const waitCards = G.d.querySelectorAll("#waiting > div");
  check("learning: the waiting list shows the ratings that have not been learned from yet", waitCards.length === 3 && /audit price question/.test(G.d.querySelector("#waiting").textContent) && /Waiting to be learned from\s*\(3\)/.test(G.d.querySelector("#waiting-section").textContent), G.d.querySelector("#waiting-section").textContent.slice(0, 120));
  check("learning: the table marks them as waiting", [...G.d.querySelectorAll("#recent td")].filter((t) => t.textContent === "Waiting").length === 3, "no Waiting cells");
  click(G, "#learn-btn"); await sleep(1200);
  const res = $(G, "#learn-result");
  check("learning: clicking Learn shows a result panel", !res.hidden && /Just learned from 3 ratings/.test(res.textContent), res.textContent.slice(0, 120));
  check("learning: the panel lists the exact feedback it read", /audit price question 1\?/.test(res.textContent) && /audit price question 2\?/.test(res.textContent) && /Do not quote prices/.test(res.textContent), res.textContent.slice(0, 300));
  check("learning: the panel names the skill and the knowledge check that came out", /New skill: Pricing questions/.test(res.textContent) && /Check VG30 lead time/.test(res.textContent), res.textContent);
  check("learning: the waiting list is now empty", G.d.querySelectorAll("#waiting > div").length === 1 && /Nothing waiting/.test(G.d.querySelector("#waiting").textContent), G.d.querySelector("#waiting").textContent);
  check("learning: the table now says Learned", [...G.d.querySelectorAll("#recent td")].filter((t) => /Learned/.test(t.textContent)).length === 3, "no Learned cells");
  const sk = [...G.d.querySelectorAll("#skills details summary")].find((x) => /Learned from 3 ratings/.test(x.textContent));
  check("learning: the new skill shows the ratings it came from", !!sk && /audit price question/.test(sk.parentElement.textContent), "no sources");
  click(G, "#learn-result button"); check("learning: Close hides the panel", $(G, "#learn-result").hidden, "still shown");
  check("learning page: no script errors", G.errors.length === 0, G.errors.join(" | "));
  G.w.close();

  for (const [path, label] of [["/inbox/team", "Team page"], ["/inbox/learning", "Learning page"], ["/inbox/stock", "Stock page"], ["/inbox/logs", "Logs page"], ["/inbox/admin", "Ops page"], ["/inbox/guide", "Guide page"], ["/inbox/architecture", "Architecture page"]]) await crawl(path, label, admin);

  const O = await openPage("/inbox/admin", admin); await sleep(1200);
  const imp = $(O, '.ops-act[data-action="import-history"]');
  check("Ops: Import history is switched off, with the reason, when Supabase is not set up", imp && imp.disabled && /Supabase/.test(imp.title), imp && (imp.disabled + " " + imp.title));
  check("Ops: other actions stay available", !$(O, '.ops-act[data-action="force-redis-scan"]').disabled, "disabled");
  O.w.close();

  const failed = results.filter((r) => !r.ok);
  console.log(JSON.stringify({ total: results.length, failed: failed.length, results }));
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.log(JSON.stringify({ total: results.length, failed: 1, crash: String(e && e.stack || e), results })); process.exit(2); });
