"""Offline tests for replay mode: virtual clock, looping, surface rebuild, and live/replay parity."""
import asyncio
import gzip
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.replay import Replayer, load_deribit, load_frames
from backend.tests.test_logger import INTERVAL, Env as LogEnv
from engine.books import to_utc

N_POLLS = 6
DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "demo")
DEMO_LOG = os.path.join(DEMO_DIR, "snapshots.jsonl.gz")
DEMO_DERIBIT = os.path.join(DEMO_DIR, "deribit.jsonl.gz")


def near_money(rows):
    """Pick the strike quoted on both venues whose fair value is closest to 0.5."""
    both = [r for r in rows if r["gemini"] and r["kalshi"] and r["fair"] is not None]
    return min(both, key=lambda r: abs(r["fair"] - 0.5))["strike"]


@pytest.fixture(scope="module")
def rec(tmp_path_factory):
    """Record a short log from the live code path, and keep what the live API said at the first poll."""
    env = LogEnv(tmp_path_factory.mktemp("rec"), every_n_polls=1)
    env.poll()
    live = TestClient(create_app(state=env.state))
    first, second, third = env.state.expiries
    ladders = {e: live.get("/ladder", params={"expiry": e}).json() for e in env.state.expiries}
    strike = near_money(ladders[second])
    bodies = {"above": {"expiry": second, "type": "above", "lo": strike, "size": 10},
              "between": {"expiry": second, "type": "between", "lo": strike - 250, "hi": strike + 250, "size": 5},
              "near_expiry": {"expiry": first, "type": "above", "lo": near_money_any(ladders[first]), "size": 1}}
    routes = {k: live.post("/route", json=b).json() for k, b in bodies.items()}
    out = {"t0": env.t, "expiries": list(env.state.expiries), "ladders": ladders, "bodies": bodies,
           "routes": routes, "expiries_json": live.get("/expiries").json(), "spot": env.state.spot}
    for _ in range(N_POLLS - 1):
        env.poll(advance_s=INTERVAL)
    out.update(path=env.path, deribit_path=env.deribit_path, dir=env.dir)
    return out


