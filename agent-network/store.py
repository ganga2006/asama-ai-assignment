"""
store.py — SQLite data model for the agent social network.

Author: Gangadhar Korakuppa (ganga2006)

Data model (the four nouns the problem asks for, plus notifications):

  agents         who is on the platform and what they care about
  posts          what agents publish
  comments       responses to posts
  subscriptions  explicit topic follows (interests are implicit subs;
                 this table holds extra follows added later)
  notifications  the routing decision log — doubles as the FEEDBACK
                 dataset: `engaged` flips to 1 when a notified agent
                 actually comments, so the router can learn.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    company     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    interests   TEXT NOT NULL DEFAULT '[]',   -- JSON list of topic strings
    webhook_url TEXT NOT NULL DEFAULT '',     -- push delivery endpoint (optional)
    api_key     TEXT NOT NULL UNIQUE,
    verified    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    text        TEXT NOT NULL,
    topics      TEXT NOT NULL DEFAULT '[]',   -- JSON list of topic strings
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    topic       TEXT NOT NULL,
    PRIMARY KEY (agent_id, topic)
);
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES agents(id),
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    score       REAL NOT NULL,
    reasons     TEXT NOT NULL DEFAULT '',
    engaged     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_agent ON notifications(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
"""


class Store:
    def __init__(self, path: str = "network.db"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()          # serialize writes across threads
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _q(self, sql, args=()):
        return self._db.execute(sql, args).fetchall()

    # ---- agents ----
    def create_agent(self, name, company, description, interests, webhook_url=""):
        api_key = "ak_" + secrets.token_hex(16)
        cur = self._exec(
            "INSERT INTO agents (name, company, description, interests, webhook_url,"
            " api_key, created_at) VALUES (?,?,?,?,?,?,?)",
            (name, company, description, json.dumps(sorted(set(interests))),
             webhook_url, api_key, time.time()))
        return self.agent_by_id(cur.lastrowid), api_key

    def agent_by_id(self, agent_id):
        rows = self._q("SELECT * FROM agents WHERE id=?", (agent_id,))
        return dict(rows[0]) if rows else None

    def agent_by_key(self, api_key):
        rows = self._q("SELECT * FROM agents WHERE api_key=?", (api_key,))
        return dict(rows[0]) if rows else None

    def all_agents(self):
        return [dict(r) for r in self._q("SELECT * FROM agents ORDER BY id")]

    def set_verified(self, agent_id, verified=True):
        self._exec("UPDATE agents SET verified=? WHERE id=?",
                   (1 if verified else 0, agent_id))

    def subscribe(self, agent_id, topic):
        self._exec("INSERT OR IGNORE INTO subscriptions (agent_id, topic) VALUES (?,?)",
                   (agent_id, topic.lower().strip()))

    def subscriptions_of(self, agent_id):
        return [r["topic"] for r in
                self._q("SELECT topic FROM subscriptions WHERE agent_id=?", (agent_id,))]

    # ---- posts & comments ----
    def create_post(self, agent_id, text, topics):
        cur = self._exec(
            "INSERT INTO posts (agent_id, text, topics, created_at) VALUES (?,?,?,?)",
            (agent_id, text, json.dumps([t.lower().strip() for t in topics]), time.time()))
        return self.post_by_id(cur.lastrowid)

    def post_by_id(self, post_id):
        rows = self._q("SELECT * FROM posts WHERE id=?", (post_id,))
        return dict(rows[0]) if rows else None

    def recent_posts(self, limit=50):
        return [dict(r) for r in
                self._q("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))]

    def create_comment(self, post_id, agent_id, text):
        cur = self._exec(
            "INSERT INTO comments (post_id, agent_id, text, created_at) VALUES (?,?,?,?)",
            (post_id, agent_id, text, time.time()))
        # FEEDBACK HOOK: if this agent was notified about this post, the
        # notification produced engagement — record it for the router.
        self._exec("UPDATE notifications SET engaged=1 WHERE agent_id=? AND post_id=?",
                   (agent_id, post_id))
        return dict(self._q("SELECT * FROM comments WHERE id=?", (cur.lastrowid,))[0])

    def comments_on(self, post_id):
        return [dict(r) for r in
                self._q("SELECT * FROM comments WHERE post_id=? ORDER BY id", (post_id,))]

    # ---- notifications & feedback ----
    def add_notification(self, agent_id, post_id, score, reasons):
        self._exec(
            "INSERT INTO notifications (agent_id, post_id, score, reasons, created_at)"
            " VALUES (?,?,?,?,?)",
            (agent_id, post_id, score, reasons, time.time()))

    def notifications_for(self, agent_id, since_id=0):
        return [dict(r) for r in self._q(
            "SELECT * FROM notifications WHERE agent_id=? AND id>? ORDER BY id",
            (agent_id, since_id))]

    def engagement_stats(self, agent_id, author_id):
        """How often has `agent_id` engaged when notified about posts by
        `author_id`? Returns (notified_count, engaged_count)."""
        rows = self._q(
            "SELECT COUNT(*) AS n, COALESCE(SUM(engaged),0) AS e "
            "FROM notifications nf JOIN posts p ON p.id = nf.post_id "
            "WHERE nf.agent_id=? AND p.agent_id=?", (agent_id, author_id))
        return rows[0]["n"], rows[0]["e"]
