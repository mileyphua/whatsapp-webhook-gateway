"""Slice 1 (RED first): which files may be sent to a buyer. The extension is not trusted: the bytes must match."""
import unittest

import media_rules as mr

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64
DOCX = b"PK\x03\x04" + b"\x00" * 64          # docx/xlsx/pptx are zip containers
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64   # legacy .doc/.xls/.ppt
TXT = b"Plain text datasheet notes\n"


class ValidateUpload(unittest.TestCase):
    def ok(self, name, data, ctype=""):
        return mr.validate_upload(name, ctype, data)

    def test_accepts_images_and_the_common_document_types(self):
        cases = [("photo.jpg", JPG, "image", "image/jpeg"), ("photo.JPEG", JPG, "image", "image/jpeg"), ("site.png", PNG, "image", "image/png"),
                 ("COA.pdf", PDF, "document", "application/pdf"), ("pds.docx", DOCX, "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                 ("prices.xlsx", DOCX, "document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                 ("deck.pptx", DOCX, "document", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                 ("old.doc", OLE, "document", "application/msword"), ("old.xls", OLE, "document", "application/vnd.ms-excel"),
                 ("notes.txt", TXT, "document", "text/plain")]
        for name, data, kind, mime in cases:
            r = self.ok(name, data)
            self.assertEqual((r["kind"], r["mime"]), (kind, mime), name)

    def test_the_extension_alone_is_not_trusted(self):
        for name, data in (("fake.pdf", PNG), ("fake.png", PDF), ("fake.jpg", PDF), ("fake.docx", PNG), ("fake.pdf", TXT)):
            with self.assertRaises(mr.UploadRejected, msg=name):
                self.ok(name, data)

    def test_dangerous_or_unsupported_types_are_refused(self):
        for name in ("run.exe", "page.html", "image.svg", "script.js", "archive.zip", "macro.docm", "tool.bat", "video.mp4", "noextension"):
            with self.assertRaises(mr.UploadRejected, msg=name):
                self.ok(name, PDF)

    def test_double_extensions_cannot_smuggle_a_program(self):
        with self.assertRaises(mr.UploadRejected):
            self.ok("invoice.pdf.exe", PDF)

    def test_empty_and_oversized_files_are_refused(self):
        with self.assertRaises(mr.UploadRejected):
            self.ok("a.pdf", b"")
        with self.assertRaises(mr.UploadRejected):
            self.ok("big.png", PNG + b"\x00" * (5 * 1024 * 1024))                 # WhatsApp limit for images is 5 MB
        with self.assertRaises(mr.UploadRejected):
            self.ok("big.pdf", PDF + b"\x00" * (mr.MAX_DOCUMENT_BYTES))

    def test_file_names_are_cleaned(self):
        r = self.ok("../../etc/Petrobind COA (final).pdf", PDF)
        self.assertEqual(r["filename"], "Petrobind COA (final).pdf")
        self.assertEqual(self.ok("a" * 300 + ".pdf", PDF)["filename"], "a" * 96 + ".pdf")
        self.assertEqual(self.ok("C:\\docs\\x.pdf", PDF)["filename"], "x.pdf")

    def test_the_reason_is_in_plain_language(self):
        with self.assertRaises(mr.UploadRejected) as cm:
            self.ok("run.exe", PDF)
        self.assertIn("images (JPG, PNG)", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
