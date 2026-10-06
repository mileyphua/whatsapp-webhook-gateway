"""Tests must never talk to the live Render/Supabase that the developer's .env points at."""
import os

os.environ["RENDER_INBOX_URL"] = ""
