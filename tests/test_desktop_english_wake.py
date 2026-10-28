"""Pinned optional installer and fake Vosk: no model, microphone or OS calls."""
import hashlib
import io
import json
import math
from pathlib import Path
import stat
from types import SimpleNamespace
import weakref
import zipfile

import pytest

from desktop import english_wake as module


def result(tokens=("jarvis",), *, confidence=1.0):
    words = [{"word": word, "conf": confidence, "start": index * 0.2, "end": index * 0.2 + 0.18}
             for index, word in enumerate(tokens)]
    return json.dumps({"text": " ".join(tokens), "result": words})


@pytest.fixture
def toy_model(tmp_path, monkeypatch):
    contents = {name: ("Synthetic test data " + name).encode() for name in module.MODEL_FILES}
    monkeypatch.setattr(module, "MODEL_FILES", {name: (len(raw), hashlib.sha256(raw).hexdigest()) for name, raw in contents.items()})
    root = tmp_path / "existing-model"
    for name, raw in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return root, contents


class FakeVosk:
    def __init__(self, *, plans=None, vocabulary=40825):
        self.plans, self.vocabulary = list(plans or []), vocabulary
        self.models, self.recognizers, self.log_levels = [], [], []

    def SetLogLevel(self, level):
        self.log_levels.append(level)

    def Model(self, path):
        self.models.append(path)
        sdk = self
        class Model:
            def vosk_model_find_word(self, word):
                assert word == "jarvis"
                return sdk.vocabulary
        return Model()

    def KaldiRecognizer(self, model, sample_rate, grammar):
        assert json.loads(grammar) == ["jarvis", "[unk]"]
        plan = self.plans.pop(0) if self.plans else {}
        class Recognizer:
            def __init__(self):
                self.words_enabled = False
                self.accepted = iter(plan.get("accepted", []))
                self.results = iter(plan.get("results", []))
                self.received = []

            def SetWords(self, enabled):
                self.words_enabled = enabled

            def AcceptWaveform(self, pcm):
                self.received.append(pcm)
                value = next(self.accepted, False)
                if isinstance(value, Exception):
                    raise value
                return value

            def Result(self):
                return next(self.results)

            def FinalResult(self):
                return plan.get("final", result())
        recognizer = Recognizer()
        self.recognizers.append(recognizer)
        return recognizer


def detector(toy_model, **sdk_options):
    sdk = FakeVosk(**sdk_options)
    detector = module.EnglishWakeDetector(toy_model[0])
    assert detector.prepare(16000, vosk_module=sdk)
    return detector, sdk


def test_public_model_pins_are_complete_and_match_observed_official_archive():
    assert module.ARCHIVE_SIZE == 41205931
    assert module.ARCHIVE_SHA256 == "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
    assert len(module.MODEL_FILES) == 14 and sum(size for size, _hash in module.MODEL_FILES.values()) == 70898967
    assert all(size > 0 and len(digest) == 64 for size, digest in module.MODEL_FILES.values())
    assert "not publisher signatures" in module.MODEL_NOTICE


def test_constructor_and_missing_prepare_never_import_sdk_or_download(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module.importlib, "import_module", lambda *_: pytest.fail("SDK imported automatically"))
    monkeypatch.setattr(module, "build_opener", lambda *_: pytest.fail("Network attempted automatically"))
    value = module.EnglishWakeDetector()
    assert value.model_dir == tmp_path / "JarvisData/publicmodels/vosk-model-small-en-us-0.15"
    assert value.finish() is False and not value.prepare(16000) and value.reason == "missing"
    assert not value.model_dir.exists()


def test_full_tree_hash_validation_rejects_modified_missing_unknown_and_notice(toy_model):
    root, _ = toy_model
    assert module.model_ready(root)
    unknown = root / "unverified-model.exe"
    unknown.write_bytes(b"never executed")
    assert not module.model_ready(root)
    unknown.unlink()
    (root / "NOTICE.txt").write_bytes(module.MODEL_NOTICE.encode("utf-8"))
    assert module.model_ready(root)
    (root / "NOTICE.txt").write_text("changed notice", encoding="utf-8")
    assert not module.model_ready(root)
    (root / "NOTICE.txt").unlink()
    asset = root / "am/final.mdl"
    original = asset.read_bytes()
    asset.write_bytes(b"X" * len(original))
    assert not module.model_ready(root)
    asset.unlink()
    assert not module.model_ready(root)


