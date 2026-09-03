"""
대화 세션 저장소 (SQLite).

conversation_id 를 키로:
  - conversations : 대화 메타 + 4개 에이전트의 conversationId 묶음(멀티턴 상태 유지용)
  - turns         : 대화 내 각 턴(사용자 요청 → 결과 보고서)
  - events        : 각 턴에서 오케스트레이터가 emit 한 스텝(라우터/분석/SQL/HTML/커널)

스레드에서 접근하므로(스트리밍 워커) 매 작업마다 커넥션을 새로 연다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    agent_convs TEXT,           -- JSON: {"router":id,"analysis":id,"sql":id,"html":id}
    created_at  REAL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS turns (
    conv_id     TEXT,
    seq         INTEGER,
    request     TEXT,
    need_sql    INTEGER,
    mock        INTEGER,
    report_url  TEXT,
    created_at  REAL,
    PRIMARY KEY (conv_id, seq)
);
CREATE TABLE IF NOT EXISTS events (
    conv_id     TEXT,
    turn_seq    INTEGER,
    ord         INTEGER,
    agent       TEXT,
    kind        TEXT,
    text        TEXT,
    data        TEXT,           -- JSON
    PRIMARY KEY (conv_id, turn_seq, ord)
);
"""


class ConversationStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    # ------------------------------------------------------------------
    def ensure_conversation(self, cid: str, title: str = ""):
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO conversations (id, title, agent_convs, created_at, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (cid, (title or "")[:120], "{}", now, now),
                )

    def get_agent_convs(self, cid: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT agent_convs FROM conversations WHERE id=?", (cid,)).fetchone()
        if not row or not row["agent_convs"]:
            return {}
        try:
            return json.loads(row["agent_convs"])
        except json.JSONDecodeError:
            return {}

    def set_agent_convs(self, cid: str, agent_convs: dict):
        with self._conn() as c:
            c.execute("UPDATE conversations SET agent_convs=?, updated_at=? WHERE id=?",
                      (json.dumps(agent_convs, ensure_ascii=False), time.time(), cid))

    def next_turn_seq(self, cid: str) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM turns WHERE conv_id=?",
                            (cid,)).fetchone()
        return int(row["m"]) + 1

    def add_turn(self, cid: str, seq: int, request: str, need_sql: bool,
                 mock: bool, report_url: str | None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO turns (conv_id, seq, request, need_sql, mock, report_url, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, seq, request, int(need_sql), int(mock), report_url, time.time()),
            )
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), cid))

    def add_events(self, cid: str, turn_seq: int, events: list[dict]):
        with self._conn() as c:
            for i, ev in enumerate(events):
                c.execute(
                    "INSERT OR REPLACE INTO events (conv_id, turn_seq, ord, agent, kind, text, data) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (cid, turn_seq, i, ev.get("agent", ""), ev.get("kind", ""),
                     ev.get("text", ""), json.dumps(ev.get("data"), ensure_ascii=False)),
                )

    def set_title_if_empty(self, cid: str, title: str):
        with self._conn() as c:
            row = c.execute("SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is not None and not (row["title"] or "").strip():
                c.execute("UPDATE conversations SET title=? WHERE id=?", ((title or "")[:120], cid))

    # ------------------------------------------------------------------
    def list_conversations(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT cv.id, cv.title, cv.updated_at, "
                "  (SELECT COUNT(*) FROM turns t WHERE t.conv_id=cv.id) AS turns "
                "FROM conversations cv ORDER BY cv.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"],
                 "turns": r["turns"]} for r in rows]

    def load_conversation(self, cid: str) -> dict | None:
        with self._conn() as c:
            conv = c.execute("SELECT id, title, agent_convs, created_at, updated_at "
                             "FROM conversations WHERE id=?", (cid,)).fetchone()
            if conv is None:
                return None
            turns = c.execute("SELECT seq, request, need_sql, mock, report_url, created_at "
                              "FROM turns WHERE conv_id=? ORDER BY seq", (cid,)).fetchall()
            evrows = c.execute("SELECT turn_seq, ord, agent, kind, text, data "
                               "FROM events WHERE conv_id=? ORDER BY turn_seq, ord", (cid,)).fetchall()
        ev_by_turn: dict[int, list] = {}
        for e in evrows:
            try:
                data = json.loads(e["data"]) if e["data"] else None
            except json.JSONDecodeError:
                data = None
            ev_by_turn.setdefault(e["turn_seq"], []).append(
                {"agent": e["agent"], "kind": e["kind"], "text": e["text"], "data": data})
        return {
            "id": conv["id"], "title": conv["title"],
            "turns": [{
                "seq": t["seq"], "request": t["request"],
                "need_sql": bool(t["need_sql"]), "mock": bool(t["mock"]),
                "report_url": t["report_url"], "events": ev_by_turn.get(t["seq"], []),
            } for t in turns],
        }

    def delete_conversation(self, cid: str):
        with self._conn() as c:
            c.execute("DELETE FROM events WHERE conv_id=?", (cid,))
            c.execute("DELETE FROM turns WHERE conv_id=?", (cid,))
            c.execute("DELETE FROM conversations WHERE id=?", (cid,))
