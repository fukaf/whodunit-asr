"""Local SQLite speaker registry.

Single-user, trusted local environment: no encryption and no permission model.
Vectors are stored as L2-normalised float32 blobs; a few dozen speakers is
cheap enough to compare by brute force.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _vector_ok(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    if v.size == 0:
        raise ValueError("empty embedding")
    norm = float(np.linalg.norm(v))
    if not norm:
        raise ValueError("zero embedding")
    return v / norm


class SpeakerRegistry:
    """CRUD + matching support for enrolled speaker identities."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS speakers (
                id            TEXT PRIMARY KEY,
                display_name  TEXT NOT NULL,
                anchor        BLOB NOT NULL,
                created_at    TEXT NOT NULL,
                consent_scope TEXT,
                consented_at  TEXT,
                revoked_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS centroids (
                id            TEXT PRIMARY KEY,
                speaker_id    TEXT NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
                vector        BLOB NOT NULL,
                source_file   TEXT,
                source_start  REAL,
                source_end    REAL,
                score         REAL,
                condition     TEXT,
                status        TEXT NOT NULL DEFAULT 'active',
                created_at    TEXT NOT NULL,
                consented_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                action      TEXT NOT NULL,
                speaker_id  TEXT,
                centroid_id TEXT,
                source_file TEXT,
                score       REAL,
                detail      TEXT
            );
            CREATE TABLE IF NOT EXISTS adapt_buffers (
                speaker_id TEXT PRIMARY KEY REFERENCES speakers(id) ON DELETE CASCADE,
                vector_sum BLOB NOT NULL,
                turns      INTEGER NOT NULL,
                seconds    REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS score_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                speaker_id TEXT NOT NULL,
                score      REAL NOT NULL,
                ts         TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # -- audit -------------------------------------------------------------
    def _audit(self, action: str, **fields: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, action, speaker_id, centroid_id, source_file, score, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (_now(), action, fields.get("speaker_id"), fields.get("centroid_id"),
             fields.get("source_file"), fields.get("score"),
             json.dumps(fields.get("detail") or {}, ensure_ascii=False)),
        )

    # -- speakers ----------------------------------------------------------
    def add_speaker(self, display_name: str, vector: np.ndarray,
                    consent_scope: str | None = None, source_file: str | None = None,
                    condition: dict[str, Any] | None = None) -> str:
        vec = _vector_ok(vector)
        sid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO speakers (id, display_name, anchor, created_at, consent_scope, consented_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, display_name, _blob(vec), _now(), consent_scope, _now() if consent_scope else None),
        )
        self._audit("enroll", speaker_id=sid, source_file=source_file,
                    detail={"condition": condition or {}})
        self.conn.commit()
        return sid

    def list_speakers(self, include_revoked: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM speakers"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at, id"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def get_speaker(self, speaker_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM speakers WHERE id=?", (speaker_id,)).fetchone()
        return dict(row) if row else None

    def rename_speaker(self, speaker_id: str, name: str) -> None:
        self.conn.execute("UPDATE speakers SET display_name=? WHERE id=?", (name, speaker_id))
        self._audit("rename", speaker_id=speaker_id, detail={"name": name})
        self.conn.commit()

    def revoke_speaker(self, speaker_id: str) -> None:
        self.conn.execute("UPDATE speakers SET revoked_at=? WHERE id=?", (_now(), speaker_id))
        self._audit("revoke", speaker_id=speaker_id)
        self.conn.commit()

    def delete_speaker(self, speaker_id: str) -> None:
        self._audit("delete", speaker_id=speaker_id)
        self.conn.execute("DELETE FROM speakers WHERE id=?", (speaker_id,))
        self.conn.commit()

    # -- centroids ---------------------------------------------------------
    def add_centroid(self, speaker_id: str, vector: np.ndarray,
                     status: str = "active", source_file: str | None = None,
                     source_start: float | None = None, source_end: float | None = None,
                     score: float | None = None,
                     condition: dict[str, Any] | None = None) -> str:
        speaker = self.get_speaker(speaker_id)
        if not speaker:
            raise KeyError(f"unknown speaker: {speaker_id}")
        vec = _vector_ok(vector)
        if vec.size != _vec(speaker["anchor"]).size:
            raise ValueError("embedding dimension does not match the registered speaker")
        cid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO centroids (id, speaker_id, vector, source_file, source_start, "
            "source_end, score, condition, status, created_at, consented_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, speaker_id, _blob(vec), source_file, source_start, source_end, score,
             json.dumps(condition or {}, ensure_ascii=False), status, _now(),
             speaker.get("consented_at")),
        )
        self._audit("adapt" if status == "active" else "adapt_pending",
                    speaker_id=speaker_id, centroid_id=cid, source_file=source_file,
                    score=score, detail={"status": status})
        self.conn.commit()
        return cid

    def list_centroids(self, speaker_id: str | None = None,
                       status: str = "active") -> list[dict[str, Any]]:
        sql = "SELECT * FROM centroids WHERE status=?"
        args: list[Any] = [status]
        if speaker_id:
            sql += " AND speaker_id=?"
            args.append(speaker_id)
        sql += " ORDER BY created_at"
        rows = self.conn.execute(sql, args).fetchall()
        return [{**dict(r), "vector": _vec(r["vector"]),
                 "condition": json.loads(r["condition"] or "{}")} for r in rows]

    def approve_centroid(self, centroid_id: str) -> None:
        self.conn.execute("UPDATE centroids SET status='active' WHERE id=?", (centroid_id,))
        self._audit("adapt_approve", centroid_id=centroid_id)
        self.conn.commit()

    def delete_centroid(self, centroid_id: str) -> None:
        self._audit("adapt_delete", centroid_id=centroid_id)
        self.conn.execute("DELETE FROM centroids WHERE id=?", (centroid_id,))
        self.conn.commit()

    def active_candidates(self) -> list[dict[str, Any]]:
        """Non-revoked speakers with anchor + active adapted centroids."""
        out = []
        for row in self.list_speakers():
            vecs = [_vec(row["anchor"])]
            vecs.extend(c["vector"] for c in self.list_centroids(row["id"], "active"))
            out.append({"id": row["id"], "display_name": row["display_name"], "vectors": vecs})
        return out

    # -- adaptive buffer ---------------------------------------------------
    def update_buffer(self, speaker_id: str, vector: np.ndarray, seconds: float,
                      min_turns: int, min_seconds: float,
                      source_file: str | None = None,
                      condition: dict[str, Any] | None = None) -> str | None:
        """Accumulate embeddings; return a new pending centroid id once ready."""
        speaker = self.get_speaker(speaker_id)
        if not speaker:
            return None
        vec = _vector_ok(vector)
        row = self.conn.execute("SELECT * FROM adapt_buffers WHERE speaker_id=?",
                                (speaker_id,)).fetchone()
        if row:
            total = _vec(row["vector_sum"]) + vec
            turns = int(row["turns"]) + 1
            secs = float(row["seconds"]) + float(seconds)
        else:
            total, turns, secs = vec, 1, float(seconds)
        self.conn.execute(
            "INSERT INTO adapt_buffers (speaker_id, vector_sum, turns, seconds, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(speaker_id) DO UPDATE SET "
            "vector_sum=excluded.vector_sum, turns=excluded.turns, "
            "seconds=excluded.seconds, updated_at=excluded.updated_at",
            (speaker_id, _blob(total), turns, secs, _now()),
        )
        self.conn.commit()
        if turns < min_turns or secs < min_seconds:
            return None
        self.conn.execute("DELETE FROM adapt_buffers WHERE speaker_id=?", (speaker_id,))
        mean = total / turns
        norm = float(np.linalg.norm(mean))
        mean = mean / norm if norm else mean
        cid = self.add_centroid(speaker_id, mean, status="pending", source_file=source_file,
                                condition=condition)
        self.conn.commit()
        return cid

    # -- score history -----------------------------------------------------
    def add_score(self, speaker_id: str, score: float) -> None:
        self.conn.execute("INSERT INTO score_history (speaker_id, score, ts) VALUES (?,?,?)",
                          (speaker_id, float(score), _now()))
        self.conn.commit()

    def drift_flag(self, speaker_id: str, window: int, threshold: float) -> bool:
        rows = self.conn.execute(
            "SELECT score FROM score_history WHERE speaker_id=? "
            "ORDER BY id DESC LIMIT ?", (speaker_id, int(window))).fetchall()
        if len(rows) < window:
            return False
        return all(float(r["score"]) < threshold for r in rows)
