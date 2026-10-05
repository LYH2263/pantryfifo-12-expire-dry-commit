"""过期下架：干跑预览与提交。

干跑（dry_run）只读，列出到期日早于今天、仍在架且有余量的批号，全层与顶条集合不变；
提交（commit）在一个 IMMEDIATE 事务里按提交瞬间重算同一世代：

- 干跑之后新入库的、同样已过期的批，被同一事务捕获（new_ids）；
- 干跑名单里的批若在此期间被消费到 0（status 已变 consumed/expired），
  事务重算时自然剔除（skipped_ids）——顶条与全层不会出现世代错位；
- 只选 status='on_shelf' 的行，已经 expired 的批第二次提交 swept 为空，不会再写一遍；
- 事务中途任何失败都整体回滚，全层、层页、顶条回到提交前。

选取规则沿用 engines.fefo.expire_lots 纯函数，规则只有一处出处。
"""

from datetime import date

from app.db import connect
from app.engines.fefo import expire_lots


def _today() -> str:
    return date.today().isoformat()


def _on_shelf_rows(c) -> list[dict]:
    return [
        dict(r)
        for r in c.execute(
            """SELECT lots.*, items.name, items.layer, items.unit
               FROM lots JOIN items ON items.id = lots.item_id
               WHERE lots.status='on_shelf'"""
        )
    ]


def _payload(lots: list[dict]) -> list[dict]:
    return [
        {
            "id": l["id"],
            "item_id": l["item_id"],
            "name": l["name"],
            "layer": l["layer"],
            "unit": l["unit"],
            "qty_remain": l["qty_remain"],
            "expiry": l["expiry"],
        }
        for l in lots
    ]


def _select_expired(c) -> list[dict]:
    """在调用方持有的事务/连接上，选出提交瞬间应下架的批。"""
    rows = _on_shelf_rows(c)
    ids = set(expire_lots(rows, _today()))
    return [r for r in rows if r["id"] in ids]


def dry_run() -> dict:
    """只读预览，不改库；全层与顶条集合不变。"""
    c = connect()
    try:
        due = _select_expired(c)
    finally:
        c.close()
    return {"today": _today(), "dry_run": True, "expired": _payload(due)}


def commit(preview_ids: list[int] | None = None, fail_after_select: bool = False) -> dict:
    """原子提交。返回提交世代的下架名单及与干跑名单的对账差异。

    preview_ids: 干跑预览给出的批号，用于分类 new_ids / skipped_ids。
    fail_after_select: 测试钩子——选出行之后、写库之前抛错，验证整单回滚。
    """
    preview_ids = list(preview_ids or [])
    c = connect()
    due: list[dict] = []
    try:
        c.isolation_level = None  # 手动事务
        c.execute("BEGIN IMMEDIATE")  # 立刻拿保留锁，与入库/消费串行化
        try:
            due = _select_expired(c)
            due_ids = [l["id"] for l in due]

            if fail_after_select:
                raise RuntimeError("injected failure after selecting rows")

            if due_ids:
                marks = ",".join("?" * len(due_ids))
                # 双重守卫：这些行从事务内选取到写回之间不可能被别的事务改动
                # （IMMEDIATE 锁），AND status='on_shelf' 仍保留，语义自证。
                c.execute(
                    f"UPDATE lots SET status='expired' WHERE id IN ({marks}) AND status='on_shelf'",
                    due_ids,
                )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    finally:
        c.close()

    due_id_set = set(l["id"] for l in due)
    preview_set = set(preview_ids)
    return {
        "today": _today(),
        "dry_run": False,
        "expired": _payload(due),
        "changed_ids": sorted(due_id_set),
        "swept_count": len(due_id_set),
        "preview_ids": sorted(preview_set),
        # 干跑后新入库、被本次提交一并带走的过期批
        "new_ids": sorted(due_id_set - preview_set),
        # 干跑名单里、提交前已被消费下架而跳过的批
        "skipped_ids": sorted(preview_set - due_id_set),
    }
