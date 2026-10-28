"""The personal welcome is a fixed media cue, never model or OS authority."""
import pytest

from desktop.core import DesktopCore


@pytest.fixture
def local(tmp_path, monkeypatch):
    actions, model_calls = [], []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Synthetic action")
    core.store.set_preferences({"local_chat_enabled": True})
    def no_model(*_args, **_kwargs):
        model_calls.append(True)
        raise AssertionError("Exact personal greeting must never call a model")
    monkeypatch.setattr("desktop.chat.LocalChat", no_model)
    yield core, actions, model_calls
    core.close()


@pytest.mark.parametrize("source", ["typed", "voice"])
@pytest.mark.parametrize("phrase", [
    "Jarvis, daddy’s home", "Jarvis daddys home", "JARVIS, DADDY'S HOME!", "daddy’s home", "daddys home",
    "Jarvis, wake up, daddy's home!", "wake up daddys home", "Джарвис дадис хоум", "Джарвис, дэддис хоум!",
    "дадис хоум", "дэддис хоум", "Jarvis wake up that is home", "wake up that is home",
])
def test_exact_original_welcome_variants_choose_only_the_same_recording(local, source, phrase):
    core, actions, model_calls = local
    reply = core.handle(phrase, source=source)
    assert reply.media_cue == "welcome_home" and reply.text == "Добро пожаловать домой, сэр."
    assert reply.speak is True and reply.action is None and reply.confirmation_id is None
    assert actions == model_calls == []


@pytest.mark.parametrize("phrase", [
    '"Jarvis daddy\'s home"', "«Джарвис дадис хоум»", "не дадис хоум", "не говори дэддис хоум",
    "don't daddy's home", "say daddy's home", "дэддис хоум и открой калькулятор",
    "daddys home and open calculator", "wake up that is home and shutdown", "Jarvis daddy's home please open browser",
    "он сказал Джарвис дадис хоум", "I think daddy is home", "my father is home", "папа дома",
    "wake up what is home", "that is home", "daddy is home", "not daddy home", "дадис хоум отмена",
])
def test_quotes_negation_reports_cancellation_and_extra_commands_never_choose_recording(local, phrase):
    core, actions, model_calls = local
    core.store.set_preferences({"local_chat_enabled": False})
    reply = core.handle(phrase)
    assert reply.media_cue is None and reply.action is None and reply.confirmation_id is None
    assert actions == model_calls == []


@pytest.mark.parametrize("source", ["model", "document", "import", "web", "telegram", "remote"])
def test_nonoriginal_sources_cannot_choose_personal_recording_or_modify_history(local, source):
    core, actions, model_calls = local
    reply = core.handle("Jarvis daddy’s home", source=source)
    assert reply.media_cue is None and actions == model_calls == []
    assert core.snapshot()["recent"] == []


def test_welcome_cue_does_not_bypass_enabled_owner_verification(local, monkeypatch):
    core, actions, model_calls = local
    core.owner.set_enabled(True)
    monkeypatch.setattr(core.owner, "allowed", lambda: False)
    reply = core.handle("Джарвис дадис хоум", source="voice")
    assert "Windows Hello" in reply.text and reply.media_cue is None
    assert actions == model_calls == [] and core.snapshot()["recent"] == []
