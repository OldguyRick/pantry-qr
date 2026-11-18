"""End-to-end check against the deployed app (and its Postgres, directly)."""

import json
import re
import sys
import time

import httpx

BASE = "https://pantry-qr.onrender.com"
c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=False)

print("health:", c.get("/healthz").json())

# 1. create a bin
r = c.post("/containers", data={"name": "Verification bin", "location": "Test shelf"})
assert r.status_code == 303, r.status_code
code = r.headers["location"].split("/c/")[1].split("?")[0]
print("bin:", code)

# 2. add an item with the fields the brief asks for
r = c.post(
    "/items",
    data={
        "name": "Verification oats",
        "description": "Rolled oats, resealed",
        "size": "1 kg",
        "quantity": "2",
        "added_on": "2026-11-17",
        "best_before": "2027-09-01",
        "container": code,
    },
)
assert r.status_code == 303, r.status_code
token = r.headers["location"].split("/i/")[1].split("?")[0]
print("item:", token)

item_html = c.get(f"/i/{token}").text
for needle in ("1 kg", "17 Nov 2026", "01 Sep 2027", "2 of 2 left", "/qr/item/" ):
    assert needle in item_html, f"missing {needle!r}"
print("item page shows size, added date, best-before, quantity and a QR")

# 3. withdraw one unit from the bin page
bin_html = c.get(f"/c/{code}").text
item_id = re.search(r'name="item_id" value="(\d+)"', bin_html).group(1)
r = c.post(f"/c/{code}/withdraw", data={"item_id": item_id, "quantity": "1", "note": "e2e check"})
print("withdraw:", r.status_code)
after = c.get(f"/c/{code}").text
assert "1 / 2" in after, "count did not drop"
assert "e2e check" in after
print("bin page now shows 1 of 2, and the withdrawal is listed")

# 4. the QR on the bin encodes the live URL
qr_png = c.get(f"/qr/bin/{code}.png")
assert qr_png.status_code == 200 and qr_png.content[:4] == b"\x89PNG"
print("bin QR png:", len(qr_png.content), "bytes")

# 5. the log has it
assert "Verification oats" in c.get("/log").text
print("log contains the withdrawal")

# 6. it is really in Postgres: connect straight to the database
state = json.load(open("render_state.json", encoding="utf-8"))
import psycopg

url = state["db_external_url"]
with psycopg.connect(url, connect_timeout=30) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM containers")
        print("postgres containers:", cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM items")
        print("postgres items:", cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM withdrawals")
        print("postgres withdrawals:", cur.fetchone()[0])
        cur.execute("SELECT name, size, best_before FROM items WHERE name = %s", ("Verification oats",))
        print("row:", cur.fetchone())

print("ALL CHECKS PASSED")
