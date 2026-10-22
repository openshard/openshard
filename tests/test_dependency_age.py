"""New dependencies' publish dates from a (stubbed) public registry; a failed lookup stays unknown."""

from __future__ import annotations

from datetime import UTC, datetime

from openshard.verification import dependency_age as da

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def _pypi(*stamps: str) -> dict:
    return {"releases": {f"1.{i}": [{"upload_time_iso_8601": s}] for i, s in enumerate(stamps)}}


def test_an_old_package_with_old_releases_is_not_recent():
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        return _pypi("2019-01-01T00:00:00Z", "2026-06-01T00:00:00.123Z")

    (row,) = da.with_ages([{"name": "requests", "ecosystem": "pypi", "manifest": "pyproject.toml"}], fetch=fetch,
                          now=NOW)
    assert calls == ["https://pypi.org/pypi/requests/json"]
    assert row["recent"] is False
    assert (row["first_published"], row["latest_published"]) == ("2019-01-01T00:00:00Z", "2026-06-01T00:00:00Z")


def test_an_old_package_with_a_release_from_last_week_is_recent():
    (row,) = da.with_ages([{"name": "tanstack-x", "ecosystem": "pypi", "manifest": "m"}],
                          fetch=lambda _u: _pypi("2020-01-01T00:00:00Z", "2026-10-18T00:00:00Z"), now=NOW)
    assert row["recent"] is True


def test_a_brand_new_npm_package_is_recent_and_scoped_names_keep_their_at():
    urls: list[str] = []

    def fetch(url: str):
        urls.append(url)
        return {"time": {"created": "2026-10-20T00:00:00Z", "modified": "2026-10-21T00:00:00Z",
                         "0.0.1": "2026-10-20T00:00:00Z"}}

    (row,) = da.with_ages([{"name": "@scope/new-pkg", "ecosystem": "npm", "manifest": "package.json"}], fetch=fetch,
                          now=NOW)
    assert urls == ["https://registry.npmjs.org/@scope%2Fnew-pkg"]
    assert row["recent"] is True and row["first_published"] == "2026-10-20T00:00:00Z"


def test_a_failed_or_odd_lookup_is_unknown_never_not_recent():
    rows = da.with_ages([{"name": "a", "ecosystem": "pypi", "manifest": "m"},
                         {"name": "b", "ecosystem": "npm", "manifest": "m"},
                         {"name": "c", "ecosystem": "pypi", "manifest": "m"}],
                        fetch=lambda u: None if "/a/" in u else {"time": "garbage"} if "npm" in u else {"releases": {}},
                        now=NOW)
    assert [r["recent"] for r in rows] == [None, None, None]
    assert all(r["first_published"] is None for r in rows)


def test_an_unsafe_name_is_never_sent():
    calls: list[str] = []
    (row,) = da.with_ages([{"name": "../../etc", "ecosystem": "pypi", "manifest": "m"}],
                          fetch=lambda u: calls.append(u), now=NOW)
    assert calls == [] and row["recent"] is None


def test_lookups_are_bounded():
    calls: list[str] = []
    packages = [{"name": f"p{i}", "ecosystem": "pypi", "manifest": "m"} for i in range(da.MAX_LOOKUPS + 3)]
    rows = da.with_ages(packages, fetch=lambda u: calls.append(u), now=NOW)
    assert len(calls) == da.MAX_LOOKUPS and len(rows) == da.MAX_LOOKUPS + 3


def test_the_finding_counts_recent_and_unknown_packages(monkeypatch):
    from openshard.verification import pr_findings as pf

    files = [pf.ChangedFile(path="requirements.txt", status="M", added=2, removed=0)]
    texts = {("base", "requirements.txt"): "click\n", ("head", "requirements.txt"): "click\nnewpkg\nmystery\n"}
    monkeypatch.setattr(da, "fetch_json",
                        lambda url, timeout=5.0: _pypi("2026-10-21T00:00:00Z") if "newpkg" in url else None)
    finding = pf.added_dependencies(files, lambda rev, path: texts.get((rev, path)), "base", "head",
                                    registry_lookup=True)
    assert finding is not None
    assert finding.extra["recent_dependencies"] == 1 and finding.extra["unknown_age"] == 1
    assert finding.extra["recent_days"] == 14
    plain = pf.added_dependencies(files, lambda rev, path: texts.get((rev, path)), "base", "head")
    assert plain is not None and "recent_dependencies" not in plain.extra


def test_all_lookups_together_stop_at_the_total_limit():
    """Each lookup 'takes' 6 s on a fake clock: within 15 s only the first three are asked; the rest stay unknown."""
    now = [0.0]
    calls: list[str] = []

    def slow(url: str):
        calls.append(url)
        now[0] += 6.0
        return _pypi("2019-01-01T00:00:00Z")

    packages = [{"name": f"p{i}", "ecosystem": "pypi", "manifest": "m"} for i in range(6)]
    rows = da.with_ages(packages, fetch=slow, now=NOW, clock=lambda: now[0])
    assert len(calls) == 3 and da.TOTAL_SECONDS == 15.0
    assert [r["recent"] for r in rows] == [False, False, False, None, None, None]


def test_the_default_fetch_never_waits_past_the_total_limit(monkeypatch):
    timeouts: list[float] = []
    monkeypatch.setattr(da, "fetch_json", lambda url, timeout=5.0: timeouts.append(timeout))
    now = [0.0]
    da.with_ages([{"name": "a", "ecosystem": "pypi", "manifest": "m"}], now=NOW, clock=lambda: now[0] + 12.0 * len(timeouts))
    assert timeouts and timeouts[0] <= 5.0


def test_lookups_are_off_unless_asked(monkeypatch):
    from openshard.verification import pr_findings as pf

    monkeypatch.setattr(da, "fetch_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    files = [pf.ChangedFile(path="requirements.txt", status="A", added=1, removed=0)]
    finding = pf.added_dependencies(files, lambda rev, path: "click\n" if rev == "head" else None, "base", "head")
    assert finding is not None and "recent_dependencies" not in finding.extra
