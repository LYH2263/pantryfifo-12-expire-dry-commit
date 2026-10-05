import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    # 提交事务持写锁期间，让并发的入库/消费稍等而不是立刻 SQLITE_BUSY，
    # 保证“有人正在消费干跑名单里的批”时两者串行、收成同一世代。
    c.execute("PRAGMA busy_timeout=5000")
    return c
