"""The order the site prints and the check that refuses any other.

    uv run --with pytest pytest tests          # from eval/

Since 2026-10-31 the site is a results report: one table per test, newest release first, no combined score and
no places (David: "just present our results and lay them out"). Tiers from drive resamples are still computed
(rankings/tiers.json) and any tier the site printed would be checked against them; it prints none.

The live page of 2026-10-31 (strict 1..9 order with medals, g55-public b713e34) is kept in
fixtures/strict_order_2026-10-31; the check must fail on it and pass on the site built from this tree.
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL))
from geval import ranking

STRICT = EVAL / "tests" / "fixtures" / "strict_order_2026-10-31"


@pytest.fixture(scope="module")
def tests():
    return ranking.site_tests(EVAL)


@pytest.fixture(scope="module")
def released():
    return ranking.release_dates(EVAL)


T = "jerk"   # a test with more than one tier in the published file, for the tier-claim cases


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    r = subprocess.run([sys.executable, str(EVAL / "site" / "build.py"), "--out", str(out)],
                       capture_output=True, text=True, cwd=EVAL, check=False)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "order check: 0 problems" in r.stdout
    return out


def _page(tmp_path, body: str) -> Path:
    (tmp_path / "index.html").write_text(f"<!doctype html><html><body>{body}</body></html>")
    return tmp_path


def _parsed(site: Path) -> list:
    out = []
    for f in sorted(site.rglob("*.html")):
        p = ranking._Page()
        p.feed(f.read_text())
        out.append((f, p))
    return out


def _newest_first(mids, released):
    return sorted(mids, key=lambda m: (released[m], m), reverse=True)


def test_tiers_split_only_what_the_resamples_separate():
    """Paired drives: a and b differ by far less than their noise, c by far more."""
    rng = np.random.default_rng(1)
    drives = [f"d{i:02d}" for i in range(12)]
    shared = {d: rng.normal(0, 1) for d in drives}
    vals = {m: {f"{d}:{k}": shared[d] + off + rng.normal(0, 0.3) for d in drives for k in range(3)}
            for m, off in (("a", 0.0), ("b", 0.02), ("c", 3.0))}
    point, boot, counts = ranking.resample({"x": vals}, {"x": ("n", "mean")}, {"n": lambda u: u.split(":")[0]})
    assert counts == {"n": (36, 12)}
    o = ranking.order(point["x"], boot["x"], "lower")
    assert [set(t) for t in o["tiers"]] == [{"a", "b"}, {"c"}]
    assert o["beats"]["a"]["c"] == 1.0 and o["beats"]["c"]["a"] == 0.0
    assert 0.05 < o["beats"]["a"]["b"] < 0.95
    assert o["models"]["c"]["rank_ci"] == [3, 3]


def test_resampling_is_paired_and_reproducible():
    """Same seed, same draws; a model never beats an identical copy of itself."""
    vals = {"a": {f"d{i}:0": float(i) for i in range(10)}}
    vals["b"] = dict(vals["a"])
    args = ({"x": vals}, {"x": ("n", "median")}, {"n": lambda u: u.split(":")[0]})
    p1, b1, _ = ranking.resample(*args)
    _, b2, _ = ranking.resample(*args)
    assert np.array_equal(b1["x"]["a"], b2["x"]["a"])
    o = ranking.order(p1["x"], b1["x"], "lower")
    assert o["beats"]["a"]["b"] == 0.5 and len(o["tiers"]) == 1


def test_no_combined_score_in_the_tiers_file(tests):
    """The composite G55 score is retired: the tiers file and the ranking module carry no trace of it."""
    assert "composite" not in tests and set(tests) <= set(ranking.TESTS) | {"public_cl2s"}
    assert not hasattr(ranking, "g55_composite") and not hasattr(ranking, "COMPOSITE_WEIGHTS")


def test_built_site_passes_newest_first(built, tests, released):
    assert ranking.check_site(built, tests, released) == []
    pages = dict((f.name, p) for f, p in _parsed(built) if f.parent == built)
    lists = pages["index.html"].lists
    assert len(lists) >= 5, "one release-ordered list per results table"
    for kind, mids in lists:
        assert kind == "release" and mids == _newest_first(mids, released), mids
    index = "\n".join(pages["index.html"].text)      # what a reader sees; the log (data-history) is history
    assert not ranking.TIER_TEXT.search(index) and "#1" not in index
    assert "G55 score" not in index and "/100" not in index
    for f, p in _parsed(built):
        assert not p.claims, f"{f.name} prints a tier"
        assert len(set(p.free)) <= 1, f"{f.name} lists models outside a release-ordered list"


def test_live_strict_order_page_fails(tmp_path, tests):
    for name in ("index.html", "site.js"):
        (tmp_path / name).write_bytes(gzip.decompress((STRICT / f"{name}.gz").read_bytes()))
    problems = ranking.check_site(tmp_path, tests)
    kinds = {p.split(": ", 1)[0] + ": " + p.split(": ", 1)[1].split(" '")[0] for p in problems}
    assert "index.html: medal" in kinds and "site.js: medal" in kinds, kinds
    assert any(p.startswith("index.html: place number") for p in problems)
    assert any(p.startswith("index.html: rank or place") for p in problems)
    assert any(p.startswith("index.html: combined score") for p in problems)


def test_strict_order_as_tier_claims_fails_on_separation(tmp_path, tests, released):
    """One tier per model in point order, no medal or place text: only the resamples can refuse it."""
    order = [m for tier in tests[T]["tiers"] for m in tier]
    rows = "".join(f'<li data-model="{m}" data-tier-{T}="{k}">{k}</li>'
                   for k, m in sorted(enumerate(order, 1), key=lambda km: (released[km[1]], km[1]), reverse=True))
    problems = ranking.check_site(_page(tmp_path, f'<ol data-order="release">{rows}</ol>'), tests, released)
    assert problems and all("beat it in only" in p for p in problems), problems
    assert len(problems) >= len(order) - len(tests[T]["tiers"])


def test_published_tiers_as_claims_pass(tmp_path, tests, released):
    tier = {m: k for k, ms in enumerate(tests[T]["tiers"], 1) for m in ms}
    rows = "".join(f'<li data-model="{m}" data-tier-{T}="{tier[m]}">{tier[m]}</li>' for m in _newest_first(tier, released))
    assert ranking.check_site(_page(tmp_path, f'<ol data-order="release">{rows}</ol>'), tests, released) == []


def test_lists_must_be_newest_release_first(tmp_path, tests, released):
    mids = _newest_first(list(tests[T]["models"]), released)
    ok = "".join(f'<tr data-model="{m}"><td><span data-model="{m}">x</span></td></tr>' for m in mids)
    assert ranking.check_site(_page(tmp_path, f'<table><tbody data-order="release">{ok}</tbody></table>'), tests, released) == []
    by_value = sorted(mids, key=lambda m: tests[T]["models"][m]["value"])
    assert by_value != mids
    cases = {
        "".join(f'<tbody data-order="release"><tr data-model="{m}"></tr></tbody>' for m in by_value[:1])
        + '<tbody data-order="release">' + "".join(f'<tr data-model="{m}"></tr>' for m in by_value) + "</tbody>": "newest release first",
        '<tbody data-order="value">' + "".join(f'<tr data-model="{m}"></tr>' for m in mids) + "</tbody>": "the only order",
        '<tbody data-order="release">' + "".join(f'<tr data-model="{m}"></tr>' for m in mids + mids[:1]) + "</tbody>": "listed twice",
        "".join(f'<p data-model="{m}">x</p>' for m in mids): "outside a release-ordered list",
        '<tbody data-order="release"><tr data-model="no_such_model"></tr><tr data-model="' + mids[0] + '"></tr></tbody>': "no release date",
        "<p>G55 score 91.7</p>": "combined score",
        "<p>91.7/100</p>": "combined score",
    }
    for body, want in cases.items():
        problems = ranking.check_site(_page(tmp_path, body), tests, released)
        assert any(want in p for p in problems), (want, problems)
    # one model's own page names it outside a list; that is not a list
    assert ranking.check_site(_page(tmp_path, f'<h1 data-model="{mids[0]}">x</h1>'), tests, released) == []


def test_tier_text_and_claims_must_be_verifiable(tmp_path, tests):
    m = next(iter(tests[T]["models"]))
    t = tests[T]["models"][m]["tier"]
    cases = {
        "<p>Tier 1: every model</p>": "outside a verified claim",
        f'<p data-model="{m}" data-tier-{T}="{t + 1}">x</p><p data-model="{m}" data-tier-{T}="{t}">y</p>': "printed in tier",
        '<p data-model="no_such_model" data-tier-jerk="1">x</p>': "has no resamples",
        f'<p data-model="{m}" data-tier-nosuchtest="1">x</p>': "no resamples for this test",
        '<p data-tier-jerk="1">x</p>': "no data-model",
        '<p title="ranked #2 on the G55">x</p>': "place number",
        '<meta name="description" content="Ranked 1st of 9">': "rank or place",
        "<p>It leads the field.</p>": "leader wording",
        "<p>Best: <b>N16</b> (41.6 cm)</p>": "best or worst label",
    }
    for body, want in cases.items():
        problems = ranking.check_site(_page(tmp_path, body), tests)
        assert any(want in p for p in problems), (body, problems)


def test_no_pages_is_a_failure(tmp_path, tests):
    assert ranking.check_site(tmp_path / "missing", tests) and ranking.check_site(tmp_path, tests)


def test_history_is_exempt_but_only_inside_it(tmp_path, tests):
    old = "models are ranked 1..N with medals 🥇 for the top three"
    assert ranking.check_site(_page(tmp_path, f"<ol data-history><li>{old}</li></ol>"), tests) == []
    assert ranking.check_site(_page(tmp_path, f"<ol data-history><li>x</li></ol><p>{old}</p>"), tests)