def test_redirected_asset_is_rejected_without_requiring_symlink_privilege(toy_model, monkeypatch):
    root, _ = toy_model
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw: root.parent / "outside" if path == root / "am/final.mdl" else original(path, *a, **kw))
    assert not module.model_ready(root)


def test_prepared_keyword_is_one_use_and_reset_preserves_model_without_reload(toy_model):
    value, sdk = detector(toy_model, plans=[{}, {"final": result(("[unk]",))}])
    assert sdk.recognizers[0].words_enabled and sdk.log_levels == [-1]
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is True and value.finish() is False
    value.reset()
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is False
    assert len(sdk.models) == 1 and len(sdk.recognizers) == 2
    value.close()
    assert value._model is None and value._recognition is None and value.finish() is False


def test_close_releases_recognizer_before_model_reference(toy_model):
    released, models = [], []
    class Model:
        def __init__(self, path):
            models.append(weakref.ref(self))

        def vosk_model_find_word(self, word):
            return 40825

        def __del__(self):
            released.append("model")

    class Recognizer:
        def __init__(self, model, rate, grammar):
            pass

        def SetWords(self, enabled):
            pass

        def __del__(self):
            released.append("recognizer-with-model" if models[0]() is not None else "recognizer-without-model")

    value = module.EnglishWakeDetector(toy_model[0])
    assert value.prepare(16000, vosk_module=SimpleNamespace(Model=Model, KaldiRecognizer=Recognizer))
    value.close()
    assert released == ["recognizer-with-model", "model"]


def test_unreadable_model_path_is_unavailable_without_exception(toy_model, monkeypatch):
    root = toy_model[0]
    monkeypatch.setattr(module, "model_ready", lambda _: False)
    original = Path.exists
    def denied(path):
        if path == root:
            raise PermissionError("private path")
        return original(path)
    monkeypatch.setattr(Path, "exists", denied)
    value = module.EnglishWakeDetector(root)
    assert not value.prepare(16000, vosk_module=FakeVosk()) and value.reason == "unavailable"


@pytest.mark.parametrize("tokens,confidence,expected", [(("jarvis",), 0.95, True), (("jarvis", "[unk]"), 1.0, True),
    (("[unk]", "jarvis"), 1.0, False), (("jarvis",), 0.94999, False), (("[unk]",), 1.0, False), ((), 1.0, False)])
