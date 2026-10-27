"""No microphone, speakers, downloads or OS actions are used by these tests."""
import hashlib
import io
import stat
import sys
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
    service.speech_engine = "windows"
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
    service.speech_engine = "windows"
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


def test_neural_preparation_is_silent_and_ack_never_uses_sapi(tmp_path, monkeypatch):
    from desktop import voice
    calls = []
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    neural = types.SimpleNamespace(
        prepare=lambda **kwargs: calls.append(("prepare", kwargs["rate"])) or True,
        synthesize=lambda text, **kwargs: (b"\x00\x00" * 100, 22050),
    )
    monkeypatch.setitem(sys.modules, "desktop.neural_speech", types.SimpleNamespace(NeuralSpeech=lambda: neural))
    monkeypatch.setattr(service, "_play_pcm", lambda pcm, rate: calls.append(("play", rate, service._speaking.is_set())))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: pytest.fail("Neural voice must not require COM"))
    service._speech.put(voice._PREPARE_SPEECH)
    service._speech.put("Да, сэр")
    service._speech.put(None)
    service._speak_loop()
    assert calls == [("prepare", 185), ("prepare", 185), ("play", 22050, True)]
    assert service.speech_backend == "neural" and not service._speaking.is_set()


def test_interrupt_during_neural_prepare_discards_old_utterance(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    def prepare(**_kwargs):
        service.interrupt()
        service._speech.put(None)
        return True
    neural = types.SimpleNamespace(prepare=prepare, synthesize=lambda *_a, **_k: pytest.fail("Cancelled speech"))
    monkeypatch.setitem(sys.modules, "desktop.neural_speech", types.SimpleNamespace(NeuralSpeech=lambda: neural))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: pytest.fail("No fallback for cancelled speech"))
    service._speech.put((0, "Old utterance"))
    service._speak_loop()
    assert not service._speaking.is_set()


def test_interrupt_during_synthesis_never_opens_audio_device(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    def synthesize(*_args, **_kwargs):
        service.interrupt()
        service._speech.put(None)
        return b"\x00\x00" * 100, 22050
    neural = types.SimpleNamespace(prepare=lambda **_k: True, synthesize=synthesize)
    monkeypatch.setitem(sys.modules, "desktop.neural_speech", types.SimpleNamespace(NeuralSpeech=lambda: neural))
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: pytest.fail("No output device after interrupt"))
    service._speech.put((0, "Old utterance"))
    service._speak_loop()
    assert not service._speaking.is_set()


