"""Correction capture: SQLite (stdlib, zero-ops on Windows Server)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY, ts REAL, input TEXT, output TEXT, provider TEXT, model TEXT,
              cost_usd REAL, latency_ms INTEGER, guard_exact INTEGER, guard_unaligned INTEGER);
            CREATE TABLE IF NOT EXISTS corrections(
              id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, ts REAL, corrected TEXT,
              editor TEXT, note TEXT, der REAL,
              FOREIGN KEY(request_id) REFERENCES requests(id));
            """
        )

    def log_request(self, rid, text, out, res, info):
        with self.lock:
            self.db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, time.time(), text, out, res.provider, res.model, res.cost_usd, res.latency_ms, int(info["exact"]), info["unaligned"]),
            )
            self.db.commit()

    def get_request(self, rid):
        row = self.db.execute("SELECT input, output FROM requests WHERE id=?", (rid,)).fetchone()
        return {"input": row[0], "output": row[1]} if row else None

    def add_correction(self, rid, corrected, editor, note, der):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO corrections(request_id, ts, corrected, editor, note, der) VALUES(?,?,?,?,?,?)",
                (rid, time.time(), corrected, editor, note, der),
            )
            self.db.commit()
            return cur.lastrowid

    def export_jsonl(self):
        q = """SELECT r.input, r.output, c.corrected, r.model, c.der, c.editor FROM corrections c
               JOIN requests r ON r.id=c.request_id ORDER BY c.id"""
        for inp, out, cor, model, der, ed in self.db.execute(q):
            yield json.dumps({"input": inp, "model_output": out, "corrected": cor, "model": model, "der": der, "editor": ed}, ensure_ascii=False) + "\n"

    def stats(self):
        n, cost = self.db.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM requests").fetchone()
        c, der = self.db.execute("SELECT COUNT(*), AVG(der) FROM corrections").fetchone()
        return {"requests": n, "cost_usd": round(cost, 4), "corrections": c, "avg_der_on_corrected": der}
