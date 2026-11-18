# Pantry QR

Food inventory where every **item** and every **storage container** carries a QR code.
Scanning a container shows what is inside it *right now*; taking something out records
a withdrawal and decrements the count. Nothing is baked into the label — the QR only
holds a URL, so a printed sticker never goes stale.

**Live app:** https://pantry-qr.onrender.com (see the deployment notes below)

## What it does

| Feature | Where |
|---|---|
| Item record: name, description, size, quantity, added date, best-before | `/i/<token>` |
| Printable QR per item, pointing at that item's page | `/qr/item/<token>.png`, `/labels?kind=items` |
| Container record with a unique code (`BIN-XXXX`) | `/c/<code>` |
| Container QR showing live contents + per-item "Take out" | `/c/<code>` |
| Withdrawals: partial counts, `min(requested, remaining)`, full audit log | `/log` |
| Best-before watchlist on the dashboard | `/` |
| Phone camera scanner (works on iOS/Android over HTTPS) | `/scan` |
| Move an item between containers | item page |

Quantity left is **derived**, not stored twice: `SUM(withdrawals.quantity)` is subtracted
from `items.quantity`, so the ledger and the counts can never disagree.

## Stack

- FastAPI + Jinja2 server-rendered pages (no build step, no JS framework)
- Postgres on Render (`DATABASE_URL`); SQLite automatically when it is absent, so local
  dev and tests need no database server
- `segno` for QR generation — pure Python, so no native image libraries in the image
- Single-stage `python:3.12-slim` Dockerfile, non-root-friendly, `$PORT` aware

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000  (seeds three demo bins + six items on first run)
```

Set `SEED_DEMO=0` to start empty. Data lives in `data/pantry.db` for the SQLite path.

## Test

```bash
pytest -q
```

Covers the container lifecycle, item creation, partial withdrawals, over-withdrawal
clamping, moving items, delete semantics, label rendering and the QR PNG endpoint.

## Lint & type-check

`basedpyright` runs against every Python file at **0 errors, 0 warnings** (config in
`pyrightconfig.json`, `standard` mode with unused imports/variables promoted to errors):

```bash
pip install -r requirements-dev.txt
basedpyright
```

CI-worthy gate: `basedpyright && pytest -q`.

## Deploy on Render

```bash
docker build -t pantry-qr .
docker run -p 8000:8000 -e SEED_DEMO=1 pantry-qr
```

On Render: an **existing Postgres** instance supplies `DATABASE_URL`, and the web
service is `runtime: docker` with `autoDeploy` from `main`. `render.yaml` in this repo
describes the same thing as a blueprint.

## Layout

```
app/main.py       routes (dashboard, containers, items, withdrawals, labels, scan)
app/db.py         SQL with `?` placeholders, rewritten to %s for psycopg
app/qr.py         inline SVG / PNG QR rendering
app/templates/    Jinja2 pages
app/static/       one hand-written stylesheet, print styles for labels
tests/            pytest suite against SQLite
```
