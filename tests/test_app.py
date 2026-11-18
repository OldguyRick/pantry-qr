import os
import re
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point the app at a throwaway SQLite file BEFORE importing it.
_TMP = tempfile.mkdtemp(prefix="pantryqr-")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["DATABASE_URL"] = ""
os.environ["SEED_DEMO"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "The pantry, at a glance" in r.text


def test_container_lifecycle_and_withdrawal(client):
    # create a bin
    r = client.post("/containers", data={"name": "Test bin", "location": "Shelf 9"}, follow_redirects=False)
    assert r.status_code == 303
    code = r.headers["location"].split("/c/")[1].split("?")[0]
    assert code.startswith("BIN-")

    page = client.get(f"/c/{code}")
    assert page.status_code == 200
    assert code in page.text
    assert "<svg" in page.text  # QR embedded inline

    # add an item into that bin
    r = client.post(
        "/items",
        data={
            "name": "Test lentils",
            "description": "Red split",
            "size": "500 g",
            "quantity": "3",
            "added_on": "2026-11-01",
            "best_before": "2028-01-01",
            "container": code,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    token = r.headers["location"].split("/i/")[1].split("?")[0]

    item_page = client.get(f"/i/{token}")
    assert "Test lentils" in item_page.text
    assert "3 of 3 left" in item_page.text

    # brand, size and dates all surface on the item page
    assert "500 g" in item_page.text
    assert "01 Nov 2026" in item_page.text
    assert "01 Jan 2028" in item_page.text

    # the bin page now lists it
    bin_page = client.get(f"/c/{code}")
    assert "Test lentils" in bin_page.text

    # withdraw 2 of 3
    _m = re.search(r'name="item_id" value="(\d+)"', bin_page.text)
    assert _m is not None, "no withdraw form in the bin page"
    item_id = _m.group(1)
    r = client.post(
        f"/c/{code}/withdraw",
        data={"item_id": item_id, "quantity": "2", "note": "dinner"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    bin_page = client.get(f"/c/{code}")
    assert "1 / 3" in bin_page.text
    assert "Test lentils" in client.get("/log").text

    # withdrawing more than is left takes only the remainder
    r = client.post(f"/i/{token}/withdraw", data={"quantity": "99", "note": "greedy"}, follow_redirects=False)
    assert r.status_code == 303
    item_page = client.get(f"/i/{token}")
    assert "0 of 3 left" in item_page.text
    assert "Nothing left in this item" in item_page.text

    # a further withdrawal is refused, not double counted
    r = client.post(f"/i/{token}/withdraw", data={"quantity": "1"}, follow_redirects=False)
    assert "err=Nothing" in r.headers["location"]
    assert "0 of 3 left" in client.get(f"/i/{token}").text


def test_item_move_between_bins(client):
    r = client.post("/containers", data={"name": "Bin A", "location": ""}, follow_redirects=False)
    code_a = r.headers["location"].split("/c/")[1].split("?")[0]
    r = client.post("/containers", data={"name": "Bin B", "location": ""}, follow_redirects=False)
    code_b = r.headers["location"].split("/c/")[1].split("?")[0]

    r = client.post(
        "/items",
        data={"name": "Moving oats", "quantity": "1", "container": code_a},
        follow_redirects=False,
    )
    token = r.headers["location"].split("/i/")[1].split("?")[0]

    client.post(f"/i/{token}/move", data={"container": code_b}, follow_redirects=False)
    assert "Moving oats" in client.get(f"/c/{code_b}").text
    assert "Moving oats" not in client.get(f"/c/{code_a}").text


def test_labels_and_qr_png(client):
    r = client.post("/containers", data={"name": "Label bin", "location": "Fridge"}, follow_redirects=False)
    code = r.headers["location"].split("/c/")[1].split("?")[0]

    page = client.get("/labels?kind=containers")
    assert page.status_code == 200
    assert code in page.text

    png = client.get(f"/qr/bin/{code}.png")
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_not_found(client):
    assert client.get("/c/BIN-NOPE").status_code == 404
    assert client.get("/i/nope").status_code == 404


def test_delete_bin_keeps_items_and_delete_item(client):
    r = client.post("/containers", data={"name": "Doomed bin", "location": ""}, follow_redirects=False)
    code = r.headers["location"].split("/c/")[1].split("?")[0]
    r = client.post("/items", data={"name": "Orphan oats", "quantity": "1", "container": code}, follow_redirects=False)
    token = r.headers["location"].split("/i/")[1].split("?")[0]

    assert client.post(f"/c/{code}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/c/{code}").status_code == 404
    # the item survives, just unassigned
    item_page = client.get(f"/i/{token}")
    assert item_page.status_code == 200
    assert "unassigned" in item_page.text

    assert client.post(f"/i/{token}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/i/{token}").status_code == 404


def test_scan_page(client):
    r = client.get("/scan")
    assert r.status_code == 200
    assert "html5-qrcode" in r.text
