"""干跑/提交世代一致性的测试。

pytest 风格；本地没有 pytest 时也可直接运行：
    python3 -m app.tests.test_sweep   （在 backend/ 下）
"""

import os
import tempfile
from datetime import date, timedelta

from app import seed
from app.db import connect
from app.engines import sweep


def _setup_db(monkey_today: str):
    """起一个临时库并灌入固定批次，返回 (tmpdir, today)。"""
    tmp = tempfile.mkdtemp(prefix="pantryfifo-sweep-")
    os.environ["DATA_DIR"] = tmp
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM lots")
    today = date.fromisoformat(monkey_today)
    yest = (today - timedelta(days=2)).isoformat()
    old = (today - timedelta(days=10)).isoformat()
    future = (today + timedelta(days=30)).isoformat()
    c.executemany(
        "INSERT INTO lots(id,item_id,qty_in,qty_remain,expiry,status,data_quality) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (1, 1, 1, 1, old, "on_shelf", "clean"),      # 已过期、在架 -> 应下架
            (2, 1, 1, 1, yest, "on_shelf", "clean"),     # 已过期、在架 -> 应下架
            (3, 2, 12, 12, future, "on_shelf", "clean"),  # 未到期 -> 不得带走
            (4, 3, 1, 1, old, "expired", "clean"),       # 已下架 -> 幂等不重写
            (5, 2, 1, 0, yest, "consumed", "clean"),     # 消费光 -> 不碰
            (6, 1, 1, -1, old, "on_shelf", "dirty"),     # 脏数据负余量 -> 不选
        ],
    )
    c.commit()
    c.close()

    orig_today = sweep._today
    sweep._today = lambda: monkey_today
    return tmp, orig_today


def _restore(orig_today):
    sweep._today = orig_today
    os.environ.pop("DATA_DIR", None)


def _statuses():
    c = connect()
    rows = {r["id"]: (r["status"], r["qty_remain"]) for r in c.execute("SELECT id,status,qty_remain FROM lots")}
    c.close()
    return rows


def test_dry_run_is_read_only_and_selects_only_due_on_shelf():
    _, orig = _setup_db("2026-10-05")
    try:
        before = _statuses()
        r = sweep.dry_run()
        assert r["dry_run"] is True
        assert sorted(x["id"] for x in r["expired"]) == [1, 2]
        assert _statuses() == before  # 全层与顶条集合不变
    finally:
        _restore(orig)


def test_commit_expires_due_only_and_is_idempotent():
    _, orig = _setup_db("2026-10-05")
    try:
        r = sweep.commit()
        assert r["dry_run"] is False
        assert r["changed_ids"] == [1, 2]
        st = _statuses()
        assert st[1][0] == "expired" and st[2][0] == "expired"
        assert st[3][0] == "on_shelf"   # 未到期不带走
        assert st[4][0] == "expired"    # 原本就 expired，未被改写
        assert st[5][0] == "consumed"
        assert st[6][0] == "on_shelf"

        # 同一批第二次提交：不得再写一遍
        r2 = sweep.commit()
        assert r2["changed_ids"] == [] and r2["swept_count"] == 0
        assert _statuses() == st
    finally:
        _restore(orig)


def test_commit_collapses_generation_with_new_inbound_and_concurrent_consume():
    """干跑之后：新入库一笔同样已过期的批，名单里另一批被消费打光。
    提交必须收成同一世代：新批带走、被消费批跳过。"""
    _, orig = _setup_db("2026-10-05")
    try:
        preview = sweep.dry_run()
        preview_ids = [x["id"] for x in preview["expired"]]
        assert preview_ids == [1, 2]

        c = connect()
        # 有人消费干跑名单里的批 1，打到 0 -> consumed（复刻 /consume 行为）
        c.execute("UPDATE lots SET qty_remain=0, status='consumed' WHERE id=1")
        # 同时新入库一笔同样已过期的新批
        c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) "
            "VALUES (1,1,1,'2026-09-20','on_shelf','clean')"
        )
        c.commit()
        new_id = c.execute("SELECT MAX(id) m FROM lots").fetchone()["m"]
        c.close()

        r = sweep.commit(preview_ids)
        assert r["skipped_ids"] == [1]          # 已被消费，跳过
        assert r["new_ids"] == [new_id]         # 新入库过期批，本次带走
        assert sorted(r["changed_ids"]) == sorted([2, new_id])
        assert r["changed_ids"].count(1) == 0   # 顶条已无该批，全层也不许留
        st = _statuses()
        assert st[1] == ("consumed", 0)
        assert st[2][0] == "expired"
        assert st[new_id][0] == "expired"
        assert st[3][0] == "on_shelf"           # 未到期不带走
    finally:
        _restore(orig)


def test_commit_rolls_back_everything_on_mid_failure():
    _, orig = _setup_db("2026-10-05")
    try:
        before = _statuses()
        try:
            sweep.commit(fail_after_select=True)
            assert False, "应当抛错"
        except RuntimeError:
            pass
        # 全层、层页、顶条一起回到提交前
        assert _statuses() == before
        # 回滚后仍可正常提交，证明锁与事务已释放干净
        r = sweep.commit()
        assert r["changed_ids"] == [1, 2]
    finally:
        _restore(orig)


def test_inbound_and_consume_serialize_against_open_commit():
    """提交事务持锁窗口内，入库必须排队，不允许夹出“顶条已下架、全层还在”的中间态。"""
    _, orig = _setup_db("2026-10-05")
    c = connect()
    try:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        due = sweep._select_expired(c)
        assert {x["id"] for x in due} == {1, 2}
        c.execute("UPDATE lots SET status='expired' WHERE id IN (1,2) AND status='on_shelf'")

        # 另一连接的入库不能穿过未提交的窗口
        other = connect()
        other.execute("PRAGMA busy_timeout=200")
        blocked = False
        try:
            other.execute(
                "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) "
                "VALUES (1,1,1,'2026-09-19','on_shelf','clean')"
            )
            other.commit()
        except Exception:
            blocked = True
        assert blocked, "写锁窗口内入库必须等待"
        other.close()

        c.execute("COMMIT")
        st = _statuses()
        assert st[1][0] == "expired" and st[2][0] == "expired"
    finally:
        c.close()
        _restore(orig)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
