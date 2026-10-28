"""Mock Tailscale/installer lifecycle; no VPN, install, login or Serve changes."""
import base64
import hashlib
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import remote_setup as setup


ORIGIN = "https://laptop.synthetic-tail.ts.net"


def served():
    return {"TCP": {"443": {"HTTPS": True}}, "Web": {ORIGIN[8:] + ":443": {"Handlers": {"/": {"Proxy": setup.TARGET}}}},
            "AllowFunnel": {ORIGIN[8:] + ":443": False}}


def result(value):
    return setup.CommandResult(0, json.dumps(value).encode())


@pytest.fixture
def tailscale(tmp_path, monkeypatch):
    program = tmp_path / "ProgramFiles"
    path = program / "Tailscale/tailscale.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"MZ" + b"synthetic-only-never-executed" * 16)
    state = SimpleNamespace(signature=True, state={"BackendState": "Running", "Self": {"DNSName": ORIGIN[8:] + "."},
        "AuthURL": "https://login.tailscale.com/a/private-auth-secret", "User": {"private": "owner@example.test"},
        "Peer": {"private-key": {"secret": "never expose"}}}, serve={}, serve_result=None)
    calls = []

    def runner(arguments, *, timeout, env, limit):
        calls.append((arguments, timeout, dict(env), limit))
        if Path(arguments[0]).name == "powershell.exe":
            assert base64.b64decode(arguments[-1]).decode("utf-16-le") == setup._SIGNATURE_SCRIPT
            target = Path(env["JARVIS_TAILSCALE_VERIFY_PATH"])
            return result({"protocol": 1, "valid": state.signature,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest() if state.signature else None})
        assert Path(arguments[0]) == path
        if arguments[1:] == ["status", "--json"]:
            return state.state if isinstance(state.state, setup.CommandResult) else result(state.state)
        if arguments[1:] == ["serve", "status", "--json"]:
            return state.serve if isinstance(state.serve, setup.CommandResult) else result(state.serve)
        assert arguments[1:] == ["serve", "--bg", "--https=443", "http://127.0.0.1:8767"]
        if state.serve_result is not None:
            return state.serve_result
        state.serve = served()
        return setup.CommandResult(0, b"Available within your tailnet")

    monkeypatch.setattr(setup, "_program_roots", lambda: [program])
    monkeypatch.setattr(setup, "_system_directory", lambda: tmp_path / "Windows/System32")
    instance = setup.RemoteSetup(tmp_path / "state", runner=runner)
    return instance, state, calls, path


def test_construction_and_installer_metadata_have_no_process_or_file_side_effects(tailscale):
    instance, _, calls, _ = tailscale
    assert not calls and not instance.state_dir.exists()
    assert instance.installer_info() == {"url": setup.INSTALLER_URL, "max_bytes": 100 * 1024 * 1024,
        "requires_user_installation": True, "requires_windows_and_iphone_login": True, "runs_automatically": False}
    assert not calls


def test_absent_tailscale_is_honestly_disconnected_and_does_not_search_path(tailscale, monkeypatch):
    instance, _, calls, _ = tailscale
    monkeypatch.setattr(setup, "_program_roots", lambda: [])
    status = instance.status()
    assert not status["installed"] and not status["logged_in"] and status["origin"] is None
    assert status["reason"] == "not_installed" and not status["phone_connection_verified"]
    assert instance.configure_serve(approved=True)["reason"] == "not_installed" and not calls


def test_valid_signature_and_cli_status_are_redacted_to_private_origin_only(tailscale):
    instance, _, calls, path = tailscale
    status = instance.status()
    assert status["installed"] and status["logged_in"] and status["origin"] == ORIGIN
    assert status["reason"] == "serve_required" and not status["serve_configured"]
    text = json.dumps(status)
    assert all(value not in text for value in ("owner@example", "private-key", "private-auth-secret", "never expose"))
    assert [call[0][1:] for call in calls if Path(call[0][0]) == path] == [["status", "--json"], ["serve", "status", "--json"]]
    probe = calls[0]
    assert "-NoProfile" in probe[0] and "-NonInteractive" in probe[0] and probe[1] == 8 and probe[3] == 8192
    assert probe[2]["JARVIS_TAILSCALE_VERIFY_PATH"] == str(path)


