"""report --weekly (aggregation math, labels) and the read-only loopback dashboard."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import socket
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import CANARIES, credential_guard

from tokenstretch.cli import main
from tokenstretch.context import build_context
from tokenstretch.dashboard import DashboardState, build_state, make_server, render_page
from tokenstretch.findings import UsageRecord
from tokenstretch.weekly import NOT_REPORTED, aggregate, build_weekly, since_setup, week_start

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)  # a Wednesday


def _rec(ts: datetime | None, model="claude-sonnet-4-5", inp=10, cr=1000, cw=0, out=50, cost=None, sid="s1"):
    return UsageRecord(adapter_id="claude-code", source="log", session_id=sid, model=model,
                       ts=ts.isoformat().replace("+00:00", "Z") if ts else None, input_tokens=inp,
                       cache_read_tokens=cr, cache_write_tokens=cw, output_tokens=out, cost_usd=cost)


# ---- weekly math ------------------------------------------------------------------------

def test_week_start_is_monday():
    assert week_start(NOW, UTC) == "2026-11-09"
    assert week_start(datetime(2026, 11, 8, 23, 59, tzinfo=UTC), UTC) == "2026-11-02"


def test_aggregate_buckets_tokens_by_week_type_and_model():
    recs = [
        _rec(NOW - timedelta(hours=1), inp=10, cr=1000, out=50),                       # today
        _rec(NOW - timedelta(days=1), model="claude-opus-4-1", inp=20, cr=3000, cw=7, out=5),  # this week
        _rec(NOW - timedelta(days=8), inp=1, cr=100, out=1, sid="s2"),               # last week
        _rec(NOW - timedelta(days=60)),                                              # outside the 4 weeks
        _rec(None),                                                                  # undated
    ]
    agg = aggregate(recs, weeks=4, now=NOW, tz=UTC)
    assert [w["week"] for w in agg["weeks"]] == ["2026-11-09", "2026-11-02", "2026-10-26", "2026-10-19"]
    wk = agg["this_week"]
    assert wk["tokens"] == {"input_uncached": 30, "cache_read": 4000, "cache_write": 7, "output": 55, "reasoning": 0}
    assert wk["total_tokens"] == 4092 and wk["records"] == 2 and wk["sessions"] == 1
    assert [m["model"] for m in wk["models"]] == ["claude-opus-4-1", "claude-sonnet-4-5"]
    assert wk["models"][0]["total_tokens"] == 3032
    assert agg["today"]["total_tokens"] == 1060
    assert agg["weeks"][1]["total_tokens"] == 102
    assert agg["weeks"][2]["records"] == 0
    assert agg["undated_records"] == 1


def test_cost_only_where_logs_report_it():
    agg = aggregate([_rec(NOW), _rec(NOW)], weeks=1, now=NOW, tz=UTC)
    assert agg["this_week"]["cost"] == {"usd": None, "label": NOT_REPORTED, "reported": 0, "records": 2}
    agg = aggregate([_rec(NOW, cost=0.25), _rec(NOW)], weeks=1, now=NOW, tz=UTC)
    c = agg["this_week"]["cost"]
    assert c["usd"] == 0.25 and c["label"] == "reported for 1 of 2 steps"
    agg = aggregate([_rec(NOW, cost=0.25), _rec(NOW, cost=0.5)], weeks=1, now=NOW, tz=UTC)
    assert agg["this_week"]["cost"]["usd"] == 0.75 and agg["this_week"]["cost"]["label"] == "reported by the logs"


def test_since_setup_is_observational_before_after():
    when = NOW - timedelta(days=2)
    recs = [_rec(when - timedelta(days=1), model="claude-opus-4", cr=10_000, inp=0, out=100),
            _rec(when + timedelta(hours=5), cr=2_000, inp=0, out=40),
            _rec(when + timedelta(hours=6), cr=4_000, inp=0, out=60),
            _rec(when - timedelta(days=10))]  # outside the equal-length before window
    ss = since_setup(recs, when, now=NOW, is_top=lambda r: "opus" in (r.model or ""))
    assert "Observational" in ss["label"] and "Not a controlled bench" in ss["label"]
    assert ss["before"]["steps"] == 1 and ss["before"]["avg_input_per_step"] == 10_000
    assert ss["before"]["top_model_share"] == 1.0
    assert ss["after"]["steps"] == 2 and ss["after"]["avg_input_per_step"] == 3_000
    assert ss["after"]["avg_output_per_step"] == 50 and ss["after"]["top_model_share"] == 0.0
    assert since_setup(recs, None) is None


# ---- a synthetic machine -------------------------------------------------------------------

def _row(i, model, history, ts, read=None, out=120, cost=None):
    content = [{"type": "text", "text": "ok"}]
    if read:
        content.append({"type": "tool_use", "name": "Read", "input": {"file_path": read}})
    row = {"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"), "message": {
        "id": f"m{i}", "model": model, "content": content,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": history, "cache_creation_input_tokens": 0,
                  "output_tokens": out}}}
    if cost is not None:
        row["costUSD"] = cost
    return row


def _world(tmp_path: Path, *, cost=None) -> Path:
    home, root = tmp_path / "home", tmp_path / "root"
    proj = home / ".claude" / "projects" / "-Users-someone-code-demo-app"
    proj.mkdir(parents=True)
    root.mkdir()
    (tmp_path / "sys").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE")
    now = datetime.now(UTC)
    read = str(root / "src" / "app.py")
    rows = [_row(i, "claude-opus-4-1", 100_000 + 20_000 * i, now - timedelta(seconds=40 * (6 - i)), read=read,
                 cost=cost) for i in range(6)]
    (proj / "abcd1234-0000.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return tmp_path


def _ctx(w: Path):
    return build_context(w / "home", w / "root", w / "sys")


def test_weekly_report_from_logs_cli(tmp_path):
    w = _world(tmp_path, cost=0.5)
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["report", "--weekly", "--json", "--home", str(w / "home"), "--root", str(w / "root"),
                     "--sys-snapshot", str(w / "sys")])
    assert code == 0
    data = json.loads(buf.getvalue())
    wk = data["weeks"][0]
    assert wk["records"] == 6 and wk["cost"]["usd"] == 3.0
    assert data["top_sessions"][0]["history_tokens"] == 200_010
    kinds = {x["kind"] for x in data["waste"]}
    assert {"context", "top-model", "rereads"} <= kinds
    assert data["since_setup"] is None and data["bench"] == []
    out = tmp_path / "weekly.html"
    with credential_guard(), redirect_stdout(io.StringIO()):
        main(["report", "--weekly", "--out", str(out), "--home", str(w / "home"), "--root", str(w / "root"),
              "--sys-snapshot", str(w / "sys")])
    page = out.read_text(encoding="utf-8")
    assert "Tokens by week" in page and "Waste spots" in page and "No measured bench" in page
    assert not re.search(r"""(src|href)\s*=\s*['"]?https?://""", page)


def test_weekly_without_cost_says_not_reported(tmp_path):
    w = _world(tmp_path)
    with credential_guard():
        data = build_weekly(_ctx(w))
    assert data["weeks"][0]["cost"]["label"] == NOT_REPORTED and data["weeks"][0]["cost"]["usd"] is None


def test_bench_results_only_when_files_exist(tmp_path):
    w = _world(tmp_path)
    d = w / "home" / ".tokenstretch" / "benches" / "20261110T120000"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({"before": {"input_uncached": 10, "cost_usd": 1.5},
                                               "after": {"input_uncached": 5, "cost_usd": 0.5}}), encoding="utf-8")
    with credential_guard():
        data = build_weekly(_ctx(w))
    assert data["bench"][0]["id"] == "20261110T120000" and data["bench"][0]["after"]["cost_usd"] == 0.5


