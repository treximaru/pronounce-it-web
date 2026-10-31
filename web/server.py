#!/usr/bin/env python3
"""
Pronounce It! — web backend.

Read-only dictionary API + static frontend, stdlib only (no pip dependencies).

Endpoints:
    GET /                      -> web UI (static/index.html)
    GET /api/search?q=<query>  -> JSON list of entries, exact match first
    GET /api/word/<word>       -> JSON single entry (exact, case-insensitive)
    GET /api/stats             -> JSON {words, glossed}
    GET /api/tts?text=<word>   -> neural TTS mp3 (voice=sonia|ryan, rate=normal|slow),
                                  generated once via edge-tts then cached

Env:
    KAMUS_DB   path to kamus_v26.sqlite (default: ./kamus_v26.sqlite)
    PORT       listen port (default: 8888)
    BIND       listen address (default: 0.0.0.0)
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KAMUS_DB", os.path.join(BASE, "kamus_v26.sqlite"))
STATIC_DIR = os.path.join(BASE, "static")
TTS_CACHE = os.path.join(BASE, "tts_cache")
PORT = int(os.environ.get("PORT", "8888"))
BIND = os.environ.get("BIND", "0.0.0.0")

# Neural voices (Microsoft Edge TTS, free). Keys are what the API accepts.
TTS_VOICES = {"sonia": "en-GB-SoniaNeural",   # female
              "ryan": "en-GB-RyanNeural"}     # male
TTS_RATES = {"normal": "+0%", "slow": "-30%"}
TTS_WORD_RE = re.compile(r"^[a-zA-Z' -]{1,40}$")
TTS_TIMEOUT = 30

COLS = ("id, word, pos, definition_en, example, ipa_us, ipa_uk, ipa_other, "
        "syllables, syllable_count, stress_index, freq_rank, gloss_id, "
        "synonyms, antonyms, gloss_source, pos_all")

db_lock = threading.Lock()
db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(DB_PATH),
                     uri=True, check_same_thread=False)
db.row_factory = sqlite3.Row


def _escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_dicts(cur):
    return [dict(r) for r in cur.fetchall()]


def _typo_variants(q):
    """Yield (kind, pattern, rank) for single-typo variants of q.

    Covers the most common mistypes: swapped adjacent letters, one extra
    letter, one wrong letter, one missing letter. All patterns stay
    index-friendly (exact lookups or LIKE with a literal prefix).
    """
    n = len(q)
    seen = set()
    # transposition: "komupter" -> "komputer" (exact lookup)
    for i in range(n - 1):
        v = q[:i] + q[i + 1] + q[i] + q[i + 2:]
        if v != q and v not in seen:
            seen.add(v)
            yield ("exact", v, 30)
    # deletion: typed one extra letter, e.g. "kompuuter" -> "komputer"
    for i in range(n):
        v = q[:i] + q[i + 1:]
        if v not in seen:
            seen.add(v)
            yield ("exact", v, 31)
    # substitution: one wrong letter, e.g. "komxuter" -> "komputer"
    for i in range(n):
        yield ("like", _escape_like(q[:i]) + "_" + _escape_like(q[i + 1:]), 40)
    # insertion: missed one letter, e.g. "kompter" -> "komputer".
    # LENGTH = n+1 forces the wildcard to stand for exactly one letter,
    # so these are near-certain corrections -> rank right after exact ones.
    # (i=0 skipped: no literal prefix, full scan, lowest value)
    for i in range(1, n + 1):
        yield ("like_ins", _escape_like(q[:i]) + "%" + _escape_like(q[i:]), 32)


def _fuzzy_search(q, have, limit):
    """Append typo-tolerant matches for q. `have` is the set of words already shown."""
    found = []  # (rank, freq_rank, row)
    n = len(q)
    for kind, pattern, rank in _typo_variants(q):
        if kind == "exact":
            cur = db.execute(
                "SELECT %s FROM words WHERE word = ? LIMIT 3" % COLS, (pattern,))
        elif kind == "like":
            cur = db.execute(
                "SELECT %s FROM words WHERE word LIKE ? ESCAPE '\\' "
                "AND LENGTH(word) = ? LIMIT 5" % COLS, (pattern, n))
        else:  # like_ins: exactly one missing letter
            cur = db.execute(
                "SELECT %s FROM words WHERE word LIKE ? ESCAPE '\\' "
                "AND LENGTH(word) = ? LIMIT 5" % COLS,
                (pattern, n + 1))
        for r in cur.fetchall():
            d = dict(r)
            if d["word"] not in have:
                have.add(d["word"])
                found.append((rank, d.get("freq_rank") or 10 ** 9, d))
    found.sort(key=lambda x: (x[0], x[1]))
    return [d for _, _, d in found[:limit]]


def _prefix_typo_search(q, have, limit):
    """Last resort: the query is the damaged start of a longer word.

    e.g. "aproxima" -> "approximately" (missing 'p' + unfinished).
    Pattern: literal prefix, one gap for the typo, open suffix.
    Only runs when the direct + single-typo tiers found almost nothing.
    """
    n = len(q)
    found = []  # (prefix_len, freq_rank, row)
    for i in range(1, n):
        pat = _escape_like(q[:i]) + "%" + _escape_like(q[i:]) + "%"
        cur = db.execute(
            "SELECT %s FROM words WHERE word LIKE ? ESCAPE '\\' "
            "AND LENGTH(word) BETWEEN ? AND ? LIMIT 4" % COLS,
            (pat, n + 1, n + 6))
        for r in cur.fetchall():
            d = dict(r)
            if d["word"] not in have:
                have.add(d["word"])
                found.append((i, d.get("freq_rank") or 10 ** 9, d))
    found.sort(key=lambda x: (-x[0], x[1]))
    return [d for _, _, d in found[:limit]]


def search(query, limit=100):
    q = query.strip().lower()
    with db_lock:
        if not q:
            cur = db.execute(
                "SELECT %s FROM words "
                "ORDER BY (freq_rank IS NULL) ASC, freq_rank ASC, word ASC "
                "LIMIT ?" % COLS, (limit,))
            return _row_dicts(cur)
        e = _escape_like(q)
        cur = db.execute(
            "SELECT %s FROM words WHERE word LIKE ? ESCAPE '\\' "
            "ORDER BY (LOWER(word) = ?) DESC, (word LIKE ? ESCAPE '\\') DESC, "
            "(freq_rank IS NULL) ASC, freq_rank ASC, word ASC "
            "LIMIT ?" % COLS,
            ("%%%s%%" % e, q, "%s%%" % e, limit))
        results = _row_dicts(cur)
        # Typo tolerance: when the direct search is thin, try single-typo
        # variants (swapped / extra / wrong / missing letter).
        if len(results) < 10 and 3 <= len(q) <= 20 and " " not in q:
            have = {r["word"] for r in results}
            results += _fuzzy_search(q, have, limit - len(results))
        # Damaged prefix: query is the mistyped start of a longer word
        # (e.g. "aproxima" -> "approximately").
        if len(results) < 5 and 4 <= len(q) <= 20 and " " not in q:
            have = {r["word"] for r in results}
            results += _prefix_typo_search(q, have, limit - len(results))
        return results[:limit]


def lookup(word):
    with db_lock:
        cur = db.execute("SELECT %s FROM words WHERE word = ? LIMIT 1" % COLS,
                         (word.strip().lower(),))
        r = cur.fetchone()
        if r is None:
            return None
        d = dict(r)
        # known verb inflections (went, gone, ...) always list verb first
        pa = d.get("pos_all") or ""
        if d["word"] in IRREG_FORM and "verb" not in pa.split(","):
            d["pos_all"] = "verb," + pa if pa else "verb"
        vf = verb_forms(d["word"], d.get("pos"))
        if vf:
            d["verb_forms"] = vf
        return d


# ---------------------------------------------------------------------------
# Verb principal parts (v1 / v2 past / v3 past participle).
# The DB stores a single pos per word and it is unreliable for verbs
# ("go" is tagged noun), so verb detection uses this irregular table plus
# regular -ed rules, with every linked form verified against the DB.
# ---------------------------------------------------------------------------
_IRREGULAR_TXT = """
arise arose arisen
awake awoke awoken
be was/were been
bear bore born
beat beat beaten
become became become
begin began begun
bend bent bent
bet bet bet
bid bid bid
bind bound bound
bite bit bitten
bleed bled bled
blow blew blown
break broke broken
breed bred bred
bring brought brought
build built built
burn burnt burnt
burst burst burst
buy bought bought
cast cast cast
catch caught caught
choose chose chosen
cling clung clung
come came come
cost cost cost
creep crept crept
cut cut cut
deal dealt dealt
dig dug dug
do did done
draw drew drawn
dream dreamt dreamt
drink drank drunk
drive drove driven
eat ate eaten
fall fell fallen
feed fed fed
feel felt felt
fight fought fought
find found found
fit fit fit
flee fled fled
fling flung flung
fly flew flown
forbid forbade forbidden
forget forgot forgotten
forgive forgave forgiven
freeze froze frozen
get got got
give gave given
go went gone
grind ground ground
grow grew grown
hang hung hung
have had had
hear heard heard
hide hid hidden
hit hit hit
hold held held
hurt hurt hurt
keep kept kept
kneel knelt knelt
know knew known
lay laid laid
lead led led
lean leant leant
learn learnt learnt
leave left left
lend lent lent
let let let
lie lay lain
light lit lit
lose lost lost
make made made
mean meant meant
meet met met
pay paid paid
put put put
quit quit quit
read read read
ride rode ridden
ring rang rung
rise rose risen
run ran run
say said said
see saw seen
seek sought sought
sell sold sold
send sent sent
set set set
sew sewed sewn
shake shook shaken
shine shone shone
shoot shot shot
show showed shown
shrink shrank shrunk
shut shut shut
sing sang sung
sink sank sunk
sit sat sat
sleep slept slept
slide slid slid
smell smelt smelt
sow sowed sown
speak spoke spoken
speed sped sped
spell spelt spelt
spend spent spent
spill spilt spilt
spin spun spun
split split split
spoil spoilt spoilt
spread spread spread
spring sprang sprung
stand stood stood
steal stole stolen
stick stuck stuck
sting stung stung
stink stank stunk
strike struck struck
swear swore sworn
sweep swept swept
swim swam swum
swing swung swung
take took taken
teach taught taught
tear tore torn
tell told told
think thought thought
throw threw thrown
thrust thrust thrust
understand understood understood
wake woke woken
wear wore worn
weep wept wept
win won won
wind wound wound
withdraw withdrew withdrawn
wring wrung wrung
write wrote written
"""


def _parse_irregular():
    base2forms = {}
    form2base = {}
    for line in _IRREGULAR_TXT.strip().split("\n"):
        p = line.split()
        if len(p) != 3:
            continue
        base, v2, v3 = p
        v2s = v2.split("/")
        v3s = v3.split("/")
        base2forms[base] = (v2s, v3s)
        for v in v2s:
            form2base.setdefault(v, (base, "v2"))
        for v in v3s:
            form2base.setdefault(v, (base, "v3"))
    return base2forms, form2base


IRREG_BASE, IRREG_FORM = _parse_irregular()


def _regular_past_candidates(base):
    """Candidate v2/v3 spellings for a regular verb, simplest first."""
    b = base.lower()
    if b.endswith("e"):
        return [b + "d"]              # dance -> danced
    if b.endswith("c"):
        return [b + "ked"]            # panic -> panicked
    if len(b) > 2 and b.endswith("y") and b[-2] not in "aeiouy":
        return [b[:-1] + "ied"]       # study -> studied
    cands = [b + "ed"]                # play -> played
    # CVC doubling as alternative: stop -> stopped (DB check decides)
    if (len(b) >= 3 and b[-1] not in "aeiouwxy"
            and b[-2] in "aeiou" and b[-3] not in "aeiou"):
        cands.append(b + b[-1] + "ed")
    return cands


def _word_exists(w):
    cur = db.execute("SELECT 1 FROM words WHERE word = ? LIMIT 1", (w,))
    return cur.fetchone() is not None


def _regular_base_candidates(w):
    """Candidate base forms for a word that looks like a regular past tense."""
    cands = []
    if w.endswith("ied") and len(w) > 4:
        cands.append(w[:-3] + "y")     # studied -> study
    if w.endswith("ed") and len(w) > 3:
        stem = w[:-2]
        if (len(stem) >= 2 and stem[-1] == stem[-2]
                and stem[-1] not in "aeiou"):
            cands.append(stem[:-1])    # stopped -> stop
        cands.append(stem)             # wanted -> want
        cands.append(stem + "e")       # liked -> like
    return cands


def verb_forms(word, pos):
    """Return verb principal parts for a word, or None.

    {"v1": str, "v2": [str], "v3": [str], "kind": "base"|"v2"|"v3",
     "regular": bool, "links": {form: bool-in-db}}.
    Must be called with db_lock held.
    """
    w = word.lower()
    # 1) irregular, queried as base form
    if w in IRREG_BASE:
        v2s, v3s = IRREG_BASE[w]
        forms = [w] + v2s + v3s
        return {"v1": w, "v2": v2s, "v3": v3s, "kind": "base",
                "regular": False,
                "links": {f: _word_exists(f) for f in forms}}
    # 2) irregular, queried as v2/v3 -> point back to base
    if w in IRREG_FORM:
        base, kind = IRREG_FORM[w]
        v2s, v3s = IRREG_BASE[base]
        forms = [base] + v2s + v3s
        return {"v1": base, "v2": v2s, "v3": v3s, "kind": kind,
                "regular": False,
                "links": {f: _word_exists(f) for f in forms}}
    # 3) regular verb: DB says verb, or its -ed form exists as evidence
    v2 = None
    if pos == "verb":
        for c in _regular_past_candidates(w):
            if _word_exists(c):
                v2 = c
                break
        if v2 is None:
            v2 = _regular_past_candidates(w)[0]  # computed, shown as text
    else:
        for c in _regular_past_candidates(w):
            if _word_exists(c):
                v2 = c
                break
    if v2 is not None:
        return {"v1": w, "v2": [v2], "v3": [v2], "kind": "base",
                "regular": True,
                "links": {w: True, v2: _word_exists(v2)}}
    # 4) reverse regular: word looks like a past tense (played -> play)
    if w.endswith("ed"):
        for b in _regular_base_candidates(w):
            if b in IRREG_BASE:
                continue  # irregular bases handled above
            cur = db.execute("SELECT pos FROM words WHERE word = ?",
                             (b,))
            r = cur.fetchone()
            if r and r[0] == "verb" and _regular_past_candidates(b)[0] == w:
                v2c = _regular_past_candidates(b)
                v2ok = next((c for c in v2c if _word_exists(c)), v2c[0])
                return {"v1": b, "v2": [v2ok], "v3": [v2ok], "kind": "v2",
                        "regular": True,
                        "links": {b: True, v2ok: _word_exists(v2ok)}}
    return None


def stats():
    with db_lock:
        total = db.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        glossed = db.execute(
            "SELECT COUNT(*) FROM words "
            "WHERE gloss_id IS NOT NULL AND gloss_id <> ''").fetchone()[0]
        return {"words": total, "glossed": glossed}


tts_lock = threading.Lock()


def tts_mp3(text, voice_key, rate_key):
    """Return path to cached neural-TTTS mp3, generating it on first request."""
    voice = TTS_VOICES[voice_key]
    rate = TTS_RATES[rate_key]
    safe = text.strip().lower().replace(" ", "_")
    path = os.path.join(TTS_CACHE, voice_key, rate_key, safe + ".mp3")
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with tts_lock:
        if os.path.isfile(path):  # another thread beat us
            return path
        try:
            subprocess.run(
                [sys.executable, "-m", "edge_tts",
                 "--voice", voice, "--rate=%s" % rate,
                 "--text", text, "--write-media", tmp],
                timeout=TTS_TIMEOUT, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if not os.path.isfile(path):
        raise RuntimeError("tts generation failed")
    return path


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
            elif path == "/api/tts":
                qs = urllib.parse.parse_qs(parsed.query)
                text = qs.get("text", [""])[0]
                voice_key = qs.get("voice", ["sonia"])[0]
                rate_key = qs.get("rate", ["normal"])[0]
                if (not TTS_WORD_RE.match(text)
                        or voice_key not in TTS_VOICES
                        or rate_key not in TTS_RATES):
                    self._send_json({"error": "bad request"}, 400)
                    return
                try:
                    mp3 = tts_mp3(text, voice_key, rate_key)
                except Exception:  # noqa: BLE001 - fall back to browser TTS
                    self._send_json({"error": "tts unavailable"}, 502)
                    return
                with open(mp3, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
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
