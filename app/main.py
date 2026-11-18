"""Pantry QR — food inventory with printable QR labels and dynamic withdrawals."""

from __future__ import annotations

import os
import secrets
import string
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, qr


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    print(f"[pantry-qr] database backend: {db.backend()}", flush=True)
    if os.environ.get("SEED_DEMO", "1") == "1":
        seed_demo()
    yield


app = FastAPI(title="Pantry QR", docs_url=None, redoc_url=None, lifespan=lifespan)

HERE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


def base_url(request: Request) -> str:
    """Public origin. Render terminates TLS, so honour the forwarded proto."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def new_code() -> str:
    return "BIN-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))


def new_token() -> str:
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


def fmt_day(value) -> str:
    """Dates are stored as ISO text; timestamps may arrive as datetime objects."""
    if not value:
        return "—"
    text = str(value)[:10]
    try:
        return date.fromisoformat(text).strftime("%d %b %Y")
    except ValueError:
        return text


def fmt_ts(value) -> str:
    if not value:
        return "—"
    text = str(value).replace("T", " ")[:16]
    return text


def fmt_ago(value) -> str:
    """Human 'x ago' for the activity rail; falls back to an absolute date."""
    if not value:
        return "—"
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("T", " ").strip()
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return fmt_ts(value)

    if parsed.tzinfo is not None:
        moment = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        moment = parsed  # CURRENT_TIMESTAMP is UTC in both engines
    secs = int((datetime.now(timezone.utc).replace(tzinfo=None) - moment).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    if secs < 7 * 86400:
        return f"{secs // 86400} d ago"
    return moment.strftime("%d %b %Y")


templates.env.filters["day"] = fmt_day
templates.env.filters["ts"] = fmt_ts
templates.env.filters["ago"] = fmt_ago
templates.env.globals["qr_svg"] = qr.svg


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    ctx.setdefault("today", date.today().strftime("%A %d %B"))
    return templates.TemplateResponse(request, name, ctx)


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    containers = db.query(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM items i WHERE i.container_id = c.id) AS item_count,
               (SELECT COALESCE(SUM(i.quantity), 0) FROM items i WHERE i.container_id = c.id) AS unit_count
        FROM containers c ORDER BY c.name
        """
    )
    items = db.get_items("WHERE i.container_id IS NOT NULL")
    for c in containers:
        c["stocked"] = sum(1 for i in items if i["container_id"] == c["id"] and i["in_stock"])
        c["on_hand"] = sum(i["remaining"] for i in items if i["container_id"] == c["id"])
    loose = [i for i in items if not i["in_stock"]]
    recent = db.query(
        "SELECT * FROM withdrawals ORDER BY withdrawn_at DESC, id DESC LIMIT 8"
    )
    stats = {
        "containers": len(containers),
        "item_types": len(items),
        "on_hand": sum(i["remaining"] for i in items),
        "out": len(loose),
    }
    expiring = db.get_items(
        "WHERE i.best_before != '' AND i.best_before <= ?", [date.today().isoformat()]
    )
    return page(
        request,
        "index.html",
        stats=stats,
        containers=containers,
        recent=recent,
        expiring=[i for i in expiring if i["in_stock"]],
    )


# --------------------------------------------------------------------------- #
# containers
# --------------------------------------------------------------------------- #
@app.post("/containers")
def create_container(
    name: str = Form(...), location: str = Form("")
):
    name = name.strip() or "Untitled bin"
    code = new_code()
    while db.query("SELECT 1 FROM containers WHERE code = ?", [code]):
        code = new_code()
    db.execute(
        "INSERT INTO containers (code, name, location) VALUES (?, ?, ?)",
        [code, name, location.strip()],
    )
    return RedirectResponse(f"/c/{code}?msg=Container+created", status_code=303)


@app.get("/c/{code}", response_class=HTMLResponse)
def container_page(request: Request, code: str):
    rows = db.query("SELECT * FROM containers WHERE code = ?", [code])
    if not rows:
        raise HTTPException(404, "No container with that code")
    container = rows[0]
    items = db.get_items("WHERE i.container_id = ?", [container["id"]])
    history = db.query(
        "SELECT * FROM withdrawals WHERE container_code = ? ORDER BY withdrawn_at DESC, id DESC LIMIT 25",
        [code],
    )
    return page(
        request,
        "container.html",
        container=container,
        items=items,
        history=history,
        container_url=f"{base_url(request)}/c/{code}",
    )


