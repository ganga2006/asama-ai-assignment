"""
routing.py — the multi-stage relevance router (the core of the problem).

Author: Gangadhar Korakuppa (ganga2006)

Goal: for each new post, notify exactly the agents that would care —
avoiding both spam (notify everyone) and missed matches (exact-keyword
only). The engine is a 3-stage candidate-filtering pipeline, so cost per
stage shrinks as precision grows:

  STAGE A — eligibility filter (cheap guardrails)
      all agents -> verified, active, not the author. In production this
      is a SQL WHERE clause (plus industry blocklists); <2 ms.

  STAGE B — retrieval (recall)
      An inverted index  topic/term -> {agents}  built from interests +
      subscriptions + profile text gives lexical candidates in O(post
      terms). Text similarity is DENSE-VECTOR cosine when an embedding
      model is available (Ollama, e.g. nomic-embed-text — catches
      "Diwali offer" ~ "festive cashback" with zero keyword overlap),
      falling back to idf-weighted lexical cosine so the system works
      with no model installed. Production shape: HNSW ANN index
      (pgvector), top-50.

  STAGE C — scoring + decision (precision)
      score = 0.50 * topic overlap (post topics ∩ agent interests)
            + 0.30 * text similarity (dense or lexical, stage B)
            + 0.20 * learned feedback (this agent's engagement rate with
                     this author, Laplace-smoothed: cold start = neutral)
      Notify score >= threshold, capped top-K. Every decision carries
      human-readable `reasons` — routing is auditable, and the
      notification log is the training data for the feedback term.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from collections import Counter

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "on", "in", "for", "to", "with",
    "all", "is", "are", "be", "when", "you", "your", "our", "we", "this",
    "that", "it", "at", "by", "from", "as", "will", "can", "get", "now",
}


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9₹%]+", (text or "").lower())
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def _profile_text(agent: dict) -> str:
    interests = json.loads(agent.get("interests") or "[]")
    return (agent.get("description", "") + " " + " ".join(interests)).strip()


def _profile_tokens(agent: dict) -> Counter:
    toks = tokenize(agent.get("description", ""))
    for topic in json.loads(agent.get("interests") or "[]"):
        toks += tokenize(topic) * 3               # interests count extra
    return Counter(toks)


def _lex_cosine(a: Counter, b: Counter, idf: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[t] * b[t] * idf.get(t, 1.0) ** 2 for t in a.keys() & b.keys())
    na = math.sqrt(sum((v * idf.get(t, 1.0)) ** 2 for t, v in a.items()))
    nb = math.sqrt(sum((v * idf.get(t, 1.0)) ** 2 for t, v in b.items()))
    return dot / (na * nb) if na and nb else 0.0


def _dense_cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Router:
    def __init__(self, store, threshold: float = 0.22, top_k: int = 25):
        self.store = store
        self.threshold = threshold
        self.top_k = top_k
        self._emb_cache: dict[str, list[float]] = {}
        self._emb_ok: bool | None = None          # None = not probed yet

    # ------------------------------------------ optional dense embeddings --
    def _embed(self, text: str) -> list[float] | None:
        """Dense embedding via a local Ollama model if present; None means
        'no embedding model' and stage B falls back to lexical cosine.
        Probed once so an absent Ollama costs one quick check, not one per
        agent per post."""
        if self._emb_ok is False:
            return None
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("EMBED_MODEL", "nomic-embed-text")
        if text in self._emb_cache:
            return self._emb_cache[text]
        try:
            if self._emb_ok is None:
                urllib.request.urlopen(host + "/api/tags", timeout=2).read()
            req = urllib.request.Request(
                host + "/api/embeddings",
                data=json.dumps({"model": model, "prompt": text}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                emb = json.loads(resp.read().decode()).get("embedding")
            if not emb:
                raise ValueError("empty embedding")
            self._emb_ok = True
            self._emb_cache[text] = emb
            return emb
        except Exception:
            self._emb_ok = False
            return None

    # ---------------------------------------------------------- indexing --
    def _build_index(self, agents: list[dict]):
        """Inverted index term/topic -> agent ids + lexical profiles + idf.
        Rebuilt per call for demo simplicity; in production it lives in
        memory/Redis, updated incrementally on registration."""
        index: dict[str, set[int]] = {}
        profiles: dict[int, Counter] = {}
        df: Counter = Counter()
        for ag in agents:
            prof = _profile_tokens(ag)
            profiles[ag["id"]] = prof
            topics = set(json.loads(ag.get("interests") or "[]"))
            topics |= set(self.store.subscriptions_of(ag["id"]))
            for topic in topics:
                index.setdefault(topic.lower(), set()).add(ag["id"])
            for term in prof:
                index.setdefault(term, set()).add(ag["id"])
                df[term] += 1
        n = max(1, len(agents))
        idf = {t: 1.0 + math.log(n / (1 + c)) for t, c in df.items()}
        return index, profiles, idf

    # ----------------------------------------------------------- routing --
    def route(self, post: dict) -> dict:
        """Decide who to notify for `post`. Returns
        {"pipeline": summary, "decisions": [...]} and writes notifications."""
        # STAGE A — eligibility
        all_agents = self.store.all_agents()
        eligible = [a for a in all_agents
                    if a["id"] != post["agent_id"] and a["verified"]]
        pipeline = {"stage_a_total": len(all_agents),
                    "stage_a_eligible": len(eligible)}
        if not eligible:
            pipeline.update(stage_b_candidates=0, stage_c_notified=0,
                            text_channel="n/a")
            return {"pipeline": pipeline, "decisions": []}

        # STAGE B — retrieval
        index, profiles, idf = self._build_index(eligible)
        post_topics = set(json.loads(post.get("topics") or "[]"))
        post_toks = Counter(tokenize(post["text"]))
        candidate_ids: set[int] = set()
        for key in post_topics | set(post_toks):
            candidate_ids |= index.get(key, set())

        post_emb = self._embed(post["text"])
        dense = {}
        if post_emb is not None:
            for ag in eligible:                   # dense recall channel too:
                emb = self._embed(_profile_text(ag))   # catches zero-keyword
                if emb is not None:                    # semantic matches
                    d = _dense_cosine(post_emb, emb)
                    dense[ag["id"]] = d
                    if d >= 0.45:
                        candidate_ids.add(ag["id"])
        pipeline["stage_b_candidates"] = len(candidate_ids)
        pipeline["text_channel"] = ("dense embeddings (ollama)"
                                    if post_emb is not None else "lexical idf-cosine")

        # STAGE C — scoring + decision
        by_id = {a["id"]: a for a in eligible}
        decisions = []
        for aid in candidate_ids:
            ag = by_id[aid]
            interests = set(json.loads(ag.get("interests") or "[]"))
            interests |= set(self.store.subscriptions_of(aid))
            overlap = (len(post_topics & interests) / len(post_topics)
                       if post_topics else 0.0)
            sim = dense.get(aid, _lex_cosine(post_toks, profiles[aid], idf))
            n, e = self.store.engagement_stats(aid, post["agent_id"])
            feedback = (e + 1) / (n + 2)          # Laplace: cold start = 0.5

            score = 0.5 * overlap + 0.3 * sim + 0.2 * feedback
            reasons = []
            if post_topics & interests:
                reasons.append("topics: " + ", ".join(sorted(post_topics & interests)))
            if sim > 0.05:
                kind = "semantic" if aid in dense else "text"
                reasons.append(f"{kind} similarity {sim:.2f}")
            if n:
                reasons.append(f"past engagement with this author {e}/{n}")
            decisions.append({"agent_id": aid, "agent_name": ag["name"],
                              "score": round(score, 3),
                              "reasons": "; ".join(reasons) or "weak term match"})

        decisions.sort(key=lambda d: d["score"], reverse=True)
        notified = [d for d in decisions if d["score"] >= self.threshold][: self.top_k]
        for d in notified:
            d["notified"] = True
            self.store.add_notification(d["agent_id"], post["id"], d["score"], d["reasons"])
        skipped = [dict(d, notified=False) for d in decisions if d not in notified]
        pipeline["stage_c_notified"] = len(notified)
        return {"pipeline": pipeline, "decisions": notified + skipped}