def test_unsigned_or_wrong_vendor_cli_is_never_run(tailscale):
    instance, state, calls, _ = tailscale
    state.signature = False
    assert instance.status()["reason"] == "untrusted_installation"
    assert len(calls) == 1 and Path(calls[0][0][0]).name == "powershell.exe"


def test_file_changed_after_vendor_probe_cannot_be_executed(tailscale):
    instance, _, calls, path = tailscale
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b"MZ" + b"changed" * 128)
    with pytest.raises(ValueError, match="binary_changed"):
        instance._command(path, digest, ["status", "--json"])
    assert not calls


@pytest.mark.parametrize("dns", ["https://laptop.demo.ts.net", "laptop.demo.ts.net.evil.example",
    "laptop.ts.net", "laptop.demo.ts.net/secret", "laptop..demo.ts.net", "laptop.demo.ts.net\n", "-laptop.demo.ts.net"])
def test_only_strict_tailnet_dns_names_are_used_as_https_origin(tailscale, dns):
    instance, state, _, _ = tailscale
    state.state["Self"]["DNSName"] = dns
    status = instance.status()
    assert status["origin"] is None and status["reason"] == "response_invalid"


@pytest.mark.parametrize("raw", [b"[]", b"null", b"invalid", b'{"BackendState":"Running","BackendState":"NeedsLogin"}',
    b'{"BackendState":NaN}', b"x" * (setup.MAX_OUTPUT + 1)],
    ids=["array", "null", "malformed", "duplicate", "nonfinite", "oversize"])
def test_invalid_oversized_duplicate_or_nonfinite_cli_json_is_refused(tailscale, raw):
    instance, state, _, _ = tailscale
    state.state = setup.CommandResult(0, raw)
    assert instance.status()["reason"] == "response_invalid"