# ---- dashboard state ---------------------------------------------------------------------

def test_dashboard_state_json(tmp_path):
    w = _world(tmp_path, cost=0.25)
    with credential_guard():
        st = build_state(_ctx(w))
    blob = json.dumps(st)
    for c in CANARIES:
        assert c not in blob
    assert st["footer"] == "Runs only on this machine. Nothing is sent anywhere."
    o = st["overview"]
    assert o["tokens_week"] == o["tokens_today"] > 0 and o["cost_week"]["usd"] == 1.5
    assert o["active_sessions"] == 1 and o["biggest_history"]["tokens"] == 200_010
    s = st["sessions"][0]
    assert s["tool"] == "Claude Code" and s["project"] == "~-code-demo-app" and s["long"] and s["pct"] == 100.0
    assert {x["kind"] for x in st["warnings"]} >= {"context", "top-model", "rereads"}
    assert st["models"][0]["model"] == "claude-opus-4-1"
    assert len(st["weekly"]) == 8 and st["weekly"][-1]["total_tokens"] == o["tokens_week"]
    assert set(st["doctor"]["counts"]) >= {"high", "med", "low", "info"}
    assert st["setup"]["undo_available"] is False and st["setup"]["undo_command"] == "tokenstretch undo"
    assert st["bench"] == [] and st["since_setup"] is None
    assert str(w / "home") not in blob  # home paths are shown as ~


