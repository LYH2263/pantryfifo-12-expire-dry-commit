import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            c.rollback(); c.close(); raise HTTPException(400, result["reason"])
        if not result["ok"]:
            c.rollback(); c.close(); raise HTTPException(409, result)
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit()
    except HTTPException:
        raise
    except Exception:
        c.rollback()
        c.close()
        raise
    c.close(); return result

def _sweep_candidates(c, today: str) -> list[dict]:
    """Commit-time generation: still on_shelf, with stock, expiry strictly before today."""
    return [dict(r) for r in c.execute(
        """SELECT lots.id, lots.item_id, lots.qty_remain, lots.expiry, items.name, items.layer
           FROM lots JOIN items ON items.id=lots.item_id
           WHERE lots.status='on_shelf' AND lots.qty_remain>0
             AND lots.expiry IS NOT NULL AND lots.expiry < ?""", (today,))]

@app.get("/api/expire-sweep/preview")
def expire_sweep_preview():
    """Dry run: read-only. Lists lots expired before today that are still on the shelf."""
    today = date.today().isoformat()
    c = connect()
    lots = _sweep_candidates(c, today)
    c.close()
    return {"as_of": today, "lots": lots}

class SweepIn(BaseModel):
    ids: list[int] | None = None

@app.post("/api/expire-sweep")
def expire_sweep(body: SweepIn | None = None):
    """Commit the sweep.

    The list is recomputed inside one IMMEDIATE transaction against the
    commit-time generation, so a lot inserted (already expired) after the dry
    run is not missed and a lot consumed meanwhile is not marked expired.
    UPDATE is guarded by status='on_shelf' AND qty_remain>0 so a second commit
    never rewrites an already expired lot. Any failure rolls the whole
    transaction back; full-layer / layer-page / alert-bar read the same DB and
    therefore return to the pre-commit generation together.
    """
    preview_ids = set((body.ids if body and body.ids is not None else []))
    today = date.today().isoformat()
    c = connect()
    c.isolation_level = None  # explicit transaction control
    try:
        c.execute("BEGIN IMMEDIATE")
        current_ids = [l["id"] for l in _sweep_candidates(c, today)]
        if current_ids:
            marks = ",".join("?" for _ in current_ids)
            c.execute(
                f"UPDATE lots SET status='expired' "
                f"WHERE id IN ({marks}) AND status='on_shelf' AND qty_remain>0",
                current_ids)
        c.commit()
    except Exception:
        c.rollback()
        c.close()
        raise
    current_set = set(current_ids)
    c.close()
    return {
        "as_of": today,
        "expired_ids": current_ids,
        "added_after_preview": sorted(current_set - preview_ids),
        "gone_after_preview": sorted(preview_ids - current_set),
    }

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
