from datetime import datetime, timezone, timedelta
from oiltrader.collectors.base import NewsItem
from oiltrader.clustering import cluster_items, tokenize


def _item(src, title, mins_ago=0):
    return NewsItem(source=src, title=title, content="", url="http://x",
                    ts=datetime.now(timezone.utc) - timedelta(minutes=mins_ago))


def _sw(s):
    return {"reuters": 1.0, "oilprice.com": 0.7}.get(s, 0.3)


def test_same_story_clusters_across_sources():
    items = [
        _item("reuters", "OPEC+ agrees surprise output cut of 1 million bpd", 2),
        _item("x/@DeItaone", "OPEC+ AGREES OUTPUT CUT 1 MILLION BPD", 1),
        _item("oilprice.com", "OPEC+ Surprises Market With Output Cut", 5),
        _item("x/@spectatorindex", "Earthquake reported off coast of Japan", 3),
    ]
    stories = cluster_items(items, _sw, sim=0.4)
    # The three OPEC items should merge; the earthquake stays separate.
    sizes = sorted(len(s.items) for s in stories)
    assert sizes == [1, 3]
    opec = max(stories, key=lambda s: len(s.items))
    assert opec.n_sources == 3
    # earliest source is oilprice (5 min ago)? No — earliest ts = 5 min ago.
    assert opec.first_item.source == "oilprice.com"
    # representative = most credible (reuters)
    assert opec.representative(_sw).source == "reuters"


def test_tokenize_extracts_entities():
    toks, ents = tokenize("OPEC and Iran clash over Hormuz")
    assert "opec" in ents and "iran" in ents and "hormuz" in ents
