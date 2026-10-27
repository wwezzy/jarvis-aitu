"""No microphone, speakers, downloads or OS actions are used by these tests."""
import hashlib
import io
import stat
import types
import zipfile

import pytest

from desktop import model_setup
from desktop.voice import VoiceService, WakeGate


def model_files(path):
    for name in model_setup.REQUIRED:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic model")


def archive_file(path, members=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in (members or [(f"{model_setup.MODEL_NAME}/{x}", b"synthetic")
                                        for x in model_setup.REQUIRED]):
            archive.writestr(name, value)
    return path


def test_wake_word_standalone_one_command_and_timeout():
    gate = WakeGate(timeout=10)
    assert gate.feed("открой калькулятор", 1).kind == "ignore"
    assert gate.feed("Джарвис!", 2).kind == "wake"
    assert gate.feed("Открой калькулятор", 3).text == "открой калькулятор"
    assert gate.feed("открой проводник", 4).kind == "ignore"
    gate.feed("джарвис", 10)
    assert gate.feed("выключи", 21).kind == "ignore"


@pytest.mark.parametrize("phrase,expected", [
    ("Джарвис, открой блокнот", "открой блокнот"),
    ("JARVIS open notepad", "open notepad"),
    ("джарвиз открой калькулятор", "открой калькулятор"),
])
def test_wake_word_with_command(phrase, expected):
    result = WakeGate().feed(phrase, 5)
    assert (result.kind, result.text) == ("command", expected)


@pytest.mark.parametrize("phrase", ["он сказал джарвис открой блокнот", "джарвист открой блокнот",
                                    "jarvisx open notepad", "привет", ""])
def test_wake_requires_first_whole_token(phrase):
    assert WakeGate().feed(phrase, 10).kind == "ignore"


def test_wake_cancel_and_extend_after_speech():
    gate = WakeGate()
    gate.feed("джарвис", 1)
    gate.extend(8)
    assert gate.feed("отмена", 15).kind == "cancel"
    assert gate.feed("открой блокнот", 16).kind == "ignore"
    gate.extend(20)
    assert gate.feed("открой блокнот", 21).kind == "ignore"


def test_voice_constructor_does_not_start_microphone_or_download(tmp_path, monkeypatch):
    from desktop import voice
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: pytest.fail("No imports until requested"))
    service = VoiceService(lambda x: None, lambda x: None, tmp_path / "missing")
    assert service._thread is None
    assert service._speaker is None
    service.stop()


def test_missing_voice_dependencies_leave_text_mode_usable(tmp_path, monkeypatch):
    from desktop import voice
    states = []
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda name: None)
    service = VoiceService(lambda x: pytest.fail("no command"), states.append, tmp_path / "missing")
    assert service.available()["reason"] == "dependencies_missing"
    assert service.start() is False
    assert "Текстовый ввод работает" in states[-1]


def test_no_microphone_reports_fixed_failure(tmp_path, monkeypatch):
    from desktop import voice
    model_files(tmp_path / "model")
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda name: object())
    def denied(**kwargs):
        raise RuntimeError("private device details")
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: types.SimpleNamespace(query_devices=denied))
    service = VoiceService(lambda x: None, lambda x: None, tmp_path / "model")
    assert service.available()["reason"] == "microphone_missing"
    assert "private device" not in str(service.available())


def test_recognition_and_audio_are_suppressed_during_tts(tmp_path, monkeypatch):
    from desktop import voice
    heard, spoken = [], []
    service = VoiceService(heard.append, lambda x: None, tmp_path)
    monkeypatch.setattr(service, "say", spoken.append)
    monkeypatch.setattr(voice.time, "monotonic", lambda: 100)
    service._recognized("джарвис")
    assert spoken == ["Да, сэр"]
    service._speaking.set()
    service._audio_callback(b"audio", 5, None, None)
    service._recognized("джарвис открой блокнот")
    assert service._audio.empty() and not heard
    service._speaking.clear()
    service._last_spoken = 99.8
    service._recognized("джарвис открой блокнот")
    assert not heard
    service._last_spoken = 99
    service._recognized("джарвис открой блокнот")
    assert heard == ["открой блокнот"]
    service.stop()
    service._recognized("джарвис открой проводник")
    assert len(heard) == 1


