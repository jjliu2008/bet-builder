"""Offline tests for the snapshot logger: format, size guard, failure handling, warm start, replay."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.ingest.poller import Poller, Settings, State
from backend.logger import (DERIBIT_FIELDS, LogSettings, SnapshotLogger, deribit_rows, in_window,
                            select_books, tail_records, used_by_surface)
from backend.tests.replay import Replay
from engine import build_surface, expiry_key, fair_above
from engine.stale import load_snapshots, record_to_book, replay
from engine.tests.conftest import mk

INTERVAL = 30.0
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
BOOK_KEYS = {"type", "ts", "venue", "contract_id", "asset", "expiry", "kind", "strike", "lo", "hi",
             "yes_bids", "yes_asks", "settlement_index", "link"}


def deribit_down(request):
    return httpx.Response(500) if request.url.host == "www.deribit.com" else None


async def _instant(_seconds):
    await asyncio.sleep(0)


class Env:
    """A poller on the recording that logs into a temp directory, with a clock the test can move."""

    def __init__(self, tmp_path, background=False, **log):
        self.replay = Replay()
        self.t = self.replay.now
        self.dir = str(tmp_path / "snapshots")
        self.state = State(now=lambda: self.t)
        self.logger = SnapshotLogger(LogSettings(dir=self.dir, **log), background=background)
        settings = Settings(gemini_rps=1e6, kalshi_rps=1e6, deribit_rps=1e6, poll_interval_s=INTERVAL)
        self.poller = Poller(self.state, settings, transport=self.replay.transport, sleep=_instant,
                             logger=self.logger)

    def poll(self, advance_s=0.0):
        self.t += timedelta(seconds=advance_s)
        asyncio.run(self.poller.poll_once())
        self.logger.flush()
        return self.state

    @property
    def path(self):
        return os.path.join(self.dir, f"{self.t:%Y-%m-%d}.jsonl")

    @property
    def deribit_path(self):
        return os.path.join(self.dir, f"deribit-{self.t:%Y-%m-%d}.jsonl")

    def records(self):
        return load_snapshots(self.path)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, every_n_polls=1)


def by_type(records):
    out = {}
    for r in records:
        out.setdefault(r["type"], []).append(r)
    return out


# ---- format ------------------------------------------------------------------------------
def test_one_poll_writes_contract_format_records(env):
    st = env.poll()
    recs = env.records()
    t = by_type(recs)
    assert set(t) == {"book", "spot", "fair"}
    assert len({r["ts"] for r in recs}) == 1                         # one poll, one timestamp
    assert recs[0]["ts"] == "2026-11-17T05:12:07.000Z"               # ISO 8601 UTC with milliseconds
    for r in t["book"]:
        assert set(r) == BOOK_KEYS
        assert r["venue"] in ("gemini", "kalshi") and r["kind"] in ("threshold", "bucket")
        assert r["settlement_index"] and r["link"].startswith("https://")
        assert all(0 < p < 1 and s > 0 for p, s in r["yes_bids"] + r["yes_asks"])
    assert len({r["contract_id"] for r in t["book"]}) == len(t["book"])          # one record per contract
    assert t["spot"] == [{"type": "spot", "ts": recs[0]["ts"], "asset": "BTC", "price": st.spot}]
    assert all(set(r) == {"type", "ts", "asset", "expiry", "strike", "p_above"} for r in t["fair"])


def test_book_records_round_trip_to_the_same_books(env):
    st = env.poll()
    live = {**st.books["gemini"], **st.books["kalshi"]}
    for r in by_type(env.records())["book"]:
        b, want = record_to_book(r), live[r["contract_id"]]
        assert (b.venue, b.kind, b.strike, b.lo, b.hi) == (want.venue, want.kind, want.strike, want.lo, want.hi)
        assert expiry_key(b.expiry) == expiry_key(want.expiry)
        assert [(l.price, l.size) for l in b.yes_bids] == [(l.price, l.size) for l in want.yes_bids]
        assert [(l.price, l.size) for l in b.yes_asks] == [(l.price, l.size) for l in want.yes_asks]
        assert (b.settlement_index, b.link) == (want.settlement_index, want.link)


def test_only_gemini_and_in_window_kalshi_books_are_logged(env):
    st = env.poll()
    logged = {r["contract_id"] for r in by_type(env.records())["book"]}
    window = (st.spot * 0.97, st.spot * 1.03)
    assert set(st.books["gemini"]) <= logged                         # every Gemini book
    kal_in = {cid for cid, b in st.books["kalshi"].items() if in_window(b, window)}
    assert logged - set(st.books["gemini"]) == kal_in and kal_in
    assert len(kal_in) < len(st.books["kalshi"])                     # the rest is left out
    kinds = {st.books["kalshi"][cid].kind for cid in kal_in}
    assert kinds == {"threshold", "bucket"}


def test_fair_records_match_the_surface_and_skip_near_expiry(env):
    st = env.poll()
    fair = by_type(env.records())["fair"]
    expiries = {r["expiry"] for r in fair}
    first, second, third = st.expiries
    assert expiries == {second, third}                               # `first` is under 2 hours away: ladder shows null
    ks = sorted(r["strike"] for r in fair if r["expiry"] == second)
    want = fair_above("BTC", second, ks, surface=st.surface)
    for r in fair:
        if r["expiry"] == second:
            assert r["p_above"] == pytest.approx(want[r["strike"]], abs=1e-6) and 0 <= r["p_above"] <= 1
    logged_strikes = {r["strike"] for r in by_type(env.records())["book"]
                      if r["kind"] == "threshold" and r["expiry"] == second}
    assert set(ks) == logged_strikes                                 # one fair value per logged strike


def test_each_poll_gets_its_own_timestamp(env):
    env.poll()
    env.poll(advance_s=INTERVAL)
    stamps = sorted({r["ts"] for r in env.records()})
    assert stamps == ["2026-11-17T05:12:07.000Z", "2026-11-17T05:12:37.000Z"]
    per_ts = {s: sum(r["ts"] == s for r in env.records()) for s in stamps}
    assert per_ts[stamps[0]] == per_ts[stamps[1]] > 50


def test_no_spot_falls_back_to_paired_kalshi_thresholds(tmp_path):
    env = Env(tmp_path, every_n_polls=1)
    env.replay.fail = deribit_down
    st = env.poll()
    t = by_type(env.records())
    assert set(t) == {"book"}                                        # no spot and no surface: no spot or fair records
    paired = {p.kalshi for e in st.expiries for p in st.match.expiries[e].pairs}
    assert {r["contract_id"] for r in t["book"] if r["venue"] == "kalshi"} == paired
    assert not os.path.exists(env.deribit_path)


# ---- Deribit rows ------------------------------------------------------------------------
def test_deribit_rows_rebuild_the_same_surface(env):
    st = env.poll()
    lines = [json.loads(l) for l in open(env.deribit_path, encoding="utf-8")]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["type"] == "deribit" and rec["asset"] == "BTC" and rec["fields"] == list(DERIBIT_FIELDS)
    assert rec["ts"] == env.records()[0]["ts"] and rec["rows_total"] == len(st.deribit_rows)
    assert 0 < len(rec["rows"]) < rec["rows_total"]                  # only the rows the surface uses
    rebuilt = build_surface(deribit_rows(rec), "BTC", env.t)
    ks = [64000, 64500, 65000, 65500]
    for expiry in st.expiries[1:]:
        assert fair_above("BTC", expiry, ks, surface=rebuilt) == pytest.approx(
            fair_above("BTC", expiry, ks, surface=st.surface), abs=1e-12)
    assert len(rebuilt.slices) == len(st.surface.slices)


def test_used_by_surface_keeps_what_it_cannot_judge():
    row = lambda name, iv=50.0, f=65000.0: {"instrument_name": name, "mark_iv": iv, "underlying_price": f}
    assert used_by_surface(row("BTC-20NOV26-66000-C")) and used_by_surface(row("BTC-20NOV26-64000-P"))
    assert not used_by_surface(row("BTC-20NOV26-64000-C")) and not used_by_surface(row("BTC-20NOV26-66000-P"))
    assert used_by_surface(row("BTC-20NOV26-65000-C")) and not used_by_surface(row("BTC-20NOV26-65000-P"))
    assert not used_by_surface(row("BTC-20NOV26-66000-C", iv=0)) and not used_by_surface(row("BTC-20NOV26-66000-C", iv=None))
    assert used_by_surface(row("SOMETHING-NEW")) and used_by_surface({})


def test_deribit_rows_written_once_per_surface_refresh(env):
    env.poll()
    env.replay.fail = deribit_down
    env.poll(advance_s=INTERVAL)                                     # Deribit down: the surface is not refreshed
    assert sum(1 for _ in open(env.deribit_path)) == 1
    env.replay.fail = None
    env.poll(advance_s=INTERVAL)
    assert sum(1 for _ in open(env.deribit_path)) == 2
    assert len({r["ts"] for r in env.records()}) == 3                # the contract log was written every time


def test_contract_log_holds_only_contract_record_types(env):
    env.poll()
    assert {r["type"] for r in env.records()} <= {"book", "spot", "fair"}
    assert all(json.loads(l)["type"] == "deribit" for l in open(env.deribit_path))


# ---- replay compatibility ----------------------------------------------------------------
def test_engine_replay_reads_the_log(env):
    for i in range(12):
        env.poll(advance_s=INTERVAL if i else 0)
    recs = env.records()
    events = replay(recs)                                            # must not raise
    assert isinstance(events, list)
    assert len({r["ts"] for r in recs}) == 12


def test_engine_replay_ignores_what_it_does_not_know(env):
    env.poll()
    recs = env.records()
    extra = [json.loads(open(env.deribit_path).readline()),          # a deribit record in the wrong file
             {"type": "note", "ts": recs[0]["ts"], "text": "restart"},
             {"type": "meta"},                                       # no timestamp at all
             dict(recs[0], future_field={"x": 1})]                   # a known record with an extra field
    assert replay(recs + extra) == replay(recs)


def test_replay_stale_script_runs_on_the_log(env):
    for i in range(3):
        env.poll(advance_s=INTERVAL if i else 0)
    for path in (env.path, env.deribit_path):                        # the script copes with either file
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "replay_stale.py"), path],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert "flag(s) raised" in out.stdout
    assert f"{len(env.records())} records" in subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "replay_stale.py"), env.path],
        capture_output=True, text=True, timeout=60).stdout


# ---- size guard --------------------------------------------------------------------------
def test_health_reports_bytes_and_daily_estimate(env):
    st = env.poll()
    h = st.health()["snapshot_log"]
    size = os.path.getsize(env.path) + os.path.getsize(env.deribit_path)
    assert h["enabled"] and h["path"] == env.path and h["deribit_path"] == env.deribit_path
    assert h["bytes_last_poll"] == h["bytes_today"] == size > 10_000
    assert h["polls_logged"] == 1 and h["records_written"] == len(env.records()) + 1
    assert h["est_mb_per_day"] == pytest.approx(size * 2880 / 1e6, abs=0.06)     # 2880 polls a day at 30 s
    assert h["last_write"] == "2026-11-17T05:12:07Z"
    assert (h["write_errors"], h["dropped"], h["capped"], h["last_error"]) == (0, 0, False, None)
    assert h["every_n_polls"] == 1 and h["every_n_mode"] == "fixed" and h["max_mb"] == 200.0


def test_log_every_n_polls(tmp_path):
    env = Env(tmp_path, every_n_polls=3)
    for i in range(7):
        env.poll(advance_s=INTERVAL if i else 0)
    stamps = sorted({r["ts"] for r in env.records()})
    assert [s[11:19] for s in stamps] == ["05:12:07", "05:13:37", "05:15:07"]      # polls 1, 4, 7
    h = env.state.health()["snapshot_log"]
    assert h["polls_logged"] == 3 and h["every_n_polls"] == 3
    assert h["est_mb_per_day"] == pytest.approx(h["bytes_last_poll"] * 960 / 1e6, rel=0.05)


def test_auto_mode_thins_logging_to_fit_the_budget(tmp_path):
    env = Env(tmp_path, every_n_polls=0, max_mb=100.0)               # 0 = automatic
    env.poll()
    h = env.state.health()["snapshot_log"]
    every_poll_mb = h["bytes_last_poll"] * 2880 / 1e6
    assert h["every_n_mode"] == "auto" and every_poll_mb > 100       # every poll would not fit
    n = h["every_n_polls"]
    assert n > 1 and every_poll_mb / n <= 92 and every_poll_mb / (n - 1) > 90      # the smallest N that fits
    assert h["est_mb_per_day"] <= 92
    for _ in range(2 * n):
        env.poll(advance_s=INTERVAL)
    assert env.state.health()["snapshot_log"]["polls_logged"] == 3   # polls 1, n+1, 2n+1

    roomy = Env(tmp_path / "roomy", every_n_polls=0, max_mb=10_000.0)
    roomy.poll()
    assert roomy.state.health()["snapshot_log"]["every_n_polls"] == 1


def test_daily_cap_stops_writing_and_resets_next_day(tmp_path):
    env = Env(tmp_path, every_n_polls=1, max_mb=0.2)                 # room for about one poll
    env.poll()
    size = os.path.getsize(env.path)
    for _ in range(3):
        env.poll(advance_s=INTERVAL)
    h = env.state.health()["snapshot_log"]
    assert h["capped"] is True and h["polls_skipped_cap"] >= 2
    assert h["bytes_today"] <= 0.2e6 and os.path.getsize(env.path) >= size
    assert env.state.status() == "ok"                                # the poller is unaffected

    env.t = datetime(2026, 11, 18, 0, 0, 5, tzinfo=timezone.utc)     # a new UTC day, a new file
    env.poll()
    h = env.state.health()["snapshot_log"]
    assert h["path"].endswith("2026-11-18.jsonl") and os.path.exists(h["path"])
    assert h["capped"] is False and 0 < h["bytes_today"] <= 0.2e6


def test_bytes_today_counts_an_existing_file(tmp_path):
    env = Env(tmp_path, every_n_polls=1)
    env.poll()
    first = env.state.health()["snapshot_log"]["bytes_today"]
    again = Env(tmp_path, every_n_polls=1)                           # a restart on the same day
    again.poll()
    h = again.state.health()["snapshot_log"]
    assert h["bytes_today"] == first + h["bytes_last_poll"] == os.path.getsize(env.path) + os.path.getsize(env.deribit_path)


# ---- failures never reach the poller -----------------------------------------------------
def test_write_error_is_recorded_and_the_poller_carries_on(tmp_path):
    blocker = tmp_path / "snapshots"
    blocker.write_text("this is a file, so the log directory cannot be created")
    env = Env(tmp_path, every_n_polls=1)
    st = env.poll()
    env.poll(advance_s=INTERVAL)
    h = st.health()["snapshot_log"]
    assert st.polls == 2 and st.status() == "ok" and st.loop_errors == 0
    assert h["write_errors"] == 2 and h["polls_logged"] == 0 and h["last_error"]
    assert "Error" in h["last_error"]


def test_bad_state_never_raises_from_on_poll(tmp_path):
    logger = SnapshotLogger(LogSettings(dir=str(tmp_path)), background=False)
    assert logger.on_poll(object()) is False                         # not a State at all
    assert logger.write_errors == 1 and "AttributeError" in logger.last_error


def test_background_thread_writes_without_blocking(tmp_path):
    env = Env(tmp_path, background=True, every_n_polls=1)
    slow = {"n": 0}
    real = env.logger._write

    def held(*job):                                                  # the disk is slow
        slow["n"] += 1
        gate.wait(5)
        real(*job)

    import threading
    gate = threading.Event()
    env.logger._write = held
    asyncio.run(env.poller.poll_once())                              # returns while the write is still held
    assert env.state.polls == 1 and not os.path.exists(env.path)
    gate.set()
    env.logger.flush()
    assert os.path.exists(env.path) and slow["n"] == 1
    env.logger.close()
    assert env.logger._thread is None


def test_full_queue_drops_instead_of_waiting(tmp_path):
    import threading
    env = Env(tmp_path, background=True, every_n_polls=1)
    gate = threading.Event()
    real = env.logger._write
    env.logger._write = lambda *job: (gate.wait(10), real(*job))
    for i in range(25):                                              # the queue holds 20
        env.t += timedelta(seconds=INTERVAL)
        asyncio.run(env.poller.poll_once())
    assert env.state.polls == 25 and env.logger.dropped >= 3
    gate.set()
    env.logger.flush()
    env.logger.close()
    assert env.state.health()["snapshot_log"]["dropped"] == env.logger.dropped


# ---- warm start --------------------------------------------------------------------------
def test_restart_reloads_the_last_ten_minutes(tmp_path):
    env = Env(tmp_path, every_n_polls=1)
    for i in range(30):                                              # 14.5 minutes of polls
        env.poll(advance_s=INTERVAL if i else 0)
    ids = [r["contract_id"] for r in by_type(env.records())["book"]]
    two_sided = next(cid for cid in ids if cid.startswith("GEMI") and env.state.history.series(cid))

    again = Env(tmp_path, every_n_polls=1)                           # a new process, same log directory
    again.t = env.t + timedelta(seconds=20)
    assert again.state.history.series(two_sided) == [] and again.state.spot_history == []
    w = again.logger.warm(again.state)
    cutoff = again.t - timedelta(seconds=600)
    series, spots = again.state.history.series(two_sided), again.state.spot_history
    assert len(spots) == 20 and w["spot_records"] == 20              # 30 s apart over 10 minutes
    assert len(series) == 20 and all(cutoff <= t <= again.t for t, _ in series + spots)
    assert spots[0][0] == cutoff + timedelta(seconds=10) and w["from"] == "2026-11-17T05:17:07Z"
    assert [v for _, v in spots] == [env.replay.meta["spot"]] * 20
    assert w["book_records"] > 1000
    assert again.state.health()["snapshot_log"]["warm_start"] == w

    # The frozen-quote rule needs 5 minutes of history: it is usable on the first poll after restart.
    again.poll()
    assert (again.t - again.state.history.series(two_sided)[0][0]).total_seconds() >= 300


def test_warm_start_runs_when_the_poller_starts(tmp_path):
    env = Env(tmp_path, every_n_polls=1)
    env.poll()
    again = Env(tmp_path, every_n_polls=1)
    again.t = env.t + timedelta(seconds=45)
    again.poller.settings.poll_interval_s = 3600.0

    async def go():
        task = asyncio.create_task(again.poller.run())
        while again.state.polls == 0:
            await asyncio.sleep(0.01)
        again.poller.stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(go())
    assert again.logger.warm_start["spot_records"] == 1
    assert len(again.state.spot_history) == 2                        # one reloaded, one from the new poll


def test_warm_start_with_no_log_or_a_damaged_one(tmp_path):
    env = Env(tmp_path, every_n_polls=1)
    assert env.logger.warm(env.state) == {"book_records": 0, "spot_records": 0, "from": None}
    env.poll()
    with open(env.path, "ab") as f:
        f.write(b'{"type":"spot","ts":"2026-11-17T05:12:20.000Z","asset":"BTC","pri')      # cut off by a crash
    again = Env(tmp_path, every_n_polls=1)
    again.t = env.t + timedelta(seconds=30)
    w = again.logger.warm(again.state)
    assert w["spot_records"] == 1 and again.logger.write_errors == 0


def test_warm_start_reads_yesterdays_file_just_after_midnight(tmp_path):
    env = Env(tmp_path, every_n_polls=1)
    env.t = datetime(2026, 11, 17, 23, 58, 0, tzinfo=timezone.utc)
    env.poll()
    again = Env(tmp_path, every_n_polls=1)
    again.t = datetime(2026, 11, 18, 0, 3, 0, tzinfo=timezone.utc)
    assert again.logger.warm(again.state)["spot_records"] == 1


def test_tail_records_reads_only_what_it_needs(tmp_path):
    path = tmp_path / "big.jsonl"
    t0 = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
    pad = "x" * 2000
    with open(path, "w") as f:
        f.write("# a comment line\n")
        for i in range(3000):                                        # about 6 MB, one record per second
            ts = (t0 + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            f.write(json.dumps({"type": "spot", "ts": ts, "price": i, "pad": pad}) + "\n")
    got = list(tail_records(str(path), t0 + timedelta(seconds=2990)))
    assert [r["price"] for r in got] == list(range(2990, 3000))
    assert len(list(tail_records(str(path), t0))) == 3000            # asking for everything still works
    assert list(tail_records(str(tmp_path / "missing.jsonl"), t0)) == []


# ---- selection helpers -------------------------------------------------------------------
def test_in_window_on_books():
    w = (63000, 67000)
    assert in_window(mk("kalshi", 65000), w) and not in_window(mk("kalshi", 54600), w)
    assert in_window(mk("kalshi", None, kind="bucket", lo=66900, hi=67400), w)
    assert not in_window(mk("kalshi", None, kind="bucket", lo=70000, hi=70500), w)
    assert not in_window(mk("kalshi", None, kind="bucket", lo=None, hi=57000), w)
    assert not in_window(mk("kalshi", 65000), None)


def test_select_books_with_an_empty_state():
    assert select_books(State(), 0.03) == []


def test_settings_from_env(monkeypatch):
    for k in ("LOG_DIR", "LOG_ENABLED", "LOG_EVERY_N_POLLS", "LOG_MAX_MB"):
        monkeypatch.delenv(k, raising=False)
    d = LogSettings.from_env()
    assert (d.dir, d.enabled, d.every_n_polls, d.max_mb) == ("data/snapshots", True, 2, 200.0)
    monkeypatch.setenv("LOG_EVERY_N_POLLS", "auto")
    assert LogSettings.from_env().every_n_polls == 0
    monkeypatch.setenv("LOG_DIR", "/tmp/x")
    monkeypatch.setenv("LOG_ENABLED", "0")
    monkeypatch.setenv("LOG_EVERY_N_POLLS", "4")
    monkeypatch.setenv("LOG_MAX_MB", "250")
    s = LogSettings.from_env()
    assert (s.dir, s.enabled, s.every_n_polls, s.max_mb) == ("/tmp/x", False, 4, 250.0)
