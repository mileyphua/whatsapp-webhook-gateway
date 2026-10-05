// Runs the browser time helper (static/time.js) and prints results as JSON.
const fs = require("fs"), vm = require("vm"), path = require("path");
const code = fs.readFileSync(path.join(__dirname, "..", "petrobind_frontend_app", "static", "time.js"), "utf8");
const sandbox = { window: {}, Date, Math, Number, String, isNaN };
vm.createContext(sandbox); vm.runInContext(code, sandbox);
const T = sandbox.window.PBTime;
const out = {
  evening_utc_to_next_morning: T.format("2026-10-05T19:03:54Z"),
  with_seconds: T.format("2026-10-05T19:03:54Z", { seconds: true }),
  naive_server_string_is_utc: T.format("2026-10-05T19:03:54"),
  offset_string: T.format("2026-10-05T19:03:54+00:00"),
  date_object: T.format(new Date(Date.UTC(2026, 9, 6, 4, 5, 0))),
  unix_seconds: T.format(1791227425),
  midnight: T.format("2026-10-05T16:07:00Z"),
  noon: T.format("2026-10-06T04:07:00Z"),
  time_only: T.timeOnly("2026-10-05T19:03:54Z"),
  time_only_seconds: T.timeOnly("2026-10-05T19:03:54Z", { seconds: true }),
  space_separated: T.format("2026-10-05 19:03:54"),
  bad: [T.format(""), T.format(null), T.format("nonsense")],
  label: T.LABEL,
};
console.log(JSON.stringify(out));
