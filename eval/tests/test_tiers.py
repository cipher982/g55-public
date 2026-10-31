"""The order the site prints: tiers from drive resamples, and the check that refuses any other order.

    uv run --with pytest pytest tests          # from eval/

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


def _claims(site: Path) -> list:
    out = []
    for f in sorted(site.rglob("*.html")):
        p = ranking._Page()
        p.feed(f.read_text())
        out += p.claims
    return out


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


def test_published_tiers_match_the_site_scores(tests):
    """The resampled composite's point values are the site's G55 scores (the site rounds its inputs)."""
    docs = [json.loads(p.read_text()) for p in sorted((EVAL / "results").glob("*.json"))]
    comp = ranking.g55_composite(docs, ranking.truck_scores(EVAL))
    tc = tests["composite"]["models"]
    assert set(tc) == {m for m, s in comp.items() if s["score"] is not None}
    for m, s in tc.items():
        assert abs(s["value"] - comp[m]["score_raw"]) < 0.15, m


def test_built_site_passes_and_prints_tiers(built, tests):
    assert ranking.check_site(built, tests) == []
    claims = _claims(built)
    printed = {(t, m) for t, m, _ in claims}
    for test in ("composite", "openloop", "public_cl2s"):
        assert {(test, m) for m in tests[test]["models"]} <= printed, test
    index = (built / "index.html").read_text()
    assert ranking.TIER_TEXT.search(index) and "#1" not in index


def test_live_strict_order_page_fails(tmp_path, tests):
    for name in ("index.html", "site.js"):
        (tmp_path / name).write_bytes(gzip.decompress((STRICT / f"{name}.gz").read_bytes()))
    problems = ranking.check_site(tmp_path, tests)
    kinds = {p.split(": ", 1)[0] + ": " + p.split(": ", 1)[1].split(" '")[0] for p in problems}
    assert "index.html: medal" in kinds and "site.js: medal" in kinds, kinds
    assert any(p.startswith("index.html: place number") for p in problems)
    assert any(p.startswith("index.html: rank or place") for p in problems)


def test_strict_order_as_tier_claims_fails_on_separation(tmp_path, tests):
    """Nine tiers in point order, no medal or place text: only the resamples can refuse it."""
    order = [m for tier in tests["composite"]["tiers"] for m in tier]
    body = "".join(f'<span data-model="{m}" data-tier-composite="{k}">{k}</span>' for k, m in enumerate(order, 1))
    problems = ranking.check_site(_page(tmp_path, body), tests)
    assert problems and all("beat it in only" in p for p in problems)
    assert len(problems) >= len(order) - len(tests["composite"]["tiers"])


def test_published_tiers_as_claims_pass(tmp_path, tests):
    body = "".join(f'<span data-model="{m}" data-tier-composite="{k}">{k}</span>'
                   for k, tier in enumerate(tests["composite"]["tiers"], 1) for m in tier)
    assert ranking.check_site(_page(tmp_path, body), tests) == []


def test_tier_text_and_claims_must_be_verifiable(tmp_path, tests):
    m = next(iter(tests["composite"]["models"]))
    t = tests["composite"]["models"][m]["tier"]
    cases = {
        "<p>Tier 1: every model</p>": "outside a verified claim",
        f'<p data-model="{m}" data-tier-composite="{t + 1}">x</p><p data-model="{m}" data-tier-composite="{t}">y</p>': "printed in tier",
        '<p data-model="no_such_model" data-tier-composite="1">x</p>': "has no resamples",
        f'<p data-model="{m}" data-tier-nosuchtest="1">x</p>': "no resamples for this test",
        '<p data-tier-composite="1">x</p>': "no data-model",
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
