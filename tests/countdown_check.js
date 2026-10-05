// Pulls tickHeaderCountdown out of app.js and runs it against a stand-in page.
const fs = require("fs"), vm = require("vm"), path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "petrobind_frontend_app", "static", "app.js"), "utf8");
const grab = (name) => { const a = src.indexOf("function " + name + "("); let d = 0, i = src.indexOf("{", a); for (let j = i; j < src.length; j++) { if (src[j] === "{") d++; if (src[j] === "}" && --d === 0) return src.slice(a, j + 1); } };
const els = { "hdr-countdown": { textContent: "", className: "" }, "window-expires-text": { textContent: "" } };
const calls = [];
const sandbox = { document: { getElementById: (id) => els[id] || null }, Date, Math, calls,
  renderWindowBanner: (inside, closes) => calls.push(["render", inside]), clockSkew: 0, lastWindowState: {} };
vm.createContext(sandbox);
vm.runInContext("const nowSec = () => Date.now() / 1000 + clockSkew;\n" + grab("formatDuration") + "\n" + grab("tickHeaderCountdown"), sandbox);
const run = (code) => vm.runInContext(code, sandbox);
const out = {};
const now = Date.now() / 1000;

run(`lastWindowState = { inside_24h_window: true, window_closes_at_unix_ts: ${now + 3725} }; tickHeaderCountdown();`);
out.open = [els["hdr-countdown"].textContent, els["window-expires-text"].textContent, calls.length];

run(`lastWindowState = { inside_24h_window: true, window_closes_at_unix_ts: ${now - 5} }; tickHeaderCountdown(); tickHeaderCountdown();`);
out.expired = [els["hdr-countdown"].textContent, JSON.stringify(calls)];          // composer locked exactly once

calls.length = 0;
run(`lastWindowState = { inside_24h_window: null, window_closes_at_unix_ts: null }; tickHeaderCountdown();`);
out.none = [els["hdr-countdown"].textContent, calls.length];

console.log(JSON.stringify(out));
