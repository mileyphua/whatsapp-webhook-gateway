// Runs the inbox script's start-up code against a stand-in page. Any error thrown while the thread page initialises
// (e.g. reading a variable before it is declared) fails here, instead of silently killing every button in the browser.
const fs = require("fs"), vm = require("vm"), path = require("path");
const code = fs.readFileSync(path.join(__dirname, "..", "petrobind_frontend_app", "static", "app.js"), "utf8");

function el() {
  const t = function () {};
  return new Proxy(t, {
    get(o, p) {
      if (p === "then") return undefined;
      if (p === Symbol.iterator) return function* () {};
      if (p === "length") return 0;
      if (p === "classList") return { toggle() {}, add() {}, remove() {}, contains() { return false; } };
      if (p === "style" || p === "dataset") return {};
      if (p === "files") return [];
      if (p === "hidden" || p === "disabled") return false;
      if (p === "value" || p === "textContent" || p === "innerHTML" || p === "className" || p === "src" || p === "href") return "";
      if (p in o) return o[p];
      return (...a) => el();
    },
    set() { return true; },
    apply() { return el(); },
  });
}
const doc = {
  getElementById: () => el(), querySelector: () => el(), querySelectorAll: () => [], createElement: () => el(), addEventListener() {},
  cookie: "", body: el(), head: el(), documentElement: el(), hidden: false,
};
const never = new Promise(() => {});
const sandbox = {
  document: doc, console, navigator: { sendBeacon() {}, userAgent: "smoke" },
  location: { pathname: "/inbox/chat/60123456789", hash: "", search: "", href: "http://x/inbox/chat/60123456789", reload() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} }, localStorage: { getItem: () => null, setItem() {} },
  fetch: () => never, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: () => 0, performance: { now: () => 0 }, history: { replaceState() {} },
  Headers: class { constructor() {} set() {} get() { return null; } }, FormData: class { append() {} },
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} }, Intl, Date, JSON, Math, Set, Map, Promise, Array, Object, String, Number, Error, Uint32Array,
  crypto: { getRandomValues: (a) => a }, addEventListener() {}, supabase: undefined,
};
sandbox.window = sandbox;
sandbox.__INBOX_CTX__ = { e164: "60123456789", session_id: "s", admin_name: "Admin", is_admin: true, last_buyer_wamid: "", supa_url: "", supa_anon_key: "" };
try {
  vm.runInNewContext(code, sandbox, { filename: "app.js" });
  console.log("INIT_OK");
} catch (e) {
  console.log("INIT_FAILED: " + (e && e.stack ? e.stack.split("\n").slice(0, 3).join(" | ") : e));
  process.exit(1);
}
