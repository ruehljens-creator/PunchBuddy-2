"""Hardware-freie Unit-Tests für die Kern-Helfer von PunchBuddy.

Setzt voraus, dass `import auto_punch_in` nebenwirkungsfrei ist (keine
Settings-Migration / kein Sprach-Laden beim Import – das passiert erst in
init_runtime()).
"""
import json
import grpc
import pytest

import auto_punch_in as a
import punchbuddy.config as cfg


# ── _deep_merge ───────────────────────────────────────────────────────────
def test_deep_merge_fills_missing_nested_keys():
    default = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 99}}
    out = a._deep_merge(default, override)
    assert out == {"a": 1, "nested": {"x": 1, "y": 99}}


def test_deep_merge_override_wins_for_lists_and_scalars():
    default = {"lst": [1, 2, 3], "s": "def"}
    override = {"lst": [9], "s": "usr"}
    assert a._deep_merge(default, override) == {"lst": [9], "s": "usr"}


def test_deep_merge_does_not_mutate_default():
    default = {"nested": {"x": 1}}
    a._deep_merge(default, {"nested": {"y": 2}})
    assert default == {"nested": {"x": 1}}


# ── load_settings (Deep-Merge + Preset-Backfill) ──────────────────────────
def test_load_settings_backfills_new_default_keys(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    # Alte Datei ohne den (neueren) Key webtrigger_token
    settings_file.write_text(json.dumps({"http_port": 1234}))
    monkeypatch.setattr(cfg, "SETTINGS_PATH", str(settings_file))

    s = a.load_settings()
    assert s["http_port"] == 1234                      # Nutzerwert bleibt
    assert "webtrigger_token" in s                     # neuer Default ergänzt
    assert s["webtrigger_token"] == a.DEFAULT_SETTINGS["webtrigger_token"]


def test_load_settings_backfills_preset_entry_keys(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    # Preset-Eintrag ohne 'export' (neu hinzugekommener Key)
    settings_file.write_text(json.dumps({"track_presets": [{"name": "P1", "rec_a": ["ST"]}]}))
    monkeypatch.setattr(cfg, "SETTINGS_PATH", str(settings_file))

    s = a.load_settings()
    p = s["track_presets"][0]
    assert p["name"] == "P1"
    assert p["rec_a"] == ["ST"]
    assert p["export"] == []        # aus _PRESET_TEMPLATE aufgefüllt
    assert "mon_a" in p


# ── webtrigger_token_ok ───────────────────────────────────────────────────
def test_token_empty_means_open():
    assert a.webtrigger_token_ok("", "irgendwas") is True
    assert a.webtrigger_token_ok("", "") is True


def test_token_required_and_matching():
    assert a.webtrigger_token_ok("secret", "secret") is True


@pytest.mark.parametrize("supplied", ["", "wrong", "secre", "Secret"])
def test_token_mismatch_rejected(supplied):
    assert a.webtrigger_token_ok("secret", supplied) is False


# ── _pt_pid Cache-Validierung ─────────────────────────────────────────────
def test_pt_pid_returns_live_cached_pid(monkeypatch):
    import os
    monkeypatch.setattr(a, "_cached_pid", os.getpid())
    assert a._pt_pid() == os.getpid()


def test_pt_pid_invalidates_dead_cached_pid(monkeypatch):
    # Tote PID im Cache + pgrep liefert nichts → None und Cache geleert
    monkeypatch.setattr(a, "_cached_pid", 2_000_000_000)

    class _Empty:
        stdout = ""
    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: _Empty())
    assert a._pt_pid() is None
    assert a._cached_pid is None


# ── _app_pid Cache-Validierung ────────────────────────────────────────────
def test_app_pid_invalidates_dead_entry(monkeypatch):
    a._app_pid_cache["GhostApp"] = 2_000_000_000

    class _Empty:
        stdout = ""
    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: _Empty())
    # AppKit-Pfad findet nichts (Name existiert nicht) → pgrep leer → None
    assert a._app_pid("GhostApp") is None
    assert "GhostApp" not in a._app_pid_cache


# ── gRPC-Deadline-Wrapper ─────────────────────────────────────────────────
class _FakeRaw:
    def __init__(self):
        self.captured = {}

    def SendGrpcRequest(self, request, *args, **kwargs):
        self.captured = dict(kwargs)
        return "resp"


class _FakeEngine:
    def __init__(self):
        self.client = type("C", (), {})()
        self.client.raw_client = _FakeRaw()


def test_install_grpc_deadline_injects_timeout():
    eng = _FakeEngine()
    a._install_grpc_deadline(eng)
    a._grpc_deadline_tls.value = 7.5
    try:
        eng.client.raw_client.SendGrpcRequest("req")
    finally:
        a._grpc_deadline_tls.value = a._GRPC_CALL_DEADLINE
    assert eng.client.raw_client.captured.get("timeout") == 7.5


def test_install_grpc_deadline_idempotent():
    eng = _FakeEngine()
    a._install_grpc_deadline(eng)
    wrapped_once = eng.client.raw_client.SendGrpcRequest
    a._install_grpc_deadline(eng)
    assert eng.client.raw_client.SendGrpcRequest is wrapped_once


# ── _ptsl_call Verhalten ──────────────────────────────────────────────────
def test_ptsl_call_happy_path_sets_deadline():
    seen = {}

    def fn(x):
        seen["deadline"] = a._current_grpc_deadline()
        return x * 2

    ok, res = a._ptsl_call(fn, 21, label="T", timeout=12.0)
    assert ok is True and res == 42
    assert seen["deadline"] == 12.0
    # Nach dem Call wieder auf Default zurückgesetzt
    assert a._current_grpc_deadline() == a._GRPC_CALL_DEADLINE


def test_ptsl_call_command_error_keeps_engine(monkeypatch):
    reset_called = {"n": 0}
    monkeypatch.setattr(a, "_reset_engine", lambda: reset_called.__setitem__("n", reset_called["n"] + 1))

    def fn():
        raise ValueError("fachlicher Fehler")

    ok, res = a._ptsl_call(fn, label="T")
    assert ok is False
    assert isinstance(res, ValueError)
    assert reset_called["n"] == 0          # KEIN Reconnect bei Nicht-RPC-Fehler


def test_ptsl_call_rpc_deadline_resets_engine(monkeypatch):
    reset_called = {"n": 0}
    monkeypatch.setattr(a, "_reset_engine", lambda: reset_called.__setitem__("n", reset_called["n"] + 1))

    class _Deadline(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.DEADLINE_EXCEEDED

    def fn():
        raise _Deadline()

    ok, res = a._ptsl_call(fn, label="T", timeout=5.0)
    assert ok is False and res is None     # Timeout → (False, None)
    assert reset_called["n"] == 1          # Verbindung verworfen