def test_dashboard_warning_feed_dedupes_and_doctor_is_cached(tmp_path):
    w = _world(tmp_path)
    clock = [1000.0]
    with credential_guard():
        ds = DashboardState(_ctx(w), now=lambda: clock[0] + datetime.now(UTC).timestamp())
        first = ds.snapshot()
        second = ds.snapshot()
        assert len(second["warnings"]) == len(first["warnings"]) > 0
        t0 = ds._doctor_at
        ds.snapshot()
        assert ds._doctor_at == t0
        clock[0] += 1
        ds.snapshot(force_doctor=True)
        assert ds._doctor_at > t0


def test_dashboard_html_is_self_contained(tmp_path):
    w = _world(tmp_path)
    with credential_guard():
        st = build_state(_ctx(w))
    for page in (render_page(None), render_page(st, static=True)):
        assert not re.search(r"https?://", page), "the page must not reference any external URL"
        assert not re.search(r"<(link|img|iframe|object|embed)\b", page, re.I)
        assert not re.search(r"<script[^>]+src=", page, re.I)
        assert "@import" not in page and "url(" not in page
        assert "Runs only on this machine. Nothing is sent anywhere." in page
    assert "</script><script" not in json.dumps(st)


def _tree(home: Path) -> dict:
    out = {}
    for p in sorted(home.rglob("*")):
        st = p.lstat()
        out[str(p)] = (st.st_mtime_ns, st.st_size, hashlib.sha256(p.read_bytes()).hexdigest()
                       if p.is_file() and ".credentials" not in p.name else None)
    return out


def test_dashboard_never_writes_files(tmp_path):
    w = _world(tmp_path)
    before_home, before_root = _tree(w / "home"), _tree(w / "root")
    with credential_guard():
        ds = DashboardState(_ctx(w))
        for _ in range(2):
            ds.snapshot()
        ds.snapshot(force_doctor=True)
        render_page(ds.snapshot())
    assert _tree(w / "home") == before_home and _tree(w / "root") == before_root