def test_only_confident_final_first_jarvis_is_candidate(toy_model, tokens, confidence, expected):
    value, _ = detector(toy_model, plans=[{"final": result(tokens, confidence=confidence)}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is expected


def test_earlier_unknown_segment_cannot_be_erased_by_later_jarvis(toy_model):
    value, _ = detector(toy_model, plans=[{"accepted": [True], "results": [result(("[unk]",))], "final": result()}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is False


def test_initial_jarvis_segment_is_preserved_through_later_unknown(toy_model):
    value, _ = detector(toy_model, plans=[{"accepted": [True], "results": [result()], "final": result(("[unk]",))}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is True


@pytest.mark.parametrize("confidence", [True, -1, 1.1, math.nan, math.inf, "1.0", None])
def test_malformed_confidence_cannot_produce_candidate(toy_model, confidence):
    value, _ = detector(toy_model, plans=[{"final": result(confidence=confidence)}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is False and value.reason == "unavailable"


@pytest.mark.parametrize("changes", [{"word": "jarvis;run"}, {"word": "Jarvis"}, {"start": -0.1}, {"end": 13},
    {"start": True}, {"end": math.nan}, {"end": 0}, {"tool": "shutdown"}])
def test_invalid_word_or_timing_metadata_fail_closed(toy_model, changes):
    data = json.loads(result())
    data["result"][0].update(changes)
    value, _ = detector(toy_model, plans=[{"final": json.dumps(data)}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is False


@pytest.mark.parametrize("raw", ['{"text":"jarvis"}', '{"text":"jarvis","result":{}}',
    '{"text":"jarvis","text":"[unk]","result":[]}', '[]', '{"text":"jarvis","result":[],"tools":[]}',
    "x" * 8193, "[" * 1200 + "]" * 1200, result(("jarvis", "unsupported"))])
def test_malformed_oversized_or_unsupported_result_cannot_produce_candidate(toy_model, raw):
    value, _ = detector(toy_model, plans=[{"final": raw}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is False and value.reason == "unavailable"


@pytest.mark.parametrize("pcm", [None, "audio", bytearray(b"00"), b"", b"odd"])
def test_invalid_pcm_is_rejected_without_native_feed(toy_model, pcm):
    value, sdk = detector(toy_model)
    value.accept(pcm)
    assert value.finish() is False and sdk.recognizers[0].received == []


def test_utterance_pcm_and_cumulative_json_are_bounded(toy_model):
    value, _ = detector(toy_model)
    value.accept(b"\x00\x00" * (16000 * 12))
    value.accept(b"00")
    assert value.finish() is False
    payload = json.loads(result())
    payload["text"] = "jarvis" + " " * 4500
    raw = json.dumps(payload)
    value, _ = detector(toy_model, plans=[{"accepted": [True], "results": [raw], "final": raw}])
    value.accept(b"\x00\x00" * 16000)
    assert value.finish() is False


@pytest.mark.parametrize("vocabulary", [-1, 0, None, True, "40825"])
def test_missing_exact_vocabulary_word_makes_detector_unavailable(toy_model, vocabulary):
    value = module.EnglishWakeDetector(toy_model[0])
    assert not value.prepare(16000, vosk_module=FakeVosk(vocabulary=vocabulary))
    assert value.reason == "unavailable"


@pytest.mark.parametrize("rate", [True, None, 16000.0, 7999, 48001, "16000"])
def test_invalid_sample_rate_does_not_load_any_model(toy_model, rate):
    sdk, value = FakeVosk(), module.EnglishWakeDetector(toy_model[0])
    assert not value.prepare(rate, vosk_module=sdk) and sdk.models == []


def test_sdk_failure_has_fixed_reason_and_no_private_error_text(toy_model, capsys):
    value, _ = detector(toy_model, plans=[{"accepted": [OSError("private/path/transcript")]}])
    value.accept(b"\x00\x00" * 16000)
    assert value.reason == "unavailable" and value.finish() is False
    assert capsys.readouterr() == ("", "")


def archive_bytes(contents, *, extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in contents.items():
            archive.writestr(f"{module.MODEL_NAME}/{name}", raw)
        for name, raw in extra:
            archive.writestr(name, raw)
    return stream.getvalue()


class Response:
    status = 200
    fp = None

    def __init__(self, payload):
        self.payload, self.stream = payload, io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return module.MODEL_URL

    def read1(self, size):
        return self.stream.read(size)

    def read(self, *_):
        pytest.fail("Download used blocking read instead of deadline-aware read1")


def fake_download(monkeypatch, payload, *, response=None):
    calls, response = [], response or Response(payload)
    monkeypatch.setattr(module, "ARCHIVE_SIZE", len(payload))
    monkeypatch.setattr(module, "ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest())
    def build(*handlers):
        assert handlers[0].proxies == {} and isinstance(handlers[1], module._NoRedirect)
        class Client:
            def open(self, url, timeout):
                calls.append((url, timeout))
                return response
        return Client()
    monkeypatch.setattr(module, "build_opener", build)
    return calls


def test_explicit_installer_publishes_only_verified_tree_license_and_notice(toy_model, tmp_path, monkeypatch):
    _root, contents = toy_model
    calls = fake_download(monkeypatch, archive_bytes(contents))
    target, messages = tmp_path / "installed", []
    original = Path.rename
    def publish(stage, destination):
        assert not destination.exists() and module.model_ready(stage)
        return original(stage, destination)
    monkeypatch.setattr(Path, "rename", publish)
    assert module.install_model(target, messages.append) == target
    assert module.model_ready(target) and len(calls) == 1
    assert (target / "NOTICE.txt").read_text(encoding="utf-8") == module.MODEL_NOTICE
    assert (target / "LICENSE_APACHE_2.txt").read_bytes() == module.LICENSE_SOURCE.read_bytes()
    assert module.install_model(target) == target and len(calls) == 1
    assert "Микрофон выключен" in messages[-1]


def test_existing_incomplete_model_is_preserved_without_network(tmp_path, monkeypatch):
    target = tmp_path / "incomplete"
    target.mkdir()
    source = target / "precious.txt"
    source.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(module, "build_opener", lambda *_: pytest.fail("Unexpected download"))
    with pytest.raises(FileExistsError):
        module.install_model(target)
    assert source.read_text(encoding="utf-8") == "preserve"


def test_target_created_during_installation_is_preserved_without_publishing(toy_model, tmp_path, monkeypatch):
    fake_download(monkeypatch, archive_bytes(toy_model[1]))
    target = tmp_path / "concurrent"
    original = module._extract
    def another_install(archive, destination):
        original(archive, destination)
        target.mkdir()
        (target / "precious.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(module, "_extract", another_install)
    with pytest.raises(FileExistsError):
        module.install_model(target)
    assert (target / "precious.txt").read_text(encoding="utf-8") == "preserve"
    assert list(target.iterdir()) == [target / "precious.txt"]


@pytest.mark.parametrize("mutation", ["wrong_hash", "redirect", "wrong_length", "oversized", "dribble_timeout"])
def test_download_integrity_and_deadline_fail_without_publishing(toy_model, tmp_path, monkeypatch, mutation):
    payload = archive_bytes(toy_model[1])
    response = Response(payload)
    fake_download(monkeypatch, payload, response=response)
    if mutation == "wrong_hash":
        monkeypatch.setattr(module, "ARCHIVE_SHA256", "0" * 64)
    elif mutation == "redirect":
        response.geturl = lambda: "https://unrelated.example/model.zip"
    elif mutation == "wrong_length":
        response.headers["Content-Length"] = "1"
    elif mutation == "oversized":
        monkeypatch.setattr(module, "MAX_DOWNLOAD", 16)
    else:
        clock = [0.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        def dribble(size):
            clock[0] += 121
            return b"x"
        response.read1 = dribble
    target = tmp_path / mutation
    with pytest.raises((ValueError, TimeoutError)):
        module.install_model(target)
    assert not target.exists()


@pytest.mark.parametrize("extra", ["../escaped", "/absolute", f"{module.MODEL_NAME}/../escaped", f"{module.MODEL_NAME}/C:escape",
                                  f"{module.MODEL_NAME}/unknown.exe", f"{module.MODEL_NAME}\\evil"])
def test_archive_paths_unknown_files_and_traversal_are_rejected_before_extraction(toy_model, tmp_path, monkeypatch, extra):
    fake_download(monkeypatch, archive_bytes(toy_model[1], extra=[(extra, b"not executed")]))
    target = tmp_path / "unsafe"
    with pytest.raises(ValueError):
        module.install_model(target)
    assert not target.exists() and not (tmp_path / "escaped").exists()


def test_symlink_archive_member_is_rejected(toy_model, tmp_path, monkeypatch):
    info = zipfile.ZipInfo(f"{module.MODEL_NAME}/linked")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    fake_download(monkeypatch, archive_bytes(toy_model[1], extra=[(info, b"/outside")]))
    with pytest.raises(ValueError):
        module.install_model(tmp_path / "unsafe")


def test_archive_asset_hash_tamper_and_bomb_limits_are_rejected(toy_model, tmp_path, monkeypatch):
    contents = dict(toy_model[1])
    name = next(iter(contents))
    contents[name] = b"X" * len(contents[name])
    fake_download(monkeypatch, archive_bytes(contents))
    with pytest.raises(ValueError):
        module.install_model(tmp_path / "tampered")
    fake_download(monkeypatch, archive_bytes(toy_model[1]))
    monkeypatch.setattr(module, "MAX_UNPACKED", 1)
    with pytest.raises(ValueError):
        module.install_model(tmp_path / "bomb")


def test_cli_has_no_implicit_installer_or_sdk_load(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(module, "install_model", lambda path: calls.append(path))
    assert module.main([]) == 0 and calls == []
    assert "--install-model" in capsys.readouterr().out
    assert module.main(["--install-model", "--model-dir", str(tmp_path)]) == 0
    assert calls == [tmp_path]
