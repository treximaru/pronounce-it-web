#!/usr/bin/env python3
"""
Pronounce It! — web backend.

Read-only dictionary API + static frontend, stdlib only (no pip dependencies).

Endpoints:
    GET /                      -> web UI (static/index.html)
    GET /api/search?q=<query>  -> JSON list of entries, exact match first
    GET /api/word/<word>       -> JSON single entry (exact, case-insensitive)
    GET /api/stats             -> JSON {words, glossed}

Env:
    KAMUS_DB   path to kamus_v26.sqlite (default: ./kamus_v26.sqlite)
    PORT       listen port (default: 8888)
    BIND       listen address (default: 0.0.0.0)
"""
import json
import os
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KAMUS_DB", os.path.join(BASE, "kamus_v26.sqlite"))
STATIC_DIR = os.path.join(BASE, "static")
PORT = int(os.environ.get("PORT", "8888"))
BIND = os.environ.get("BIND", "0.0.0.0")

COLS = ("id, word, pos, definition_en, example, ipa_us, ipa_uk, ipa_other, "
        "syllables, syllable_count, stress_index, freq_rank, gloss_id, "
        "synonyms, antonyms, gloss_source")

db_lock = threading.Lock()
db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(DB_PATH),
                     uri=True, check_same_thread=False)
db.row_factory = sqlite3.Row


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(query, limit=100):
    q = query.strip().lower()
    with db_lock:
        if not q:
            cur = db.execute(
                "SELECT %s FROM words "
                "ORDER BY (freq_rank IS NULL) ASC, freq_rank ASC, word ASC "
                "LIMIT ?" % COLS, (limit,))
        else:
            e = _escape_like(q)
            cur = db.execute(
                "SELECT %s FROM words WHERE word LIKE ? ESCAPE '\\' "
                "ORDER BY (LOWER(word) = ?) DESC, (word LIKE ? ESCAPE '\\') DESC, "
                "(freq_rank IS NULL) ASC, freq_rank ASC, word ASC "
                "LIMIT ?" % COLS,
                ("%%%s%%" % e, q, "%s%%" % e, limit))
        return [dict(r) for r in cur.fetchall()]


def lookup(word):
    with db_lock:
        cur = db.execute("SELECT %s FROM words WHERE word = ? LIMIT 1" % COLS,
                         (word.strip().lower(),))
        r = cur.fetchone()
        return dict(r) if r else None


def stats():
    with db_lock:
        total = db.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        glossed = db.execute(
            "SELECT COUNT(*) FROM words "
            "WHERE gloss_id IS NOT NULL AND gloss_id <> ''").fetchone()[0]
        return {"words": total, "glossed": glossed}


MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png", ".svg": "image/svg+xml",
        ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PronounceItWeb/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        # block traversal, allow only safe extensions
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.splitext(rel)[1] not in MIME:
            self.send_error(404)
            return
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME[os.path.splitext(rel)[1]])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/stats":
                self._send_json(stats())
            elif path == "/api/search":
                qs = urllib.parse.parse_qs(parsed.query)
                q = qs.get("q", [""])[0]
                try:
                    limit = max(1, min(int(qs.get("limit", ["100"])[0]), 200))
                except ValueError:
                    limit = 100
                self._send_json(search(q, limit))
            elif path.startswith("/api/word/"):
                word = urllib.parse.unquote(path[len("/api/word/"):])
                entry = lookup(word)
                if entry is None:
                    self._send_json({"error": "not found"}, 404)
                else:
                    self._send_json(entry)
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - never crash on a bad request
            try:
                self._send_json({"error": "internal error"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def log_message(self, fmt, *args):
        pass  # quiet; use a reverse proxy's logs


if __name__ == "__main__":
    if not os.path.isfile(DB_PATH):
        raise SystemExit("database not found: %s (set KAMUS_DB)" % DB_PATH)
    # quick sanity check
    print("words: %d" % stats()["words"], flush=True)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print("Pronounce It! web on http://%s:%d" % (BIND, PORT), flush=True)
    httpd.serve_forever()
