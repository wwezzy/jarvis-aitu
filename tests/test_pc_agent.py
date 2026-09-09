from services.pc_agent import build_signed_command, verify_signed_command


def test_signed_pc_command_roundtrip():
    secret = "a-very-long-test-secret"
    payload = build_signed_command("lock", 42, secret)
    assert verify_signed_command(payload, 42, secret) == "lock"


def test_signed_pc_command_rejects_wrong_secret():
    payload = build_signed_command("shutdown", 42, "correct-secret")
    assert verify_signed_command(payload, 42, "wrong-secret") is None
