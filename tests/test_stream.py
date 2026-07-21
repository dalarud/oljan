"""Streaming engine: ingest clusters items; flush emits after gather window."""
import time
from datetime import datetime, timezone
from oiltrader.collectors.base import NewsItem
from oiltrader.stream import NewsStreamEngine


class FakeCfg:
    def __init__(self, d): self.d = d
    def get(self, k, dflt=None): return self.d.get(k, dflt)


def _engine(on_story):
    cfg = FakeCfg({"news.cluster_similarity": 0.3, "news.stream_gather_seconds": 30,
                   "news.stream_priority_gather_seconds": 5,
                   "news.max_age_minutes": 100000})
    seen = set()
    return NewsStreamEngine(
        cfg, collectors=[], on_story=on_story,
        seen=lambda h: h in seen, mark_seen=lambda h, s: seen.add(h),
        source_weight=lambda s: 1.0 if s == "reuters" else 0.3,
        primary_symbol="BZ=F")


def test_ingest_clusters_same_story():
    got = []
    eng = _engine(got.append)
    now = datetime.now(timezone.utc)
    eng._ingest(NewsItem("reuters", "OPEC output cut 1 mb/d", "", "u1", now))
    eng._ingest(NewsItem("x/@a", "OPEC AGREES OUTPUT CUT of 1 mb/d", "", "u2", now))
    eng._ingest(NewsItem("bbc", "Earthquake hits Japan coast", "", "u3", now))
    # two stories pending: OPEC (2 sources) + earthquake (1)
    assert len(eng.pending) == 2
    opec = max(eng.pending, key=lambda e: e[0].n_sources)[0]
    assert opec.n_sources == 2


def test_flush_emits_after_deadline():
    got = []
    eng = _engine(got.append)
    now = datetime.now(timezone.utc)
    eng._ingest(NewsItem("reuters", "OPEC output cut", "", "u1", now))
    eng._flush()                      # deadline not reached yet
    assert got == []
    eng.pending[0][1] = time.monotonic() - 1   # force due
    eng._flush()
    assert len(got) == 1 and not eng.pending


def test_priority_source_shorter_window():
    eng = _engine(lambda s: None)
    now = datetime.now(timezone.utc)
    eng._ingest(NewsItem("reuters", "OPEC output cut", "", "u1", now))  # weight 1.0
    remaining = eng.pending[0][1] - time.monotonic()
    assert remaining <= eng.fast_gather + 1   # used the fast window