def test_audio_overflow_drops_stale_commands(tmp_path):
    service = VoiceService(lambda x: None, lambda x: None, tmp_path)
    service.gate.feed("джарвис", 100)
    for _ in range(25):
        service._audio_callback(b"audio", 5, None, None)
    assert service._audio.empty()
    assert service.gate.deadline == 0


def test_sapi_uses_plain_text_and_always_cleans_up_com(tmp_path, monkeypatch):
    from desktop import voice
    calls, states = [], []
    service = VoiceService(lambda x: None, states.append, tmp_path)
    class Speaker:
        def GetVoices(self):
            return []
        def Speak(self, text, flags):
            calls.append((text, flags, service._speaking.is_set()))
            raise RuntimeError("synthetic speaker failure")
    com = types.SimpleNamespace(CoInitialize=lambda: calls.append("init"),
                                CoUninitialize=lambda: calls.append("uninit"))
    client = types.SimpleNamespace(Dispatch=lambda name: Speaker())
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: com if name == "pythoncom" else client)
    service._speech.put("<speak>джарвис выключи ноут</speak>")
    service._speak_loop()
    assert calls == ["init", ("<speak>джарвис выключи ноут</speak>", 17, True), "uninit"]
    assert not service._speaking.is_set()
    assert "Ответ показан текстом" in states[-1]


def test_voice_reply_opt_out_also_mutes_wake_ack(tmp_path):
    service = VoiceService(lambda x: None, lambda x: None, tmp_path)
    service.speech_enabled = False
    service.say("Да, сэр")
    assert service._speech.empty()
    assert service._speaker is None


def test_disabling_speech_purges_playback_on_its_com_thread(tmp_path, monkeypatch):
    from desktop import voice
    calls = []
    service = VoiceService(lambda x: None, lambda x: None, tmp_path)
    class Speaker:
        def GetVoices(self):
            return []
        def Speak(self, text, flags):
            calls.append((text, flags))
        def WaitUntilDone(self, timeout):
            assert timeout == 100
            service.speech_enabled = False
            return False
    com = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
    client = types.SimpleNamespace(Dispatch=lambda name: Speaker())
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: com if name == "pythoncom" else client)
    # StopIteration ends this deterministic synthetic worker after one item.
    service._speech = types.SimpleNamespace(get=iter(["answer"]).__next__)
    service._speak_loop()
    assert calls == [("answer", 17), ("", 18)]
    assert not service._speaking.is_set()


