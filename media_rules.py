"""What may be sent to a buyer as an image or document through the WhatsApp Cloud API.

The file extension decides the type, but the file's own first bytes must agree (so a renamed program or a PNG called
.pdf is refused). Limits follow WhatsApp: images up to 5 MB (JPG/PNG), documents up to MAX_DOCUMENT_BYTES.
"""
import os
from typing import Dict

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = int(os.getenv("MAX_DOCUMENT_MB", "16")) * 1024 * 1024
MAX_FILENAME = 100

_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# extension -> (kind, mime, signature check)
_TYPES = {
    ".jpg": ("image", "image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image", "image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ".png": ("image", "image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    ".pdf": ("document", "application/pdf", lambda d: d.startswith(b"%PDF-")),
    ".docx": ("document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", lambda d: d.startswith(_ZIP)),
    ".xlsx": ("document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", lambda d: d.startswith(_ZIP)),
    ".pptx": ("document", "application/vnd.openxmlformats-officedocument.presentationml.presentation", lambda d: d.startswith(_ZIP)),
    ".doc": ("document", "application/msword", lambda d: d.startswith(_OLE)),
    ".xls": ("document", "application/vnd.ms-excel", lambda d: d.startswith(_OLE)),
    ".ppt": ("document", "application/vnd.ms-powerpoint", lambda d: d.startswith(_OLE)),
    ".txt": ("document", "text/plain", lambda d: b"\x00" not in d[:2048]),
}
ALLOWED_HELP = "images (JPG, PNG) and documents (PDF, Word, Excel, PowerPoint, TXT)"
ACCEPT_ATTR = ",".join(sorted(_TYPES))


class UploadRejected(ValueError):
    """The file may not be sent; str(exc) is a plain-language reason for the admin."""


def clean_filename(name: str) -> str:
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    stem, ext = os.path.splitext(base)
    return (stem[: MAX_FILENAME - len(ext)] + ext).strip() or "file"


def validate_upload(filename: str, content_type: str, data: bytes) -> Dict[str, str]:
    """Return {kind, mime, filename} for a sendable file, or raise UploadRejected."""
    name = clean_filename(filename)
    ext = os.path.splitext(name)[1].lower()
    spec = _TYPES.get(ext)
    if spec is None:
        raise UploadRejected(f"This file type can't be sent. Allowed: {ALLOWED_HELP}.")
    kind, mime, signature_ok = spec
    if not data:
        raise UploadRejected("The file is empty.")
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
    if len(data) > limit:
        raise UploadRejected(f"The file is too large (limit {limit // (1024 * 1024)} MB for {'images' if kind == 'image' else 'documents'}).")
    if not signature_ok(data):
        raise UploadRejected("The file's contents don't match its type (is it renamed or damaged?).")
    stem = os.path.splitext(name)[0]
    if os.path.splitext(stem)[1].lower() in {".exe", ".bat", ".cmd", ".com", ".scr", ".js", ".vbs", ".ps1", ".sh", ".msi", ".jar", ".html", ".htm", ".svg"}:
        raise UploadRejected("Files with a hidden program extension can't be sent.")
    return {"kind": kind, "mime": mime, "filename": name}
