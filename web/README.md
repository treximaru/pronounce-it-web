# Pronounce It! — Web

Web version of the **English Dictionary: Pronounce It!** Android app
(main repo: [treximaru/pronounce-it-english-indo-dictionary](https://github.com/treximaru/pronounce-it-english-indo-dictionary)).

Same dataset (91,556 words, UK IPA first, Indonesian gloss), served on demand —
the browser only downloads what you search. Audio uses the browser's built-in
speech engine (UK English voice when available).

## Run

Requirements: Python 3.8+ (stdlib only), and `kamus_v26.sqlite` next to
`server.py` (or set `KAMUS_DB`).

```bash
cd web
python3 server.py            # http://0.0.0.0:8888  (PORT / BIND env overrides)
```

## API

- `GET /api/search?q=<query>` — entries, exact match first (same ranking as the app)
- `GET /api/word/<word>` — single entry
- `GET /api/stats` — `{words, glossed}`

## Deploy (batam)

```bash
# on the server, under ~/muse/pronounce-it-web/
cp web/server.py web/static -r ~/muse/pronounce-it-web/
cp kamus_v26.sqlite ~/muse/pronounce-it-web/
sudo cp deploy/pronounce-it-web.service /etc/systemd/system/
sudo systemctl enable --now pronounce-it-web
```