@app.post("/c/{code}/delete")
def delete_container(code: str):
    rows = db.query("SELECT id FROM containers WHERE code = ?", [code])
    if not rows:
        raise HTTPException(404, "No container with that code")
    # Items survive as unassigned rather than vanishing with the bin.
    db.execute("UPDATE items SET container_id = NULL WHERE container_id = ?", [rows[0]["id"]])
    db.execute("DELETE FROM containers WHERE id = ?", [rows[0]["id"]])
    return RedirectResponse("/?msg=Container+deleted", status_code=303)


@app.post("/c/{code}/withdraw")
def withdraw_from_container(
    code: str,
    item_id: int = Form(...),
    quantity: int = Form(1),
    note: str = Form(""),
):
    rows = db.query("SELECT * FROM containers WHERE code = ?", [code])
    if not rows:
        raise HTTPException(404, "No container with that code")
    return _withdraw(item_id, max(1, quantity), note, redirect_to=f"/c/{code}")


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
@app.get("/items/new", response_class=HTMLResponse)
def new_item_form(request: Request, container: str = ""):
    containers = db.query("SELECT * FROM containers ORDER BY name")
    return page(
        request,
        "item_form.html",
        containers=containers,
        preset=container,
        today=date.today().isoformat(),
    )


@app.post("/items")
def create_item(
    name: str = Form(...),
    description: str = Form(""),
    size: str = Form(""),
    quantity: int = Form(1),
    added_on: str = Form(""),
    best_before: str = Form(""),
    container: str = Form(""),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/items/new?err=Name+is+required", status_code=303)
    container_id = None
    if container:
        rows = db.query("SELECT id FROM containers WHERE code = ?", [container])
        if rows:
            container_id = rows[0]["id"]
    token = new_token()
    while db.query("SELECT 1 FROM items WHERE token = ?", [token]):
        token = new_token()
    db.execute(
        """INSERT INTO items (token, name, description, size, quantity, added_on, best_before, container_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            token,
            name,
            description.strip(),
            size.strip(),
            max(1, quantity),
            added_on or date.today().isoformat(),
            best_before,
            container_id,
        ],
    )
    return RedirectResponse(f"/i/{token}?msg=Item+added", status_code=303)


@app.get("/i/{token}", response_class=HTMLResponse)
def item_page(request: Request, token: str):
    items = db.get_items("WHERE i.token = ?", [token])
    if not items:
        raise HTTPException(404, "No item with that code")
    item = items[0]
    history = db.query(
        "SELECT * FROM withdrawals WHERE item_id = ? ORDER BY withdrawn_at DESC, id DESC",
        [item["id"]],
    )
    containers = db.query("SELECT * FROM containers ORDER BY name")
    return page(
        request,
        "item.html",
        item=item,
        history=history,
        containers=containers,
        item_url=f"{base_url(request)}/i/{token}",
    )


@app.post("/i/{token}/withdraw")
def withdraw_from_item(
    token: str, quantity: int = Form(1), note: str = Form("")
):
    items = db.get_items("WHERE i.token = ?", [token])
    if not items:
        raise HTTPException(404, "No item with that code")
    return _withdraw(items[0]["id"], max(1, quantity), note, redirect_to=f"/i/{token}")


@app.post("/i/{token}/delete")
def delete_item(token: str):
    items = db.query("SELECT id FROM items WHERE token = ?", [token])
    if not items:
        raise HTTPException(404, "No item with that code")
    db.execute("DELETE FROM items WHERE id = ?", [items[0]["id"]])
    return RedirectResponse("/?msg=Item+deleted", status_code=303)


@app.post("/i/{token}/move")
def move_item(token: str, container: str = Form("")):
    items = db.query("SELECT id FROM items WHERE token = ?", [token])
    if not items:
        raise HTTPException(404, "No item with that code")
    container_id = None
    if container:
        rows = db.query("SELECT id FROM containers WHERE code = ?", [container])
        container_id = rows[0]["id"] if rows else None
    db.execute("UPDATE items SET container_id = ? WHERE id = ?", [container_id, items[0]["id"]])
    return RedirectResponse(f"/i/{token}?msg=Moved", status_code=303)


def _withdraw(item_id: int, quantity: int, note: str, redirect_to: str):
    rows = db.get_items("WHERE i.id = ?", [item_id])
    if not rows:
        raise HTTPException(404, "No such item")
    item = rows[0]
    if not item["in_stock"]:
        return RedirectResponse(f"{redirect_to}?err=Nothing+left+to+take", status_code=303)
    take = min(quantity, item["remaining"])
    container_code = item["container_code"] or ""
    db.execute(
        """INSERT INTO withdrawals (item_id, container_code, item_name, quantity, note)
           VALUES (?, ?, ?, ?, ?)""",
        [item_id, container_code, item["name"], take, note.strip()],
    )
    msg = f"Took+{take}+x+{item['name'].replace(' ', '+')}"
    return RedirectResponse(f"{redirect_to}?msg={msg}", status_code=303)


# --------------------------------------------------------------------------- #
# log, labels, scan
# --------------------------------------------------------------------------- #
@app.get("/log", response_class=HTMLResponse)
def withdrawal_log(request: Request):
    rows = db.query("SELECT * FROM withdrawals ORDER BY withdrawn_at DESC, id DESC LIMIT 300")
    daily = db.query(
        """SELECT SUBSTR(CAST(withdrawn_at AS TEXT), 1, 10) AS day, SUM(quantity) AS units
           FROM withdrawals GROUP BY day ORDER BY day DESC LIMIT 14"""
    )
    return page(request, "log.html", rows=rows, daily=daily)


@app.get("/labels", response_class=HTMLResponse)
def labels(request: Request, kind: str = "containers", container: str = ""):
    base = base_url(request)
    if kind == "items":
        where, params = "WHERE 1=1", []
        if container:
            where += " AND c.code = ?"
            params.append(container)
        rows = db.get_items(where, params)
        cards = [
            {
                "title": i["name"],
                "line1": " ".join(x for x in [i["size"], f"qty {i['quantity']}"] if x),
                "line2": i["container_code"] or "unassigned",
                "url": f"{base}/i/{i['token']}",
                "code": i["token"],
            }
            for i in rows
        ]
    else:
        rows = db.query("SELECT * FROM containers ORDER BY name")
        cards = [
            {
                "title": c["name"],
                "line1": c["location"] or "no location",
                "line2": c["code"],
                "url": f"{base}/c/{c['code']}",
                "code": c["code"],
            }
            for c in rows
        ]
    return page(request, "labels.html", cards=cards, kind=kind)


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request):
    return page(request, "scan.html")


@app.get("/qr/item/{token}.png")
def qr_item_png(request: Request, token: str):
    return Response(content=qr.png(f"{base_url(request)}/i/{token}", scale=10), media_type="image/png")


@app.get("/qr/bin/{code}.png")
def qr_bin_png(request: Request, code: str):
    return Response(content=qr.png(f"{base_url(request)}/c/{code}", scale=10), media_type="image/png")


@app.get("/healthz")
def healthz():
    db.query("SELECT 1")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
SEED_CONTAINERS = [
    ("BIN-A1", "Dry goods bin", "Pantry shelf 1"),
    ("BIN-B2", "Fridge crisper", "Fridge middle shelf"),
    ("BIN-C3", "Freezer basket", "Freezer top"),
]

SEED_ITEMS = [
    ("Basmati rice", "Long grain, 5 kg sack decanted", "5 kg", 1, "2026-11-01", "2028-06-01", "BIN-A1"),
    ("Black beans", "Dried, resealed bag", "900 g", 2, "2026-10-20", "2027-12-31", "BIN-A1"),
    ("Olive oil", "Extra virgin tin", "1 L", 1, "2026-09-14", "2027-08-01", "BIN-A1"),
    ("Greek yoghurt", "Family tub", "1 kg", 2, "2026-11-15", "2026-11-24", "BIN-B2"),
    ("Baby spinach", "Washed bag", "300 g", 1, "2026-11-16", "2026-11-20", "BIN-B2"),
    ("Chicken thighs", "Portioned and frozen", "1.2 kg", 2, "2026-10-28", "2027-04-28", "BIN-C3"),
]


def seed_demo() -> None:
    if db.query("SELECT 1 FROM containers LIMIT 1"):
        return
    for code, name, location in SEED_CONTAINERS:
        db.execute(
            "INSERT INTO containers (code, name, location) VALUES (?, ?, ?)",
            [code, name, location],
        )
    for name, desc, size, qty, added, best, code in SEED_ITEMS:
        rows = db.query("SELECT id FROM containers WHERE code = ?", [code])
        db.execute(
            """INSERT INTO items (token, name, description, size, quantity, added_on, best_before, container_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [new_token(), name, desc, size, qty, added, best, rows[0]["id"] if rows else None],
        )