def test_dashboard_refuses_non_loopback_host(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(ValueError):
        make_server(DashboardState(_ctx(w)), host="0.0.0.0", port=0)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["dashboard", "--host", "0.0.0.0", "--no-open", "--home", str(w / "home"),
                     "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert code == 2 and "only listens on 127.0.0.1" in buf.getvalue()


# ---- live server (needs loopback) ------------------------------------------------------------

def _loopback_works() -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        c = socket.socket()
        c.settimeout(1)
        try:
            _REAL_CONNECT(c, s.getsockname())
            return True
        finally:
            c.close()
    except OSError:
        return False
    finally:
        s.close()


@pytest.fixture
def loopback(monkeypatch):
    """Allow connections to 127.0.0.1 only (the autouse guard still blocks everything else).

    Under `unshare -rn` without `ip link set lo up` there is no loopback; those tests are skipped
    with this reason instead of passing silently.
    """
    import conftest
    real_connect = _REAL_CONNECT  # captured at import, before the autouse guard patches it

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in ("127.0.0.1", "::1"):
            raise conftest.NetworkBlockedError(f"only loopback is allowed here ({address})")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", _create_loopback(connect))
    if not _loopback_works():
        pytest.skip("LOOPBACK-UNAVAILABLE: no loopback interface in this network namespace "
                    "(run `ip link set lo up` inside the namespace to exercise the live server tests)")


_REAL_CONNECT = socket.socket.connect


def _create_loopback(connect):
    def create_connection(address, timeout=None, *a, **k):
        host, port = address
        if host not in ("127.0.0.1", "localhost"):
            import conftest
            raise conftest.NetworkBlockedError(f"only loopback is allowed here ({address})")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout is not None:
            s.settimeout(timeout)
        connect(s, ("127.0.0.1", port))
        return s
    return create_connection


@pytest.fixture
def server(tmp_path, loopback):
    w = _world(tmp_path)
    srv, token = make_server(DashboardState(_ctx(w)), port=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv, token
    srv.shutdown()
    srv.server_close()


def _get(srv, path, host=None, method="GET", headers=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    conn.putrequest(method, path, skip_host=True)
    for k, v in hdrs.items():
        conn.putheader(k, v)
    conn.endheaders()
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


def test_server_binds_loopback_only(server):
    srv, _ = server
    assert srv.server_address[0] == "127.0.0.1"
    assert srv.socket.getsockname()[0] == "127.0.0.1"


def test_server_requires_token(server):
    srv, token = server
    assert _get(srv, "/")[0].status == 403
    assert _get(srv, "/api/state")[0].status == 403
    assert _get(srv, "/wrong-token/api/state")[0].status == 403
    assert _get(srv, f"/{token[:-1]}/api/state")[0].status == 403
    assert _get(srv, "/wrong-token/api/doctor", method="POST")[0].status == 403
    r, body = _get(srv, f"/{token}/api/state")
    assert r.status == 200 and json.loads(body)["footer"].startswith("Runs only on this machine")
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    r, body = _get(srv, f"/{token}/")
    assert r.status == 200 and b"tokenstretch dashboard" in body
    assert not re.search(rb"https?://", body)
    assert _get(srv, f"/{token}")[0].status == 302
    assert _get(srv, f"/{token}/api/doctor", method="POST")[0].status == 200


def test_server_rejects_bad_host_and_origin(server):
    srv, token = server
    port = srv.server_address[1]
    assert _get(srv, f"/{token}/api/state", host=f"evil.example:{port}")[0].status == 403
    assert _get(srv, f"/{token}/api/state", host="rebind.attacker.test")[0].status == 403
    assert _get(srv, f"/{token}/api/state", host=f"localhost:{port}")[0].status == 200
    assert _get(srv, f"/{token}/api/state", headers={"Origin": "https://evil.example"})[0].status == 403


def test_busy_port_picks_a_free_one(tmp_path, loopback):
    w = _world(tmp_path)
    ds = DashboardState(_ctx(w))
    a, _ = make_server(ds, port=0)
    try:
        b, _ = make_server(ds, port=a.server_address[1])
        try:
            assert b.server_address[1] != a.server_address[1] and b.server_address[0] == "127.0.0.1"
        finally:
            b.server_close()
    finally:
        a.server_close()


# ---- background builds: requests never build state ------------------------------------------

def test_requests_never_build_state(tmp_path, monkeypatch):
    from tokenstretch import dashboard as dmod
    w = _world(tmp_path)
    ds = DashboardState(_ctx(w))

    def boom(*a, **k):
        raise AssertionError("state was built on the request path")

    for name in ("build_live", "build_usage", "build_doctor", "refresh", "snapshot"):
        monkeypatch.setattr(ds, name, boom)
    handler = dmod._make_handler(ds, "tok", 5.0)
    # The page is rendered once, up front, from no state at all.
    assert "tokenstretch dashboard" in render_page(None)
    blob = json.loads(ds.latest_json())
    assert blob["warming_up"] is True and blob["pending"] == ["live sessions", "usage logs", "doctor", "instruction files"]
    assert blob["sessions"] is None and blob["doctor"] is None and blob["overview"]["tokens_week"] is None
    ds.request_doctor()  # only sets a flag for the worker
    assert json.loads(ds.latest_json())["doctor_refreshing"] is True
    assert handler is not None


def test_html_and_state_routes_do_not_call_builder(server_lazy):
    srv, token, calls = server_lazy
    t0 = time.perf_counter()
    r, body = _get(srv, f"/{token}/")
    assert r.status == 200 and b"tokenstretch dashboard" in body
    r, body = _get(srv, f"/{token}/api/state")
    assert r.status == 200 and json.loads(body)["warming_up"] is True
    r, _ = _get(srv, f"/{token}/api/doctor", method="POST")
    assert r.status == 200
    assert calls == [] and time.perf_counter() - t0 < 5


def test_worker_publishes_parts_in_background(tmp_path):
    w = _world(tmp_path)
    ds = DashboardState(_ctx(w), interval=0.05)
    ds.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not json.loads(ds.latest_json())["ready"]:
            time.sleep(0.02)
        st = json.loads(ds.latest_json())
        assert st["ready"] and st["updated_local"] and st["sessions"] and st["doctor"] is not None
        assert st["overview"]["tokens_week"] > 0
    finally:
        ds.stop()


@pytest.fixture
def server_lazy(tmp_path, loopback):
    w = _world(tmp_path)
    ds = DashboardState(_ctx(w))
    calls: list[str] = []
    for name in ("build_live", "build_usage", "build_doctor", "refresh", "snapshot"):
        setattr(ds, name, lambda *a, _n=name, **k: calls.append(_n))
    srv, token = make_server(ds, port=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv, token, calls
    srv.shutdown()
    srv.server_close()