def near_money_any(rows):
    both = [r for r in rows if r["gemini"] and r["kalshi"] and r["gemini"]["ask"] and r["kalshi"]["ask"]]
    return both[len(both) // 2]["strike"]


@pytest.fixture
def rp(rec):
    return Replayer(rec["path"], rec["deribit_path"])


def client(rp):
    return TestClient(create_app(replayer=rp))


def shape(x):
    """The structure of a JSON value: keys and value types, without the values."""
    if isinstance(x, dict):
        return {k: shape(v) for k, v in x.items()}
    if isinstance(x, list):
        return [shape(x[0])] if x else []
    return "number" if isinstance(x, (int, float)) and not isinstance(x, bool) else type(x).__name__


# ---- loading -----------------------------------------------------------------------------
def test_frames_are_grouped_by_timestamp_in_order(rec):
    frames = load_frames(rec["path"])
    assert len(frames) == N_POLLS
    assert [f.ts for f in frames] == [rec["t0"] + timedelta(seconds=INTERVAL * i) for i in range(N_POLLS)]
    assert all(f.spot == rec["spot"] and len(f.books) > 100 for f in frames)
    assert {b.venue for b in frames[0].books} == {"gemini", "kalshi"}
    deribit = load_deribit(rec["deribit_path"])
    assert [ts for ts, _ in deribit] == [f.ts for f in frames] and all(len(rows) > 100 for _, rows in deribit)


def test_gzipped_logs_load_the_same(rec, tmp_path):
    for name in ("path", "deribit_path"):
        with open(rec[name], "rb") as src, gzip.open(tmp_path / (os.path.basename(rec[name]) + ".gz"), "wb") as dst:
            shutil.copyfileobj(src, dst)
    gz = Replayer(str(tmp_path / (os.path.basename(rec["path"]) + ".gz")),
                  str(tmp_path / (os.path.basename(rec["deribit_path"]) + ".gz")))
    plain = Replayer(rec["path"], rec["deribit_path"])
    assert len(gz.frames) == len(plain.frames) == N_POLLS and len(gz.deribit) == N_POLLS
    assert client(gz).get("/ladder", params={"expiry": rec["expiries"][1]}).json() == \
        client(plain).get("/ladder", params={"expiry": rec["expiries"][1]}).json()


def test_unknown_and_broken_lines_are_ignored(rec, tmp_path):
    path = tmp_path / "noisy.jsonl"
    with open(rec["path"], encoding="utf-8") as src, open(path, "w", encoding="utf-8") as dst:
        dst.write("# a comment\n\n")
        dst.write('{"type":"note","ts":"2026-11-17T05:12:07.000Z","text":"hello"}\n')
        dst.write('{"type":"meta"}\n[1, 2, 3]\n')
        dst.write(src.read())
        dst.write('{"type":"book","ts":"2026-11-17T05:20:00.000Z","venue":"gem')          # cut off by a crash
    assert [f.ts for f in load_frames(str(path))] == [f.ts for f in load_frames(rec["path"])]


def test_empty_log_is_an_error(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text('{"type":"spot","ts":"2026-11-17T05:12:07.000Z","asset":"BTC","price":65000}\n')
    with pytest.raises(ValueError, match="no book snapshots"):
        Replayer(str(path))


# ---- /health -----------------------------------------------------------------------------
def test_health_shows_replay_mode_and_the_snapshot_ts(rp, rec):
    r = client(rp).get("/health")
    assert r.status_code == 200
    h = r.json()
    assert h["mode"] == "replay" and h["status"] == "ok" and h["snapshot_log"] is None
    assert h["last_poll"] == "2026-11-17T05:12:07Z"
    assert h["replay"] == {"file": rec["path"], "deribit_file": rec["deribit_path"],
                           "snapshot_ts": "2026-11-17T05:12:07Z", "snapshot": 1, "snapshots": N_POLLS,
                           "first_ts": "2026-11-17T05:12:07Z", "last_ts": "2026-11-17T05:14:37Z",
                           "speed": 1.0, "loops": 0, "surface": True, "deribit_records": N_POLLS}
    assert all(v["ok"] and v["requests"] == 0 for v in h["venues"].values())      # no venue was contacted
    rp.step()
    h = client(rp).get("/health").json()
    assert (h["replay"]["snapshot_ts"], h["replay"]["snapshot"]) == ("2026-11-17T05:12:37Z", 2)
    assert h["last_poll"] == "2026-11-17T05:12:37Z" and h["polls"] == 2


# ---- virtual clock -----------------------------------------------------------------------
def test_clock_is_the_snapshot_time_not_the_wall_clock(rp, rec):
    first, second, third = rec["expiries"]
    assert to_utc(first) < datetime.now(timezone.utc)                # on the wall clock this expiry is long past
    assert rp.state.now() == rec["t0"] == rp.ts
    c = client(rp)
    assert c.get("/expiries").json() == [first, second, third]       # still served: the virtual clock is before it
    assert c.get("/ladder", params={"expiry": first}).status_code == 200
    got = c.post("/route", json=rec["bodies"]["above"]).json()
    assert got["timestamp"] == "2026-11-17T05:12:07Z"
    rp.step()
    assert rp.state.now() == rec["t0"] + timedelta(seconds=INTERVAL)
    assert c.post("/route", json=rec["bodies"]["above"]).json()["timestamp"] == "2026-11-17T05:12:37Z"


def test_two_hour_fair_cutoff_uses_the_virtual_clock(rp, rec):
    first, second, third = rec["expiries"]                           # 48 minutes, 17 hours, and 4 days away
    c = client(rp)
    near = c.get("/ladder", params={"expiry": first}).json()
    far = c.get("/ladder", params={"expiry": second}).json()
    assert near and all(row["fair"] is None for row in near)
    assert far and all(row["fair"] is not None for row in far)
    r = c.post("/route", json=rec["bodies"]["near_expiry"]).json()
    assert r["fair_value"] is None and r["verdict_code"] == "unavailable"
    r = c.post("/route", json=rec["bodies"]["above"]).json()
    assert r["fair_value"] is not None and r["verdict_code"] in ("options_higher", "in_line", "options_lower")


def test_stale_rules_see_recorded_history_on_the_virtual_clock(rp, rec):
    st = rp.state
    assert len(st.spot_history) == 1
    for _ in range(3):
        rp.step()
    spots = st.spot_history
    assert [t for t, _ in spots] == [rec["t0"] + timedelta(seconds=INTERVAL * i) for i in range(4)]
    gem = next(cid for cid in st.books["gemini"] if st.history.series(cid))
    assert [t for t, _ in st.history.series(gem)] == [t for t, _ in spots]
    assert all(t <= st.now() for t, _ in spots)                      # nothing is "in the future" for the detector
    rows = client(rp).get("/ladder", params={"expiry": rec["expiries"][1]}).json()
    assert all(set(row["stale"]) == {"gemini", "kalshi"} for row in rows)


# ---- looping -----------------------------------------------------------------------------
def test_loop_wraps_to_the_first_snapshot(rp, rec):
    for _ in range(N_POLLS - 1):
        rp.step()
    assert rp.index == N_POLLS - 1 and rp.loops == 0
    assert rp.state.now() == rec["t0"] + timedelta(seconds=INTERVAL * (N_POLLS - 1))
    assert len(rp.state.spot_history) == N_POLLS
    rp.step()                                                        # past the end
    assert rp.index == 0 and rp.loops == 1 and rp.state.now() == rec["t0"]
    assert len(rp.state.spot_history) == 1                           # the history restarts with the clock
    c = client(rp)
    h = c.get("/health").json()
    assert h["replay"]["loops"] == 1 and h["replay"]["snapshot"] == 1 and h["status"] == "ok"
    assert c.get("/expiries").json() == rec["expiries"]
    assert c.post("/route", json=rec["bodies"]["above"]).json()["total_cost"] == rec["routes"]["above"]["total_cost"]


def test_run_advances_in_order_and_loops(rec):
    rp = Replayer(rec["path"], rec["deribit_path"], speed=1e9)
    seen = []
    real = rp.step

    def spy():
        frame = real()
        seen.append(rp.index)
        if rp.loops >= 2:
            rp.stop()
        return frame

    rp.step = spy
    asyncio.run(asyncio.wait_for(rp.run(), timeout=30))
    assert seen[:N_POLLS + 1] == [1, 2, 3, 4, 5, 0, 1] and rp.loops == 2


def test_run_without_loop_stops_at_the_end(rec):
    rp = Replayer(rec["path"], rec["deribit_path"], speed=1e9, loop=False)
    asyncio.run(asyncio.wait_for(rp.run(), timeout=30))
    assert rp.index == N_POLLS - 1 and rp.loops == 0


def test_replay_speed_scales_the_wait(rec):
    rp = Replayer(rec["path"], rec["deribit_path"], speed=150.0)     # 30 s between snapshots becomes 0.2 s

    async def go():
        task = asyncio.create_task(rp.run())
        await asyncio.sleep(0.1)
        early = rp.index
        await asyncio.sleep(0.75)
        rp.stop()
        await asyncio.wait_for(task, timeout=5)
        return early

    assert asyncio.run(go()) == 0                                    # not yet due after 0.1 s
    assert 1 <= rp.index <= 5      # ~4 steps in 0.85 s; >=1 since each step also rebuilds the surface (slow machines)
    with pytest.raises(ValueError):
        Replayer(rec["path"], speed=0)


# ---- surface rebuild ---------------------------------------------------------------------
def test_surface_is_rebuilt_from_the_deribit_file(rp, rec):
    assert rp.state.surface is not None and rp.state.surface_at == rec["t0"]
    second = rec["expiries"][1]
    live = {row["strike"]: row["fair"] for row in rec["ladders"][second]}
    got = {row["strike"]: row["fair"] for row in client(rp).get("/ladder", params={"expiry": second}).json()}
    assert got and all(got[k] == live[k] for k in got)               # the same fair values the live API gave
    r = client(rp).post("/route", json=rec["bodies"]["between"]).json()
    assert r["fair_value"] == pytest.approx(rec["routes"]["between"]["fair_value"], abs=1e-9)
    assert r["verdict_code"] == rec["routes"]["between"]["verdict_code"]
    assert r["verdict_text"] == rec["routes"]["between"]["verdict_text"]
    first_surface = rp.state.surface
    rp.step()
    assert rp.state.surface is not first_surface and rp.state.surface_at == rec["t0"] + timedelta(seconds=INTERVAL)
    for _ in range(N_POLLS):
        rp.step()                                                    # around the loop: surfaces come from the cache
    assert rp.state.surface is rp._surfaces[1] and len(rp._surfaces) == N_POLLS


def test_without_a_deribit_file_fair_is_null(rec):
    rp = Replayer(rec["path"])
    c = client(rp)
    h = c.get("/health").json()
    assert h["replay"]["surface"] is False and h["replay"]["deribit_file"] is None and h["status"] == "ok"
    rows = c.get("/ladder", params={"expiry": rec["expiries"][1]}).json()
    assert rows and all(row["fair"] is None for row in rows)
    r = c.post("/route", json=rec["bodies"]["above"]).json()
    assert r["fair_value"] is None and r["verdict_code"] == "unavailable" and r["total_cost"] == rec["routes"]["above"]["total_cost"]


# ---- live and replay return the same shapes ----------------------------------------------
def test_ladder_has_the_same_shape_and_values_in_live_and_replay(rp, rec):
    c = client(rp)
    assert c.get("/expiries").json() == rec["expiries_json"]
    for e in rec["expiries"]:
        live, got = rec["ladders"][e], c.get("/ladder", params={"expiry": e}).json()
        assert got and shape(got[0]) == shape(next(r for r in live if r["strike"] == got[0]["strike"]))
        by_strike = {row["strike"]: row for row in live}
        assert {row["strike"] for row in got} <= set(by_strike)      # replay holds the logged subset
        both = 0
        for row in got:
            want = by_strike[row["strike"]]
            assert set(row) == set(want)
            for v in ("gemini", "kalshi"):
                if row[v] is None:
                    continue                                         # that venue's book was outside the log window
                assert set(row[v]) == set(want[v])
                assert {k: x for k, x in row[v].items() if k != "updated"} == \
                    {k: x for k, x in want[v].items() if k != "updated"}
            assert row["fair"] == want["fair"]
            if row["gemini"] and row["kalshi"]:
                both += 1
                assert row["stale"] == want["stale"] and row["stale_reasons"] == want["stale_reasons"]
        assert both > 0


def test_route_has_the_same_shape_and_values_in_live_and_replay(rp, rec):
    c = client(rp)
    for name, body in rec["bodies"].items():
        r = c.post("/route", json=body)
        assert r.status_code == 200, name
        got, want = r.json(), rec["routes"][name]
        assert shape(got) == shape(want), name
        for k in ("legs", "total_cost", "best_single_venue_cost", "savings", "verdict_code", "verdict_text",
                  "fair_note", "mixed_settlement_index",
                  "build", "cash_outlay", "filled_size", "timestamp", "stale_warnings"):
            assert got[k] == want[k], (name, k)
        assert got["fair_value"] == (pytest.approx(want["fair_value"], abs=1e-9) if want["fair_value"] is not None else None)


def test_errors_are_the_same_in_live_and_replay(rp, rec):
    c = client(rp)
    second = rec["expiries"][1]
    r = c.post("/route", json={"expiry": second, "type": "above", "lo": rec["bodies"]["above"]["lo"] + 123})
    assert r.status_code == 422 and set(r.json()) == {"detail", "nearest"} and len(r.json()["nearest"]) == 2
    assert c.get("/ladder", params={"expiry": "2026-12-25T22:00"}).status_code == 404
    assert c.get("/expiries", params={"asset": "DOGE"}).status_code == 422


# ---- app wiring --------------------------------------------------------------------------
def test_replay_file_env_serves_the_api_with_no_venue_calls(rec, monkeypatch):
    async def no_network(self, *a, **kw):
        raise AssertionError("replay mode must not call a venue")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    monkeypatch.setenv("REPLAY_FILE", rec["path"])
    monkeypatch.setenv("REPLAY_DERIBIT_FILE", rec["deribit_path"])
    monkeypatch.setenv("REPLAY_SPEED", "300")                        # a snapshot every 0.1 s
    app = create_app()
    assert app.state.poller is None and app.state.replayer is not None
    assert app.state.replayer.speed == 300.0 and not os.path.exists("data/snapshots/replay")
    with TestClient(app) as c:                                       # entering starts the replay loop
        assert c.get("/health").json()["mode"] == "replay"
        seen = set()
        deadline = time.monotonic() + 20
        while len(seen) < 3:
            assert time.monotonic() < deadline, "the replay never advanced"
            seen.add(c.get("/health").json()["replay"]["snapshot_ts"])
            assert c.get("/ladder", params={"expiry": rec["expiries"][1]}).status_code == 200
            assert c.post("/route", json=rec["bodies"]["above"]).status_code == 200
            time.sleep(0.03)
        h = c.get("/health").json()
        assert h["snapshot_log"] is None and all(v["requests"] == 0 for v in h["venues"].values())
    assert app.state.replayer._stop.is_set()


def test_without_replay_file_the_app_is_live(monkeypatch):
    monkeypatch.delenv("REPLAY_FILE", raising=False)
    monkeypatch.setenv("LOG_ENABLED", "0")
    app = create_app()
    assert app.state.replayer is None and app.state.poller is not None
    assert app.state.store.mode == "live" and app.state.store.replay is None


# ---- the committed demo fixture ----------------------------------------------------------
def test_demo_fixture_is_small_long_enough_and_far_from_expiry():
    assert os.path.exists(DEMO_LOG) and os.path.exists(DEMO_DERIBIT)
    assert os.path.getsize(DEMO_LOG) + os.path.getsize(DEMO_DERIBIT) < 3_000_000
    frames = load_frames(DEMO_LOG)
    gaps = [(b.ts - a.ts).total_seconds() for a, b in zip(frames, frames[1:])]
    assert len(frames) >= 31 and (frames[-1].ts - frames[0].ts).total_seconds() >= 15 * 60
    assert all(25 <= g <= 40 for g in gaps)                          # one snapshot every 30 s, no holes
    for f in frames:
        soonest = min(b.expiry for b in f.books)
        assert (soonest - f.ts).total_seconds() > 2 * 3600           # fair values are never hidden
        assert f.spot and {b.venue for b in f.books} == {"gemini", "kalshi"}
    assert len(load_deribit(DEMO_DERIBIT)) == len(frames)


def test_demo_fixture_serves_fair_values_and_verdicts():
    rp = Replayer(DEMO_LOG, DEMO_DERIBIT)
    c = client(rp)
    h = c.get("/health").json()
    assert h["mode"] == "replay" and h["status"] == "ok" and h["replay"]["surface"] is True
    expiries = c.get("/expiries").json()
    assert len(expiries) >= 2
    for _ in range(3):
        for e in expiries:
            rows = c.get("/ladder", params={"expiry": e}).json()
            assert rows and all(row["fair"] is not None and 0 <= row["fair"] <= 1 for row in rows)
            k = near_money(rows)
            r = c.post("/route", json={"expiry": e, "type": "above", "lo": k, "size": 10})
            assert r.status_code == 200
            got = r.json()
            assert 0 < got["total_cost"] < 1 and 0 < got["fair_value"] < 1
            assert got["verdict_code"] in ("options_higher", "in_line", "options_lower") and got["verdict_text"].startswith("Options market prices")
        rp.step()