def test_cli_timeout_and_login_needed_never_start_authentication_or_serve(tailscale):
    instance, state, calls, path = tailscale
    state.state = setup.CommandResult(-1, b"private details", timed_out=True)
    assert instance.status()["reason"] == "timeout"
    state.state = {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/private"}
    status = instance.configure_serve(approved=True)
    assert status["reason"] == "login_required" and "private" not in json.dumps(status)
    assert all(call[0][1:] == ["status", "--json"] for call in calls if Path(call[0][0]) == path)


def test_serve_requires_explicit_ui_approval_and_uses_only_fixed_private_target(tailscale):
    instance, _, calls, path = tailscale
    assert instance.configure_serve()["reason"] == "approval_required" and not calls
    assert instance.configure_serve(approved=1)["reason"] == "approval_required" and not calls
    status = instance.configure_serve(approved=True)
    assert status["serve_configured"] and status["reason"] == "ready" and not status["phone_connection_verified"]
    mutations = [call for call in calls if Path(call[0][0]) == path and "--bg" in call[0]]
    assert len(mutations) == 1
    assert mutations[0][0] == [str(path), "serve", "--bg", "--https=443", setup.TARGET]
    assert mutations[0][1] == 10
    assert all(value not in " ".join(call[0]) for call in calls for value in ("--yes", "funnel", "0.0.0.0", "--accept-routes", " up "))
    assert instance.configure_serve(approved=True)["reason"] == "ready"
    assert sum("--bg" in call[0] for call in calls) == 1


@pytest.mark.parametrize("conflict", [
    {"TCP": {"443": {"HTTPS": True}}, "Web": {ORIGIN[8:] + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}},
    {**served(), "AllowFunnel": {ORIGIN[8:] + ":443": True}},
    {**served(), "TCP": {"443": {"HTTPS": 1}}},
    {"Foreground": {"other-owned-session": served()}},
])
def test_existing_foreign_proxy_or_public_funnel_is_never_overwritten(tailscale, conflict):
    instance, state, calls, _ = tailscale
    state.serve = conflict
    assert instance.configure_serve(approved=True)["reason"] == "serve_conflict"
    assert not any("--bg" in call[0] for call in calls)


@pytest.mark.parametrize("value", [{"TCP": []}, {"Web": False}, {"AllowFunnel": 0}])
def test_malformed_serve_types_never_authorize_configuration(tailscale, value):
    instance, state, calls, _ = tailscale
    state.serve = value
    assert instance.configure_serve(approved=True)["reason"] == "response_invalid"
    assert not any("--bg" in call[0] for call in calls)


def test_https_consent_link_is_returned_only_for_exact_tailscale_login_host(tailscale):
    instance, state, _, _ = tailscale
    consent = "https://login.tailscale.com/f/serve?node=synthetic"
    state.serve_result = setup.CommandResult(-1, ("Enable HTTPS at " + consent).encode(), timed_out=True)
    status = instance.configure_serve(approved=True)
    assert status["reason"] == "needs_user_consent" and status["consent_url"] == consent
    for malicious in ("http://login.tailscale.com/f/serve", "https://login.tailscale.com.evil.example/f/serve",
                      "https://login.tailscale.com@evil.example/f/serve", "https://login.tailscale.com/f/serve#secret"):
        assert setup._consent_url(malicious.encode()) is None


class Download(io.BytesIO):
    status = 200

    def __init__(self, data, *, url=setup.INSTALLER_URL, length=None):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def geturl(self):
        return self.url


def install_download(monkeypatch, response):
    calls = []

    def open_request(request, timeout):
        calls.append((request.full_url, timeout))
        assert request.full_url == setup.INSTALLER_URL and timeout == 10
        return response

    monkeypatch.setattr(setup.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    return calls


def test_installer_is_atomic_vendor_verified_not_executed_and_rechecked_before_ui_launch(tailscale, monkeypatch):
    instance, _, calls, _ = tailscale
    data = b"MZ" + b"synthetic installer never executed" * 100
    downloads = install_download(monkeypatch, Download(data))
    result = instance.download_installer()
    assert result["ok"] and not result["launched"] and len(downloads) == 1
    target = Path(result["path"])
    assert target.read_bytes() == data and result["sha256"] == hashlib.sha256(data).hexdigest()
    assert instance.verified_installer() == {"path": str(target), "sha256": result["sha256"]}
    assert not list(target.parent.glob("*.pending.exe"))
    assert all(Path(call[0][0]).name == "powershell.exe" for call in calls)


@pytest.mark.parametrize("failure", ["unsigned", "too-large", "truncated", "foreign-url"])
def test_failed_download_preserves_existing_installer_and_removes_only_owned_partial(tailscale, monkeypatch, failure):
    instance, state, _, _ = tailscale
    directory = instance.state_dir / "installers"
    directory.mkdir(parents=True)
    target = directory / "tailscale-setup-latest.exe"
    target.write_bytes(b"previous approved installer")
    unowned = directory / "tailscale-other.pending.exe"
    unowned.write_bytes(b"other operation")
    data = b"MZ" + b"test" * 100
    response = Download(data)
    if failure == "unsigned":
        state.signature = False
    elif failure == "too-large":
        response.headers["Content-Length"] = str(setup.MAX_INSTALLER_BYTES + 1)
    elif failure == "truncated":
        response.headers["Content-Length"] = str(len(data) + 1)
    else:
        response.url = "https://evil.example/installer.exe"
    install_download(monkeypatch, response)
    assert not instance.download_installer()["ok"]
    assert target.read_bytes() == b"previous approved installer" and unowned.read_bytes() == b"other operation"
    assert list(directory.glob("*.pending.exe")) == [unowned]


def test_redirects_are_limited_to_official_https_stable_setup_downloads():
    assert setup._official_download(setup.INSTALLER_URL)
    assert setup._official_download("https://dl.tailscale.com/stable/tailscale-setup-1.104.4.exe")
    for url in ("http://pkgs.tailscale.com/stable/tailscale-setup-latest.exe",
                "https://pkgs.tailscale.com.evil.example/stable/tailscale-setup-latest.exe",
                "https://pkgs.tailscale.com/stable/../../tailscale-setup-latest.exe",
                "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe?secret=yes"):
        assert not setup._official_download(url)


def test_stream_without_size_header_still_cannot_exceed_download_cap(tailscale, monkeypatch):
    instance, _, _, _ = tailscale
    response = Download(b"MZ" + b"x" * 1024)
    response.headers = {}
    install_download(monkeypatch, response)
    monkeypatch.setattr(setup, "MAX_INSTALLER_BYTES", 256)
    assert not instance.download_installer()["ok"]
    assert not list((instance.state_dir / "installers").glob("*.exe"))


def test_signature_probe_cannot_authorize_bytes_with_a_different_hash(tailscale):
    instance, _, calls, path = tailscale
    original_runner = instance._runner

    def stale_signature(*args, **kwargs):
        response = original_runner(*args, **kwargs)
        if Path(args[0][0]).name == "powershell.exe":
            return result({"protocol": 1, "valid": True, "sha256": "0" * 64})
        return response

    instance._runner = stale_signature
    assert instance.status()["reason"] == "untrusted_installation"
    assert not any(Path(call[0][0]) == path for call in calls)


def test_bounded_process_reader_stops_oversized_output_without_unbounded_capture(monkeypatch):
    events = []

    class Process:
        stdout = io.BytesIO(b"x" * 8192)
        returncode = 0

        def wait(self, timeout):
            events.append(("wait", timeout))
            return self.returncode

        def terminate(self):
            events.append("terminate")

    def start(arguments, **kwargs):
        assert arguments == ["owned-test-cli", "status", "--json"]
        assert kwargs["shell"] is False and kwargs["stdin"] == setup.subprocess.DEVNULL
        assert kwargs["stderr"] == setup.subprocess.STDOUT
        return Process()

    monkeypatch.setattr(setup.subprocess, "Popen", start)
    response = setup._run_process(["owned-test-cli", "status", "--json"], timeout=5, env={}, limit=256)
    assert response.overflow and len(response.output) == 257 and "terminate" in events


def test_process_timeout_preserves_bounded_consent_output_and_stops_only_owned_child(monkeypatch):
    calls = []
    link = b"https://login.tailscale.com/f/serve?node=synthetic"

    class Process:
        stdout = io.BytesIO(link)
        returncode = None

        def wait(self, timeout):
            calls.append(("wait", timeout))
            if len(calls) == 1:
                raise setup.subprocess.TimeoutExpired("owned-test-cli", timeout)
            self.returncode = -15
            return -15

        def terminate(self):
            calls.append("terminate")

    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_a, **_kw: Process())
    response = setup._run_process(["owned-test-cli"], timeout=10, env={})
    assert response.timed_out and response.output == link and calls == [("wait", 10), "terminate", ("wait", 1)]
    assert setup._consent_url(response.output) == link.decode()


def test_serve_permission_rechecked_after_inspection_before_mutation(tailscale, monkeypatch):
    instance, _, calls, _ = tailscale
    allowed = [True]
    inspect = instance._inspect
    def revoke():
        result = inspect()
        allowed[0] = False
        return result
    monkeypatch.setattr(instance, "_inspect", revoke)
    assert instance.configure_serve(approved=True, approval_guard=lambda: allowed[0])["reason"] == "approval_required"
    assert not any("--bg" in call[0] for call in calls)


def test_serve_permission_rechecked_after_final_binary_hash(tailscale, monkeypatch):
    instance, _, calls, path = tailscale
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    allowed, original = [True], setup._digest
    def revoked(file):
        value = original(file)
        allowed[0] = False
        return value
    monkeypatch.setattr(setup, "_digest", revoked)
    with pytest.raises(PermissionError):
        instance._command(path, digest, ["serve", "--bg", "--https=443", setup.TARGET], approval_guard=lambda: allowed[0])
    assert calls == []


@pytest.mark.parametrize("guard", [lambda: False, lambda: 1, lambda: (_ for _ in ()).throw(OSError("private"))])
def test_invalid_or_expired_serve_permission_has_no_process_side_effects(tailscale, guard):
    instance, _, calls, _ = tailscale
    assert instance.configure_serve(approved=True, approval_guard=guard)["reason"] == "approval_required"
    assert calls == []


@pytest.mark.parametrize("when", ["before", "during_read", "during_signature"])
def test_download_cancel_preserves_previous_installer_and_owns_only_its_partial(tailscale, monkeypatch, when):
    instance, _, _, _ = tailscale
    directory = instance.state_dir / "installers"
    directory.mkdir(parents=True)
    target = directory / "tailscale-setup-latest.exe"
    target.write_bytes(b"previous approved installer")
    event = threading.Event()
    response = Download(b"MZ" + b"synthetic-only" * 64)
    if when == "before":
        event.set()
    elif when == "during_read":
        original_read = response.read1
        def read(size):
            value = original_read(size)
            event.set()
            return value
        response.read1 = read
    else:
        original = instance._signature
        def signature(path):
            value = original(path)
            event.set()
            return value
        monkeypatch.setattr(instance, "_signature", signature)
    downloads = install_download(monkeypatch, response)
    result = instance.download_installer(cancel_event=event)
    assert not result["ok"] and not result["launched"]
    assert target.read_bytes() == b"previous approved installer" and not list(directory.glob("*.pending.exe"))
    assert bool(downloads) is (when != "before")


def test_download_eof_after_absolute_deadline_is_not_accepted(tailscale, monkeypatch):
    instance, _, _, _ = tailscale
    response = Download(b"MZ" + b"synthetic-only" * 64)
    clock = [0.0]
    original = response.read1
    def read(size):
        result = original(size)
        clock[0] = 179.0 if result else 181.0
        return result
    response.read1 = read
    response.read = lambda *_: pytest.fail("Blocking read fallback used")
    monkeypatch.setattr(setup.time, "monotonic", lambda: clock[0])
    install_download(monkeypatch, response)
    assert not instance.download_installer()["ok"]
    assert not list((instance.state_dir / "installers").glob("*.exe"))


def test_installer_download_explicitly_disables_ambient_proxies(tailscale, monkeypatch):
    instance, _, _, _ = tailscale
    response = Download(b"MZ" + b"synthetic-only" * 64)
    handlers_seen = []
    def build(*handlers):
        handlers_seen.extend(handlers)
        return SimpleNamespace(open=lambda *_a, **_kw: response)
    monkeypatch.setattr(setup.urllib.request, "build_opener", build)
    assert instance.download_installer()["ok"]
    assert isinstance(handlers_seen[0], setup.urllib.request.ProxyHandler) and handlers_seen[0].proxies == {}
    assert isinstance(handlers_seen[1], setup._OfficialRedirect)


def test_foreign_route_created_after_initial_inspection_is_not_overwritten(tailscale):
    instance, state, calls, path = tailscale
    original = instance._runner
    inspections = []
    foreign = {"TCP": {"443": {"HTTPS": True}}, "Web": {
        ORIGIN[8:] + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}}
    def another_client(arguments, **kwargs):
        response = original(arguments, **kwargs)
        if Path(arguments[0]) == path and arguments[1:] == ["serve", "status", "--json"]:
            inspections.append(True)
            if len(inspections) == 1:
                state.serve = foreign
        return response
    instance._runner = another_client
    assert instance.configure_serve(approved=True)["reason"] == "serve_conflict"
    assert state.serve == foreign and not any("--bg" in call[0] for call in calls)
