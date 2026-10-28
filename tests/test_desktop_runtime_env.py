"""Private settings are literal, narrowly scoped, atomic and never logged."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from desktop import runtime_env as module
from desktop.runtime_env import create_phone_environment, load_phone_environment, update_phone_environment


PHONE = "synthetic_phone_0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
REMOTE = "synthetic_control_9876543210_ZYXWVUTSRQPONMLKJIHGFEDCBA"
ORIGIN = "https://jarvis.test-tailnet.ts.net"


def loaded(root):
    target = {}
    assert load_phone_environment(root, environ=target) == "loaded"
    return target


def test_only_explicit_profile_env_is_loaded_without_overriding_process(tmp_path):
    (tmp_path / "phone.env").write_text(f'JARVIS_PHONE_ENABLED=0\nJARVIS_PHONE_TOKEN="{PHONE}"\n', encoding="utf-8")
    target = {"JARVIS_PHONE_ENABLED": "1"}
    assert load_phone_environment(tmp_path, environ=target) == "loaded"
    assert target == {"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_TOKEN": PHONE}


def test_unknown_or_duplicate_keys_refuse_entire_file(tmp_path):
    path = tmp_path / "phone.env"
    for text in ('JARVIS_PHONE_ENABLED=0\nDATABASE_URL=never-load\n',
                 'JARVIS_PHONE_ENABLED=0\nJARVIS_PHONE_ENABLED=1\n',
                 'JARVIS_PHONE_ENABLED=0\nTWILIO_AUTH_TOKEN="unfinished\n'):
        path.write_text(text, encoding="utf-8")
        target = {}
        assert load_phone_environment(tmp_path, environ=target) == "refused"
        assert target == {}


def test_values_do_not_execute_or_expand_and_unrelated_env_is_not_read(tmp_path):
    (tmp_path / ".env").write_text("BOT_TOKEN=unrelated-private-file", encoding="utf-8")
    assert load_phone_environment(tmp_path, environ={}) == "absent"
    (tmp_path / "phone.env").write_text("TWILIO_AUTH_TOKEN=$(do-not-run)\n", encoding="utf-8")
    target = {}
    assert load_phone_environment(tmp_path, environ=target) == "loaded"
    assert target["TWILIO_AUTH_TOKEN"] == "$(do-not-run)"


def test_oversized_or_remote_profile_is_refused(tmp_path):
    (tmp_path / "phone.env").write_bytes(b"X" * 16385)
    assert load_phone_environment(tmp_path, environ={}) == "refused"
    assert load_phone_environment("//untrusted/share", environ={}) == "refused"


def test_private_template_is_disabled_strong_and_never_rotates_existing_token(tmp_path):
    assert create_phone_environment(tmp_path) == "created"
    env = {}
    assert load_phone_environment(tmp_path, environ=env) == "loaded"
    assert env["JARVIS_PHONE_ENABLED"] == env["JARVIS_REMOTE_ENABLED"] == env["DESKTOP_TELEGRAM_ENABLED"] == env["JARVIS_PHONE_CALLS_ENABLED"] == "0"
    assert len(env["JARVIS_PHONE_TOKEN"]) >= 32
    assert len(env["JARVIS_REMOTE_TOKEN"]) >= 32 and env["JARVIS_PHONE_TOKEN"] != env["JARVIS_REMOTE_TOKEN"]
    saved = (tmp_path / "phone.env").read_bytes()
    assert create_phone_environment(tmp_path) == "preserved"
    assert (tmp_path / "phone.env").read_bytes() == saved


def test_legacy_migration_adds_only_missing_disabled_settings_and_keeps_comments_secrets(tmp_path):
    text = ("# Local operator comment\r\nJARVIS_PHONE_ENABLED=1\r\n"
            f'JARVIS_PHONE_TOKEN="{PHONE}"\r\nJARVIS_PHONE_ORIGIN=https://old.example\r\n'
            "DESKTOP_TELEGRAM_ENABLED=1\r\nDESKTOP_TELEGRAM_BOT_TOKEN=synthetic_old_value\r\n"
            "TWILIO_AUTH_TOKEN=$(literal-only)\r\nJARVIS_PHONE_CALLS_ENABLED=0\r\n")
    path = tmp_path / "phone.env"
    path.write_bytes(text.encode())
    assert create_phone_environment(tmp_path) == "migrated"
    assert path.read_bytes().startswith(text.encode())
    values = loaded(tmp_path)
    assert values["JARVIS_PHONE_TOKEN"] == PHONE and values["JARVIS_PHONE_ENABLED"] == "1"
    assert values["JARVIS_REMOTE_ENABLED"] == "0" and values["JARVIS_REMOTE_TOKEN"] != PHONE
    assert values["JARVIS_PHONE_ORIGIN"] == "https://old.example"
    assert values["DESKTOP_TELEGRAM_ENABLED"] == "1" and values["TWILIO_AUTH_TOKEN"] == "$(literal-only)"
    raw = path.read_bytes()
    assert create_phone_environment(tmp_path) == "preserved" and path.read_bytes() == raw


def test_explicit_origin_update_preserves_all_tokens_channels_and_process_settings(tmp_path, monkeypatch):
    path = tmp_path / "phone.env"
    path.write_text(f"JARVIS_PHONE_TOKEN={PHONE}\nJARVIS_REMOTE_TOKEN={REMOTE}\nDESKTOP_TELEGRAM_ENABLED=1\nTWILIO_TO=synthetic-recipient\n",
                    encoding="utf-8")
    monkeypatch.setenv("JARVIS_PHONE_ENABLED", "0")
    monkeypatch.setenv("JARVIS_PHONE_ORIGIN", "https://external.example")
    assert update_phone_environment(tmp_path, origin=ORIGIN, remote_enabled=True) == "updated"
    values = loaded(tmp_path)
    assert values["JARVIS_PHONE_ENABLED"] == values["JARVIS_REMOTE_ENABLED"] == "1"
    assert values["JARVIS_PHONE_ORIGIN"] == ORIGIN
    assert values["JARVIS_PHONE_TOKEN"] == PHONE and values["JARVIS_REMOTE_TOKEN"] == REMOTE
    assert values["DESKTOP_TELEGRAM_ENABLED"] == "1" and values["TWILIO_TO"] == "synthetic-recipient"
    assert os.environ["JARVIS_PHONE_ENABLED"] == "0" and os.environ["JARVIS_PHONE_ORIGIN"] == "https://external.example"
    target = {"JARVIS_PHONE_ENABLED": "0", "JARVIS_REMOTE_TOKEN": PHONE, "JARVIS_PHONE_ORIGIN": "https://external.example"}
    assert load_phone_environment(tmp_path, environ=target) == "loaded"
    assert target["JARVIS_REMOTE_TOKEN"] == PHONE and target["JARVIS_PHONE_TOKEN"] == PHONE  # Process configuration wins.
    assert target["JARVIS_PHONE_ENABLED"] == "0" and target["JARVIS_PHONE_ORIGIN"] == "https://external.example"
    before = path.read_bytes()
    assert update_phone_environment(tmp_path, origin=ORIGIN, remote_enabled=True) == "preserved"
    assert path.read_bytes() == before


@pytest.mark.parametrize("origin", ["http://host.ts.net", "https://public.example", "https://ts.net", "https://evilts.net",
                                    "https://host.ts.net/path", "https://host.ts.net/", "https://host.ts.net?token=private",
                                    "https://host.ts.net#fragment", "https://user@host.ts.net", "https://host.ts.net:443",
                                    "https://host.ts.net.evil.example", "https://host.ts.net\\suffix", "https://HOST.ts.net",
                                    "https://host%2ets.net", "https://-host.ts.net", "https://host..ts.net", "", None])
def test_configure_accepts_only_exact_private_https_origin_without_mutation(tmp_path, origin):
    assert create_phone_environment(tmp_path) == "created"
    before = (tmp_path / "phone.env").read_bytes()
    assert update_phone_environment(tmp_path, origin=origin, remote_enabled=True) == "refused"
    assert (tmp_path / "phone.env").read_bytes() == before


@pytest.mark.parametrize("options", [{"phone_enabled": 1}, {"remote_enabled": 1},
                                    {"phone_enabled": False, "remote_enabled": True}])
def test_boolean_flags_and_phone_dependency_are_strict(tmp_path, options):
    assert update_phone_environment(tmp_path, origin=ORIGIN, **options) == "refused"
    assert not (tmp_path / "phone.env").exists()


@pytest.mark.parametrize("bad", ["JARVIS_PHONE_ENABLED=yes\n", "JARVIS_REMOTE_ENABLED=true\n",
                                  "JARVIS_REMOTE_TOKEN=weak\n", "JARVIS_PHONE_TOKEN=" + "x" * 40 + "\n",
                                  f"JARVIS_PHONE_TOKEN={PHONE}\nJARVIS_REMOTE_TOKEN={PHONE}\n",
                                  "JARVIS_PHONE_ORIGIN=https://host.ts.net/private\n",
                                  "JARVIS_PHONE_TOKEN=\n", "JARVIS_PHONE_ENABLED=0\nunknown=1\n",
                                  "JARVIS_REMOTE_ENABLED=0\nJARVIS_REMOTE_ENABLED=1\n",
                                  "TWILIO_AUTH_TOKEN=value\x00private\n"])
def test_invalid_legacy_file_is_refused_whole_without_repair_rotation_or_partial_load(tmp_path, bad):
    path = tmp_path / "phone.env"
    path.write_bytes(bad.encode())
    before = path.read_bytes()
    target = {"existing": "preserved"}
    assert load_phone_environment(tmp_path, environ=target) == "refused" and target == {"existing": "preserved"}
    assert create_phone_environment(tmp_path) == "refused"
    assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused"
    assert path.read_bytes() == before


def test_existing_valid_remote_fields_are_not_disabled_or_rotated_by_migration(tmp_path):
    path = tmp_path / "phone.env"
    path.write_text(f"JARVIS_REMOTE_ENABLED=1\nJARVIS_REMOTE_TOKEN={REMOTE}\nJARVIS_PHONE_TOKEN={PHONE}\n", encoding="utf-8")
    assert create_phone_environment(tmp_path) == "migrated"
    values = loaded(tmp_path)
    assert values["JARVIS_REMOTE_ENABLED"] == "1" and values["JARVIS_REMOTE_TOKEN"] == REMOTE
    assert values["JARVIS_PHONE_ENABLED"] == "0" and values["JARVIS_PHONE_TOKEN"] == PHONE


def test_no_final_newline_and_utf8_bom_are_safe_legacy_literals(tmp_path):
    path = tmp_path / "phone.env"
    path.write_bytes(b"\xef\xbb\xbf" + f'JARVIS_PHONE_TOKEN="{PHONE}"'.encode())
    assert create_phone_environment(tmp_path) == "migrated"
    assert loaded(tmp_path)["JARVIS_PHONE_TOKEN"] == PHONE
    assert path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("separator", ["\x00", "\x0b", "\x0c", "\x85", "\u2028", "\u2029", "\r"])
def test_hidden_line_separators_cannot_inject_additional_flags(tmp_path, separator):
    path = tmp_path / "phone.env"
    before = ("# private comment" + separator + "JARVIS_REMOTE_ENABLED=1\n").encode()
    path.write_bytes(before)
    target = {}
    assert load_phone_environment(tmp_path, environ=target) == "refused" and target == {}
    assert create_phone_environment(tmp_path) == "refused"
    assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused" and path.read_bytes() == before


def test_non_utf8_settings_are_never_replaced_or_partially_loaded(tmp_path):
    path = tmp_path / "phone.env"
    path.write_bytes(b"JARVIS_PHONE_ENABLED=0\nTWILIO_AUTH_TOKEN=\xff\xfe\n")
    before, target = path.read_bytes(), {}
    assert load_phone_environment(tmp_path, environ=target) == "refused" and not target
    assert create_phone_environment(tmp_path) == "refused"
    assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused" and path.read_bytes() == before


def test_generated_control_token_cannot_equal_existing_phone_token(tmp_path, monkeypatch):
    (tmp_path / "phone.env").write_text(f"JARVIS_PHONE_TOKEN={PHONE}\n", encoding="utf-8")
    generated = iter([PHONE, REMOTE])
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: next(generated))
    assert create_phone_environment(tmp_path) == "migrated"
    assert loaded(tmp_path)["JARVIS_REMOTE_TOKEN"] == REMOTE


def test_broken_random_source_cannot_spin_forever_or_write_equal_tokens(tmp_path, monkeypatch):
    (tmp_path / "phone.env").write_text(f"JARVIS_PHONE_TOKEN={PHONE}\n", encoding="utf-8")
    before = (tmp_path / "phone.env").read_bytes()
    calls = []
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: calls.append(True) or PHONE)
    assert create_phone_environment(tmp_path) == "refused" and len(calls) == 8
    assert (tmp_path / "phone.env").read_bytes() == before


@pytest.mark.parametrize("guard", [False, lambda: False, lambda: 1])
def test_unverified_owner_cannot_write_private_enablement(tmp_path, guard):
    assert update_phone_environment(tmp_path, origin=ORIGIN, authorization_guard=guard) == "refused"
    assert not (tmp_path / "phone.env").exists()


def test_owner_revocation_during_durable_prepare_prevents_replace(tmp_path, monkeypatch):
    assert create_phone_environment(tmp_path) == "created"
    path, allowed = tmp_path / "phone.env", [True]
    before, fsync = path.read_bytes(), module.os.fsync
    def revoke(descriptor):
        fsync(descriptor)
        allowed[0] = False
    monkeypatch.setattr(module.os, "fsync", revoke)
    assert update_phone_environment(tmp_path, origin=ORIGIN, remote_enabled=True,
                                    authorization_guard=lambda: allowed[0]) == "refused"
    assert path.read_bytes() == before and not list(tmp_path.glob(".phone.env.*.tmp"))


def test_authorization_exception_is_fixed_refusal_without_file_or_secret_output(tmp_path, capsys):
    def fail():
        raise RuntimeError("synthetic private detail")
    assert update_phone_environment(tmp_path, origin=ORIGIN, authorization_guard=fail) == "refused"
    assert not (tmp_path / "phone.env").exists()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_failed_atomic_write_keeps_old_settings_and_removes_own_temporary_file(tmp_path, monkeypatch, operation):
    assert create_phone_environment(tmp_path) == "created"
    path = tmp_path / "phone.env"
    before = path.read_bytes()
    def fail(*_args):
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(module.os, operation, fail)
    assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused"
    assert path.read_bytes() == before and not list(tmp_path.glob(".phone.env.*.tmp"))


def test_noncooperating_external_edit_is_not_overwritten_by_older_transaction(tmp_path, monkeypatch):
    assert create_phone_environment(tmp_path) == "created"
    path, fsync = tmp_path / "phone.env", module.os.fsync
    external = path.read_bytes() + b"TWILIO_TO=external-operator-edit\n"
    def edit(descriptor):
        fsync(descriptor)
        path.write_bytes(external)
    monkeypatch.setattr(module.os, "fsync", edit)
    assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused"
    assert path.read_bytes() == external


def test_additive_migration_never_overflows_private_file_budget(tmp_path):
    path = tmp_path / "phone.env"
    before = ("#" + "x" * (module.MAX_ENV_BYTES - 2) + "\n").encode()
    path.write_bytes(before)
    assert create_phone_environment(tmp_path) == "refused" and path.read_bytes() == before


@pytest.mark.parametrize("raw", ["relative/profile", "//untrusted/share", r"\\server\share", "https://host/profile", "bad\x00profile"])
def test_no_helper_creates_or_loads_a_relative_remote_or_malformed_profile(raw):
    assert create_phone_environment(raw) == "refused"
    assert load_phone_environment(raw, environ={}) == "refused"
    assert update_phone_environment(raw, origin=ORIGIN) == "refused"


@pytest.mark.parametrize("filename", ["phone.env", "phone.env.lock"])
def test_hardlinked_settings_or_lock_never_mutate_the_other_file(tmp_path, filename):
    source = tmp_path / "operator-original"
    source.write_text(f"JARVIS_PHONE_TOKEN={PHONE}\n" if filename == "phone.env" else "0", encoding="utf-8")
    os.link(source, tmp_path / filename)
    before = source.read_bytes()
    assert create_phone_environment(tmp_path) == "refused"
    assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused"
    if filename == "phone.env":
        assert load_phone_environment(tmp_path, environ={}) == "refused"
    assert source.read_bytes() == before


def test_linked_profile_or_settings_never_escape_approved_directory(tmp_path):
    real, root = tmp_path / "real", tmp_path / "linked"
    real.mkdir()
    try:
        root.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation unavailable to this Windows user")
    assert create_phone_environment(root) == "refused"
    assert update_phone_environment(root, origin=ORIGIN) == "refused"
    assert load_phone_environment(root, environ={}) == "refused"
    target = tmp_path / "private-target"
    target.write_text(f"JARVIS_PHONE_TOKEN={PHONE}\n", encoding="utf-8")
    (real / "phone.env").symlink_to(target)
    before = target.read_bytes()
    assert create_phone_environment(real) == "refused" and load_phone_environment(real, environ={}) == "refused"
    assert update_phone_environment(real, origin=ORIGIN) == "refused" and target.read_bytes() == before


def test_parallel_migrations_generate_one_pair_without_lost_old_settings(tmp_path, monkeypatch):
    (tmp_path / "phone.env").write_text(f"JARVIS_PHONE_TOKEN={PHONE}\nTWILIO_TO=preserve-operator-value\n", encoding="utf-8")
    barrier = threading.Barrier(8)
    snapshots = threading.Barrier(8)
    seen, writes = set(), []
    observation_lock = threading.Lock()
    fstat, write = module.os.fstat, module.os.write
    def same_empty_snapshot(descriptor):
        info = fstat(descriptor)
        with observation_lock:
            first = threading.get_ident() not in seen
            seen.add(threading.get_ident())
        if first:
            # Every contender sees the new empty lock before any can acquire
            # it. That snapshot must not authorize an unlocked write later.
            assert info.st_size == 0
            snapshots.wait(timeout=5)
        return info
    def observe_write(descriptor, raw):
        with observation_lock:
            writes.append(raw)
        return write(descriptor, raw)
    monkeypatch.setattr(module.os, "fstat", same_empty_snapshot)
    monkeypatch.setattr(module.os, "write", observe_write)
    def migrate(_index):
        barrier.wait(timeout=5)
        return create_phone_environment(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = list(workers.map(migrate, range(8)))
    monkeypatch.setattr(module.os, "fstat", fstat)
    monkeypatch.setattr(module.os, "write", write)
    assert statuses.count("migrated") == 1 and statuses.count("preserved") == 7
    values = loaded(tmp_path)
    assert values["JARVIS_PHONE_TOKEN"] == PHONE and values["TWILIO_TO"] == "preserve-operator-value"
    assert values["JARVIS_REMOTE_ENABLED"] == "0" and values["JARVIS_REMOTE_TOKEN"] != PHONE
    assert writes == [b"0"]


def test_new_lock_byte_is_initialized_only_while_exclusively_locked(tmp_path, monkeypatch):
    owned, writes = set(), []
    if os.name == "nt":
        import msvcrt as locking_module
        function, acquire, release = "locking", locking_module.LK_NBLCK, locking_module.LK_UNLCK
    else:
        import fcntl as locking_module
        function, acquire, release = "flock", locking_module.LOCK_EX | locking_module.LOCK_NB, locking_module.LOCK_UN
    locking = getattr(locking_module, function)
    write = module.os.write
    def observe_lock(descriptor, mode, *arguments):
        result = locking(descriptor, mode, *arguments)
        if mode == acquire:
            owned.add(descriptor)
        elif mode == release:
            owned.remove(descriptor)
        return result
    def checked_write(descriptor, raw):
        assert descriptor in owned, "Lock-byte initialization preceded the exclusive OS lock"
        writes.append(raw)
        return write(descriptor, raw)
    monkeypatch.setattr(locking_module, function, observe_lock)
    monkeypatch.setattr(module.os, "write", checked_write)
    with module._writer_lock(tmp_path):
        assert len(owned) == 1
    assert not owned and writes == [b"0"]
    assert (tmp_path / "phone.env.lock").read_bytes() == b"0"


def test_concurrent_process_migrations_cannot_undo_explicit_enablement(tmp_path):
    (tmp_path / "phone.env").write_text(f"JARVIS_PHONE_TOKEN={PHONE}\nJARVIS_REMOTE_TOKEN={REMOTE}\nTWILIO_TO=keep-this\n", encoding="utf-8")
    script = ("import sys; from desktop.runtime_env import create_phone_environment, update_phone_environment; "
              "result = update_phone_environment(sys.argv[1], origin=sys.argv[2], remote_enabled=True) "
              "if sys.argv[3] == 'update' else create_phone_environment(sys.argv[1]); "
              "raise SystemExit(0 if result in {'updated','created','migrated','preserved'} else 2)")
    children = []
    try:
        for index in range(4):
            children.append(subprocess.Popen([sys.executable, "-c", script, str(tmp_path), ORIGIN,
                                               "update" if index == 0 else "migrate"],
                                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              cwd=Path(__file__).resolve().parents[1]))
        assert [child.wait(timeout=15) for child in children] == [0, 0, 0, 0]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    values = loaded(tmp_path)
    assert values["JARVIS_PHONE_ENABLED"] == values["JARVIS_REMOTE_ENABLED"] == "1"
    assert values["JARVIS_PHONE_ORIGIN"] == ORIGIN and values["TWILIO_TO"] == "keep-this"
    assert values["JARVIS_PHONE_TOKEN"] == PHONE and values["JARVIS_REMOTE_TOKEN"] == REMOTE


def test_busy_private_writer_refuses_boundedly_instead_of_overwriting(tmp_path, monkeypatch):
    assert create_phone_environment(tmp_path) == "created"
    before = (tmp_path / "phone.env").read_bytes()
    monkeypatch.setattr(module, "LOCK_TIMEOUT", 0.05)
    with module._writer_lock(tmp_path):
        assert update_phone_environment(tmp_path, origin=ORIGIN) == "refused"
    assert (tmp_path / "phone.env").read_bytes() == before
