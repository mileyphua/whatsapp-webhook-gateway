"""Regression: the reply form must be handled only by our own JS.

Bug: the form carried hx-post/hx-headers/hx-on attributes. HTMX bound to it, and an `hx-post=""` that app.js tried to
'disable' actually means "POST to the current page". Clicking Send therefore POSTed the chat page to itself (404) and our
fetch() to /api/inbox/chats/{e164}/messages never ran: no human message could ever be sent."""
import os
import re
import unittest

import jinja2

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "petrobind_frontend_app", "templates")


def _render(**kw):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    env.globals["asset_v"] = lambda: "test"
    ctx = dict(e164="60123456789", embed=True, messages=[], admin_name="x", session_id="s", last_buyer_wamid="",
               supa_url="", supa_anon_key="", contact_name="")
    ctx.update(kw)
    return env.get_template("chat_thread.html").render(**ctx)


class ReplyFormIsNotHijackedByHtmx(unittest.TestCase):
    def test_reply_form_has_no_htmx_attributes(self):
        html = _render()
        form = re.search(r'<form[^>]*id="human-send-form"[^>]*>', html, re.S)
        self.assertIsNotNone(form, "reply form missing")
        self.assertNotRegex(form.group(0), r"\bhx-", "htmx must not bind to the reply form")

    def test_send_script_posts_json_to_the_messages_api(self):
        js = open(os.path.join(os.path.dirname(TEMPLATES), "static", "app.js"), encoding="utf-8").read()
        self.assertIn("/api/inbox/chats/${encodeURIComponent(E164)}/messages", js)

    def test_send_handler_only_uses_variables_that_exist(self):
        """Bug: the submit handler read `sendBtn`, which was never declared in its scope, so it threw a
        ReferenceError on the first line and nothing was ever sent."""
        js = open(os.path.join(os.path.dirname(TEMPLATES), "static", "app.js"), encoding="utf-8").read()
        handler = js.index('sendForm.addEventListener("submit"')
        body = js[handler:js.index("// Shift+Enter = newline", handler)]
        before = js[:handler]
        for ident in ("sendBtn", "textarea", "sugMetaEl", "ctx"):
            if re.search(r"\b%s\b" % ident, body):
                # must be declared at file level or thread-block level (2-4 space indent), NOT inside another function
                self.assertRegex(before, r"(?m)^ {2,4}(const|let|var)\s+%s\b" % ident,
                                 f"{ident} is used by the send handler but not declared in its scope")

    def test_send_failures_show_the_real_reason(self):
        """A Meta rejection comes back as {error: ...}; the admin must see it, not just 'FAILED: 502'."""
        js = open(os.path.join(os.path.dirname(TEMPLATES), "static", "app.js"), encoding="utf-8").read()
        self.assertRegex(js, r"data\.detail \|\| data\.error")


if __name__ == "__main__":
    unittest.main()