def test_cached_model_installs_atomically_and_is_reused(tmp_path, monkeypatch):
    archive = archive_file(tmp_path / "model.zip")
    monkeypatch.setattr(model_setup, "MODEL_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    target, progress = tmp_path / "models" / model_setup.MODEL_NAME, []
    result = model_setup.install_model(target, progress.append, archive_path=archive)
    assert result == target and model_setup.model_ready(result)
    assert progress[-1] == "Локальная модель готова"
    assert not list(target.parent.glob(".jarvis-model-*"))
    assert model_setup.install_model(target, archive_path=tmp_path / "nonexistent.zip") == target


def test_checksum_failure_does_not_leave_a_partial_model(tmp_path):
    archive = archive_file(tmp_path / "model.zip")
    target = tmp_path / "models" / model_setup.MODEL_NAME
    with pytest.raises(ValueError, match="checksum"):
        model_setup.install_model(target, archive_path=archive)
    assert not target.exists()
    assert not list(target.parent.glob(".jarvis-model-*"))


def test_existing_incomplete_model_is_never_deleted(tmp_path):
    target = tmp_path / "model"
    target.mkdir()
    owned = target / "user-owned.txt"
    owned.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        model_setup.install_model(target)
    assert owned.read_text(encoding="utf-8") == "keep"


def test_existing_onecore_male_voice_selected_without_creating_tokens():
    from desktop.voice import select_windows_voice
    class Token:
        def __init__(self, gender):
            self.gender = gender
        def GetAttribute(self, key):
            return "419" if key == "Language" else self.gender
    female, male = Token("Female"), Token("Male")
    selections = []
    category = types.SimpleNamespace(SetId=lambda *args: selections.append(args),
                                     EnumerateTokens=lambda: [male])
    speaker = types.SimpleNamespace(GetVoices=lambda: [female], Voice=None)
    client = types.SimpleNamespace(Dispatch=lambda name: category)
    assert select_windows_voice(speaker, client)
    assert speaker.Voice is male
    assert selections == [(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)]


def test_rejected_onecore_engine_falls_back_to_existing_desktop_voice():
    from desktop.voice import select_windows_voice
    female = types.SimpleNamespace(GetAttribute=lambda key: "419" if key == "Language" else "Female")
    male = types.SimpleNamespace(GetAttribute=lambda key: "419" if key == "Language" else "Male")
    class Speaker:
        def GetVoices(self):
            return [female]
        @property
        def Voice(self):
            return self.selected
        @Voice.setter
        def Voice(self, token):
            if token is male:
                raise RuntimeError("unsupported engine")
            self.selected = token
    speaker = Speaker()
    category = types.SimpleNamespace(SetId=lambda *_args: None, EnumerateTokens=lambda: [male])
    assert select_windows_voice(speaker, types.SimpleNamespace(Dispatch=lambda _name: category))
    assert speaker.Voice is female


def test_voice_discovery_failures_preserve_system_default():
    from desktop.voice import select_windows_voice
    def unavailable(*_args):
        raise RuntimeError("not installed")
    speaker = types.SimpleNamespace(GetVoices=unavailable, Voice="existing default")
    assert not select_windows_voice(speaker, types.SimpleNamespace(Dispatch=unavailable))
    assert speaker.Voice == "existing default"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/windows/evil", "bad\\path",
                                f"{model_setup.MODEL_NAME}/../../escape",
                                f"{model_setup.MODEL_NAME}/CON", f"{model_setup.MODEL_NAME}/x. "])
def test_archive_rejects_traversal_and_windows_devices(tmp_path, name):
    archive = archive_file(tmp_path / "model.zip", [(name, b"bad")])
    target = tmp_path / "stage"
    target.mkdir()
    with pytest.raises(ValueError, match="Unsafe|escapes"):
        model_setup._extract(archive, target)
    assert not (tmp_path / "escape").exists()


def test_archive_rejects_symlinks_and_expansion_limits(tmp_path, monkeypatch):
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo(f"{model_setup.MODEL_NAME}/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr(info, "../../escape")
    with pytest.raises(ValueError, match="Unsafe"):
        model_setup._extract(archive, tmp_path / "stage")
    archive = archive_file(tmp_path / "large.zip")
    monkeypatch.setattr(model_setup, "MAX_UNPACKED", 1)
    with pytest.raises(ValueError, match="limits"):
        model_setup._extract(archive, tmp_path / "stage")


def test_download_rejects_large_truncated_and_redirected_responses(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        def __init__(self, data, length, url=model_setup.MODEL_URL):
            super().__init__(data)
            self.headers = {"Content-Length": str(length)}
            self.url = url
        def geturl(self):
            return self.url
    responses = [Response(b"x", model_setup.MAX_DOWNLOAD + 1), Response(b"x", 2),
                 Response(b"x", 1, "https://another.example/model.zip")]
    for i, response in enumerate(responses):
        monkeypatch.setattr(model_setup.urllib.request, "build_opener", lambda *a, r=response:
                            types.SimpleNamespace(open=lambda *a, **k: r))
        with pytest.raises(ValueError):
            model_setup._download(tmp_path / f"archive-{i}.zip", None)
    assert model_setup._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None
