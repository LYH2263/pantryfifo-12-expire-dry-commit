"""Expire-sweep dry run + commit generation/idempotency/rollback tests."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
import app.main as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(main.app) as c:
        yield c


def _add_item(c, name="测试奶"):
    cur = c.execute("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", (name, "upper", "盒"))
    return cur.lastrowid


def _add_lot(c, item_id, qty, expiry):
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, "on_shelf", "clean"))
    return cur.lastrowid


def _status(c, lot_id):
    return c.execute("SELECT status FROM lots WHERE id=?", (lot_id,)).fetchone()["status"]


def test_preview_is_dry_run_and_read_only(client):
    c = connect()
    iid = _add_item(c)
    old = _add_lot(c, iid, 1, (date.today() - timedelta(days=2)).isoformat())
    c.commit(); c.close()

    r = client.get("/api/expire-sweep/preview")
    assert r.status_code == 200
    data = r.json()
    assert data["as_of"] == date.today().isoformat()
    assert old in [l["id"] for l in data["lots"]]
    # dry run writes nothing
    c = connect()
    assert _status(c, old) == "on_shelf"
    c.close()


def test_commit_converges_to_one_generation(client):
    """After preview: a new expired lot is inbound and a preview lot gets consumed.

    Commit must expire the union-minus-gone set atomically: new lot included,
    consumed lot excluded, unexpired lots untouched.
    """
    today = date.today().isoformat()
    c = connect()
    iid = _add_item(c)
    will_be_consumed = _add_lot(c, iid, 2, (date.today() - timedelta(days=1)).isoformat())
    will_expire = _add_lot(c, iid, 1, (date.today() - timedelta(days=3)).isoformat())
    due_today = _add_lot(c, iid, 1, today)                          # expiry == today: stays
    future = _add_lot(c, iid, 1, (date.today() + timedelta(days=5)).isoformat())
    c.commit(); c.close()

    preview_ids = [l["id"] for l in client.get("/api/expire-sweep/preview").json()["lots"]]
    assert will_be_consumed in preview_ids and will_expire in preview_ids
    assert due_today not in preview_ids and future not in preview_ids

    c = connect()
    # someone consumes the preview lot to zero while reviewer is reading the list
    c.execute("UPDATE lots SET qty_remain=0, status='consumed' WHERE id=?", (will_be_consumed,))
    # a brand-new, already-expired lot arrives after the dry run
    late_expired = _add_lot(c, iid, 1, (date.today() - timedelta(days=6)).isoformat())
    c.commit(); c.close()

    r = client.post("/api/expire-sweep", json={"ids": preview_ids})
    assert r.status_code == 200
    out = r.json()
    assert late_expired in out["added_after_preview"]
    assert will_be_consumed in out["gone_after_preview"]
    expired = set(out["expired_ids"])
    assert will_expire in expired and late_expired in expired
    assert will_be_consumed not in expired
    assert due_today not in expired and future not in expired

    c = connect()
    assert _status(c, will_expire) == "expired"
    assert _status(c, late_expired) == "expired"
    assert _status(c, will_be_consumed) == "consumed"   # not flipped to expired
    assert _status(c, due_today) == "on_shelf"
    assert _status(c, future) == "on_shelf"
    c.close()


def test_second_commit_does_not_rewrite_expired(client):
    c = connect()
    iid = _add_item(c)
    old = _add_lot(c, iid, 1, (date.today() - timedelta(days=2)).isoformat())
    c.execute("CREATE TABLE update_log(n INTEGER DEFAULT 0)")
    c.execute("INSERT INTO update_log VALUES (0)")
    c.execute("""CREATE TRIGGER lot_update_counter AFTER UPDATE ON lots
                 BEGIN UPDATE update_log SET n = n + 1; END""")
    c.commit(); c.close()

    first = client.post("/api/expire-sweep", json={"ids": []}).json()
    assert old in first["expired_ids"]

    c = connect()
    writes = c.execute("SELECT n FROM update_log").fetchone()["n"]
    c.close()

    # second commit simulates pressing submit once more with a fresh, empty list
    second = client.post("/api/expire-sweep", json={"ids": []}).json()
    assert second["expired_ids"] == []
    assert second["added_after_preview"] == [] and second["gone_after_preview"] == []

    c = connect()
    # no lot UPDATE happened on the second commit
    assert c.execute("SELECT n FROM update_log").fetchone()["n"] == writes
    assert _status(c, old) == "expired"
    c.close()


def test_mid_commit_failure_rolls_everything_back(client):
    c = connect()
    iid = _add_item(c)
    old = _add_lot(c, iid, 1, (date.today() - timedelta(days=2)).isoformat())
    other = _add_lot(c, iid, 2, (date.today() - timedelta(days=4)).isoformat())
    c.commit(); c.close()

    c = connect()
    c.execute("CREATE TRIGGER fail_sweep AFTER UPDATE ON lots "
              "WHEN NEW.status='expired' "
              "BEGIN SELECT RAISE(ABORT, 'sweep boom'); END")
    c.commit(); c.close()

    bad = TestClient(main.app, raise_server_exceptions=False)
    r = bad.post("/api/expire-sweep", json={"ids": []})
    assert r.status_code == 500

    c = connect()
    # full layer generation is back to pre-commit state: nothing is expired
    assert _status(c, old) == "on_shelf"
    assert _status(c, other) == "on_shelf"
    assert c.execute("SELECT COUNT(*) n FROM lots WHERE status='expired'").fetchone()["n"] == 0
    c.close()