def test_pcm_playback_stops_between_short_output_blocks(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    played = []
    class Output:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def write(self, chunk):
            played.append(len(chunk))
            service._halt_speech.set()
        def abort(self):
            played.append("abort")
    monkeypatch.setattr(voice.importlib, "import_module", lambda _: types.SimpleNamespace(RawOutputStream=lambda **_k: Output()))
    service._play_pcm(b"\x00\x00" * 5000, 22050)
    assert played == [2048, "abort"]


def test_speech_display_prefix_removed_and_queue_uses_generation(tmp_path, monkeypatch):
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    queued = []
    monkeypatch.setattr(service, "_enqueue_speech", queued.append)
    service.say("Локальная модель: **Начнём.**\n- Один шаг.")
    assert queued == ["Начнём. Один шаг."]


def test_superseded_plan_event_is_discarded_before_backend_preparation(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    current = [True]
    queued = []
    monkeypatch.setattr(service, "_enqueue_speech", queued.append)
    service.say_event("Старая тренировка в среду", is_current=lambda: current[0])
    assert len(queued) == 1
    current[0] = False
    monkeypatch.setattr(voice.importlib, "import_module", lambda *_: pytest.fail("Stale plan must not speak"))
    service._speech.put(queued[0])
    service._speech.put(None)
    service._speak_loop()
    assert service._last_spoken == 0


def test_plan_edit_during_pcm_stops_the_remaining_audio(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    current, writes = [True], []
    class Output:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def write(self, data):
            writes.append(len(data))
            current[0] = False
        def abort(self):
            writes.append("aborted")
    monkeypatch.setattr(voice.importlib, "import_module", lambda _: types.SimpleNamespace(RawOutputStream=lambda **_: Output()))
    service._play_pcm(b"\x00\x00" * 5000, 24000, is_current=lambda: current[0])
    assert writes == [2048, "aborted"]


def test_personal_eugene_selection_never_loads_piper_or_windows(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    service.apply_preferences({"voice_engine": "expressive", "voice_character": "eugene"})
    choices, played = [], []
    def create(**kwargs):
        choices.append(kwargs)
        return types.SimpleNamespace(prepare=lambda **_: True, synthesize=lambda *_a, **_k: (b"\x00\x00" * 100, 24000))
    monkeypatch.setitem(sys.modules, "desktop.expressive_speech", types.SimpleNamespace(ExpressiveSpeech=create))
    monkeypatch.setattr(service, "_play_pcm", lambda pcm, rate: played.append(rate))
    monkeypatch.setattr(voice.importlib, "import_module", lambda _: pytest.fail("No COM for ready personal voice"))
    service._speech.put("К вашим услугам, сэр.")
    service._speech.put(None)
    service._speak_loop()
    assert choices == [{"voice": "eugene", "personal_use": True}]
    assert played == [24000] and service.speech_backend == "expressive"


def test_idle_hours_do_not_overflow_the_next_bilingual_utterance():
    from desktop.voice import UtteranceBuffer
    audio = UtteranceBuffer(16000)
    for _ in range(72000):
        audio.append(b"\0\0" * 800, active=False)
    assert audio.pcm() is None and not audio.overflow
    audio.append(b"\x01\0" * 800, active=True)
    assert len(audio.pcm()) == 20 * 1600
    for _ in range(250):
        audio.append(b"\x01\0" * 800, active=True)
    assert audio.overflow and audio.pcm() is None
    audio.reset()
    assert not audio.overflow and audio.pcm() is None


def test_bilingual_decoder_only_runs_after_explicit_wake_and_never_for_fast_alias(tmp_path, monkeypatch):
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    service.multilingual_stt_enabled = True
    calls, heard = [], []
    service.on_text = heard.append
    service._stt = types.SimpleNamespace(transcribe=lambda pcm, rate: calls.append(rate) or "Jarvis, what is my plan for tomorrow?")
    monkeypatch.setattr(service, "say", lambda _: None)
    service._recognized("посторонний разговор", b"raw", 16000)
    assert calls == [] and heard == []
    service._recognized("джарвис открой калькулятор", b"raw", 16000)
    assert calls == [] and heard == ["открой калькулятор"]
    service._recognized("джарвис какой план", b"raw", 16000)
    assert calls == [16000] and heard[-1] == "what is my plan for tomorrow?"


def test_stop_during_multilingual_decode_discards_result_and_capture(tmp_path):
    heard = []
    service = VoiceService(heard.append, lambda _: None, tmp_path)
    service.multilingual_stt_enabled = True
    def decode(*_):
        service._audio_callback(b"must not queue during transcription", 1, None, None)
        service.stop()
        return "open calculator"
    service._stt = types.SimpleNamespace(transcribe=decode)
    service._recognized("джарвис какой план", b"raw", 16000)
    assert heard == [] and service._audio.empty() and not service._transcribing.is_set()


def test_exact_personal_ack_does_not_wait_for_dynamic_helper(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    service.speech_engine = "clone"
    calls, played = [], []
    clone = types.SimpleNamespace(is_ready=False,
        prepare_background=lambda **_: calls.append("background"),
        fixed_ack=lambda language: (b"\x00\x00" * 100, 24000),
        stream=lambda *_a, **_k: pytest.fail("Known acknowledgment must not need dynamic inference"),
        close=lambda: calls.append("closed"))
    monkeypatch.setitem(sys.modules, "desktop.clone_speech", types.SimpleNamespace(CloneSpeech=lambda **_: clone))
    monkeypatch.setattr(service, "_play_pcm", lambda pcm, rate, **_: played.append(rate))
    monkeypatch.setattr(voice.importlib, "import_module", lambda _: pytest.fail("No COM or real output"))
    service._speech.put(voice._PREPARE_SPEECH)
    service._speech.put("Да, сэр")
    service._speech.put(None)
    service._speak_loop()
    assert played == [24000] and service.speech_backend == "clone"
    assert calls == ["background", "background", "closed"]


def test_dynamic_audio_chunks_keep_one_output_stream_and_close_generator(tmp_path, monkeypatch):
    from desktop import voice
    service = VoiceService(lambda _: None, lambda _: None, tmp_path)
    calls = []
    class Output:
        def __enter__(self):
            calls.append("open")
            return self
        def __exit__(self, *_):
            calls.append("close")
        def write(self, data):
            calls.append(data)
        def abort(self):
            calls.append("abort")
    def chunks():
        try:
            yield b"\x01\0" * 50, 24000
            yield b"\x02\0" * 50, 24000
        finally:
            calls.append("generator_closed")
    monkeypatch.setattr(voice.importlib, "import_module", lambda _: types.SimpleNamespace(RawOutputStream=lambda **_: Output()))
    service._play_pcm_stream(chunks())
    assert calls == ["open", b"\x01\0" * 50, b"\x02\0" * 50, "generator_closed", "close"]


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
