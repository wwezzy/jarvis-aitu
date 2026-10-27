"""Private settings are literal, narrowly scoped, atomic and never logged."""
from desktop.runtime_env import load_phone_environment


def test_only_explicit_profile_env_is_loaded_without_overriding_process(tmp_path):
    (tmp_path / "phone.env").write_text('JARVIS_PHONE_ENABLED=0\nJARVIS_PHONE_TOKEN="local-test-value"\n', encoding="utf-8")
    target = {"JARVIS_PHONE_ENABLED": "1"}
    assert load_phone_environment(tmp_path, environ=target) == "loaded"
    assert target == {"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_TOKEN": "local-test-value"}


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
    from desktop.runtime_env import create_phone_environment
    assert create_phone_environment(tmp_path) == "created"
    env = {}
    assert load_phone_environment(tmp_path, environ=env) == "loaded"
    assert env["JARVIS_PHONE_ENABLED"] == env["DESKTOP_TELEGRAM_ENABLED"] == env["JARVIS_PHONE_CALLS_ENABLED"] == "0"
    assert len(env["JARVIS_PHONE_TOKEN"]) >= 32
    saved = (tmp_path / "phone.env").read_bytes()
    assert create_phone_environment(tmp_path) == "preserved"
    assert (tmp_path / "phone.env").read_bytes() == saved
