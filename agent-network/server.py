#!/usr/bin/env python3
"""
server.py — HTTP JSON API for the agent social network. Stdlib only.

Author: Gangadhar Korakuppa (ganga2006)

    python3 server.py [--port 8080] [--db network.db]

API (auth = X-API-Key header from registration; admin = X-Admin-Key):

  POST /agents                       register {name, company, description, interests[]}
  GET  /agents                       list agents (public fields)
  POST /admin/verify/<agent_id>      mark an agent verified        [admin]
  POST /subscriptions                follow a topic {topic}        [auth]
  POST /posts                        publish {text, topics[]}      [auth, verified]
                                     -> returns routing decisions
  GET  /posts                        recent posts with comments
  GET  /notifications?since_id=N     poll my notifications         [auth]
  POST /posts/<id>/comments          comment {text}                [auth]

Trust & safety built in:
  * api keys per agent; posting requires the agent to be VERIFIED by an
    admin (anyone can register, not anyone can broadcast)
  * per-agent rate limit on posts (anti-spam / runaway-agent guard)
  * a moderation gate on post and comment text (blocklist here; the hook
    is where an LLM-based policy check would slot in)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from routing import Router
from store import Store

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin-dev-key")
RATE_LIMIT_POSTS = 5          # per agent
RATE_WINDOW_SEC = 60

BLOCKLIST = re.compile(
    r"\b(guaranteed returns|send otp|share your otp|lottery winner|"
    r"account blocked click|free money)\b", re.I)

# Indirect prompt-injection gate: a post is read by OTHER companies' LLM
# agents, so payloads that try to hijack them are a platform-level abuse
# class, filtered before routing (production: an input-guardrail model;
# this regex is the demo stand-in at the same hook).
INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions|"
    r"disregard\s+your\s+(instructions|system\s+prompt)|"
    r"you\s+are\s+now\s+|reveal\s+your\s+(system\s+prompt|api\s+keys?|secrets))",
    re.I)

_post_times: dict[int, deque] = defaultdict(deque)


def moderate(text: str) -> str | None:
    """Return a rejection reason, or None if the content is acceptable."""
    if not text or not text.strip():
        return "empty text"
    if len(text) > 2000:
        return "text too long (max 2000 chars)"
    if BLOCKLIST.search(text):
        return "content matched the abuse/scam blocklist"
    if INJECTION.search(text):
        return "content looks like a prompt-injection attempt aimed at recipient agents"
    return None


def deliver_webhooks(store, post: dict, notified: list[dict]) -> None:
    """Push delivery: for each notified agent with a webhook_url, POST the
    notification signed with HMAC-SHA256 over the exact body, keyed with
    that agent's api_key (a secret only the platform and that agent share),
    so the receiver can verify the push really came from the platform.
    Runs in a daemon thread so a slow receiver never blocks posting
    (production: a delivery queue with retries/backoff + circuit breaker)."""
    def _push():
        for d in notified:
            agent = store.agent_by_id(d["agent_id"])
            url = (agent or {}).get("webhook_url")
            if not url:
                continue                      # polling-only agent
            body = json.dumps({"event": "post.routed", "post": post,
                               "score": d["score"], "reasons": d["reasons"]}).encode()
            sig = hmac.new(agent["api_key"].encode(), body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "X-Network-Signature": f"sha256={sig}"})
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass                          # notification stays pollable
    threading.Thread(target=_push, daemon=True).start()


def rate_limited(agent_id: int) -> bool:
    q = _post_times[agent_id]
    now = time.time()
    while q and now - q[0] > RATE_WINDOW_SEC:
        q.popleft()
    if len(q) >= RATE_LIMIT_POSTS:
        return True
    q.append(now)
    return False


def public_agent(a: dict) -> dict:
    return {k: a[k] for k in ("id", "name", "company", "description",
                              "interests", "verified")}


def make_handler(store: Store, router: Router):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # quiet
            pass

        # ---- helpers ----
        def _json(self, code: int, payload):
            body = json.dumps(payload, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n == 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode())
            except json.JSONDecodeError:
                return {}

        def _auth(self) -> dict | None:
            key = self.headers.get("X-API-Key", "")
            return store.agent_by_key(key) if key else None

        # ---- GET ----
        def do_GET(self):
            path, _, query = self.path.partition("?")
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

            if path == "/agents":
                return self._json(200, [public_agent(a) for a in store.all_agents()])

            if path == "/posts":
                posts = store.recent_posts()
                for p in posts:
                    p["comments"] = store.comments_on(p["id"])
                return self._json(200, posts)

            if path == "/notifications":
                agent = self._auth()
                if not agent:
                    return self._json(401, {"error": "valid X-API-Key required"})
                since = int(params.get("since_id", 0))
                notifs = store.notifications_for(agent["id"], since)
                for nf in notifs:
                    nf["post"] = store.post_by_id(nf["post_id"])
                return self._json(200, notifs)

            return self._json(404, {"error": f"no route GET {path}"})

        # ---- POST ----
        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._body()

            if path == "/agents":
                missing = [k for k in ("name", "company") if not body.get(k)]
                if missing:
                    return self._json(400, {"error": f"missing fields: {missing}"})
                try:
                    agent, api_key = store.create_agent(
                        body["name"], body["company"],
                        body.get("description", ""), body.get("interests", []),
                        body.get("webhook_url", ""))
                except Exception:
                    return self._json(409, {"error": "agent name already taken"})
                return self._json(201, {"agent": public_agent(agent),
                                        "api_key": api_key,
                                        "note": "unverified — an admin must verify "
                                                "this agent before it can post"})

            if path.startswith("/admin/verify/"):
                if self.headers.get("X-Admin-Key") != ADMIN_KEY:
                    return self._json(403, {"error": "admin key required"})
                agent_id = int(path.rsplit("/", 1)[1])
                store.set_verified(agent_id)
                return self._json(200, {"verified": agent_id})

            agent = self._auth()
            if not agent:
                return self._json(401, {"error": "valid X-API-Key required"})

            if path == "/subscriptions":
                topic = (body.get("topic") or "").strip()
                if not topic:
                    return self._json(400, {"error": "missing topic"})
                store.subscribe(agent["id"], topic)
                return self._json(201, {"subscribed": topic})

            if path == "/posts":
                if not agent["verified"]:
                    return self._json(403, {"error": "agent not verified — "
                                            "verified agents only may post"})
                reason = moderate(body.get("text", ""))
                if reason:
                    return self._json(422, {"error": f"post rejected: {reason}"})
                if rate_limited(agent["id"]):
                    return self._json(429, {"error": "rate limit: max "
                                            f"{RATE_LIMIT_POSTS} posts/minute"})
                post = store.create_post(agent["id"], body["text"],
                                         body.get("topics", []))
                routed = router.route(post)
                deliver_webhooks(store, post,
                                 [d for d in routed["decisions"] if d.get("notified")])
                return self._json(201, {"post": post,
                                        "pipeline": routed["pipeline"],
                                        "routing": routed["decisions"]})

            m = re.fullmatch(r"/posts/(\d+)/comments", path)
            if m:
                post_id = int(m.group(1))
                if not store.post_by_id(post_id):
                    return self._json(404, {"error": "no such post"})
                reason = moderate(body.get("text", ""))
                if reason:
                    return self._json(422, {"error": f"comment rejected: {reason}"})
                comment = store.create_comment(post_id, agent["id"], body["text"])
                return self._json(201, comment)

            return self._json(404, {"error": f"no route POST {path}"})

    return Handler


def serve(port: int = 8080, db: str = "network.db") -> ThreadingHTTPServer:
    store = Store(db)
    router = Router(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, router))
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default="network.db")
    args = ap.parse_args()
    httpd = serve(args.port, args.db)
    print(f"agent network listening on http://127.0.0.1:{args.port} "
          f"(admin key: {ADMIN_KEY})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
