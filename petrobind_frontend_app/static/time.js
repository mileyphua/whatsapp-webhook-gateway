/* Every time shown in the inbox is GMT+8 in 12-hour form, whatever timezone this computer is set to.
   Server times arrive as UTC (with or without a trailing Z); unix seconds and Date objects work too. */
(function () {
  var OFFSET_MS = 8 * 3600 * 1000;
  function toDate(v) {
    if (v == null || v === "") return null;
    if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
    if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
    var s = String(v).trim();
    if (/^\d{4}-\d{2}-\d{2} \d/.test(s)) s = s.replace(" ", "T");
    if (/^\d{4}-\d{2}-\d{2}T/.test(s) && !/(Z|[+-]\d{2}:?\d{2})$/.test(s)) s += "Z";   // no zone given = UTC
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function clock(d, seconds) {
    var g = new Date(d.getTime() + OFFSET_MS), h = g.getUTCHours();
    return (h % 12 || 12) + ":" + pad(g.getUTCMinutes()) + (seconds ? ":" + pad(g.getUTCSeconds()) : "") + (h < 12 ? " AM" : " PM");
  }
  window.PBTime = {
    LABEL: "GMT+8",
    format: function (v, o) {
      var d = toDate(v);
      if (!d) return v == null ? "" : (typeof v === "string" ? v : "");
      var g = new Date(d.getTime() + OFFSET_MS);
      return g.getUTCFullYear() + "-" + pad(g.getUTCMonth() + 1) + "-" + pad(g.getUTCDate()) + " " + clock(d, o && o.seconds);
    },
    timeOnly: function (v, o) { var d = toDate(v); return d ? clock(d, o && o.seconds) : ""; },
    now: function () { return new Date(); }
  };
})();
