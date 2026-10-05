"""Slices 3+4 (RED first): attachments show up in the thread and can be chosen in the reply box."""
import os
import re
import unittest

import jinja2

import media_rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "petrobind_frontend_app", "templates")


def render(messages):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    env.globals["asset_v"] = lambda: "t"
    return env.get_template("chat_thread.html").render(e164="60123456789", embed=True, messages=messages, admin_name="x", session_id="s",
                                                       last_buyer_wamid="", supa_url="", supa_anon_key="", contact_name="", is_admin=True)


def js():
    return open(os.path.join(ROOT, "petrobind_frontend_app", "static", "app.js"), encoding="utf-8").read()


class ThreadShowsAttachments(unittest.TestCase):
    def msg(self, **kw):
        base = {"id": 1, "wamid": "w1", "direction": "human", "text": "Here you go", "created_at": "2026-10-05T12:00:00", "held_by": "Mei Ling"}
        base.update(kw)
        return base

    def test_a_document_shows_its_file_name(self):
        html = render([self.msg(media_type="document", filename="COA 60-70.pdf")])
        self.assertIn('class="attachment-chip', html)
        self.assertIn("COA 60-70.pdf", html)
        self.assertIn("Here you go", html)

    def test_an_image_is_marked_as_a_photo(self):
        html = render([self.msg(media_type="image", filename="site.png", text="📎 site.png")])
        self.assertIn('class="attachment-chip', html)
        self.assertIn("site.png", html)

    def test_plain_messages_have_no_attachment_chip(self):
        self.assertNotIn('class="attachment-chip', render([self.msg()]))

    def test_the_file_name_is_escaped(self):
        html = render([self.msg(media_type="document", filename='<img src=x onerror=alert(1)>.pdf')])
        self.assertNotIn("<img src=x", html)


class ComposerHasAnAttachButton(unittest.TestCase):
    def setUp(self):
        self.html = render([])

    def test_paperclip_file_input_and_preview_exist(self):
        for needle in ('id="attach-btn"', 'id="attach-input"', 'id="attach-preview"'):
            self.assertIn(needle, self.html)

    def test_the_file_picker_offers_exactly_what_the_server_accepts(self):
        accept = re.search(r'id="attach-input"[^>]*accept="([^"]+)"', self.html).group(1)
        self.assertEqual(sorted(accept.split(",")), sorted(media_rules._TYPES))


class ScriptSendsAttachments(unittest.TestCase):
    def test_script_posts_the_file_as_form_data_to_the_attachments_endpoint(self):
        src = js()
        self.assertIn("/attachments", src)
        self.assertIn("new FormData()", src)
        self.assertRegex(src, r"json:\s*false")                     # FormData must not be turned into JSON

    def test_attaching_takes_over_from_the_ai_like_typing_does(self):
        self.assertRegex(js(), r"function setAttachment[\s\S]{0,2000}goHuman\(\)")

    def test_a_file_can_be_sent_without_any_caption_text(self):
        self.assertRegex(js(), r"if \(!text && !pendingFile\) return;")


if __name__ == "__main__":
    unittest.main()
