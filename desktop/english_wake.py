"""Optional English wake candidate; never authorizes or interprets commands.

The exact English model is installed only by an explicit action. Constructor
and model checks neither import Vosk nor download anything. A positive result
requires a separate transcription of the original audio with an exact leading
Jarvis before the voice controller may open its one-utterance wake policy.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import zipfile


MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
ARCHIVE_SIZE = 41205931
ARCHIVE_SHA256 = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
MAX_DOWNLOAD = 60 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024
MAX_RESULT_BYTES = 8192
MAX_UTTERANCE_SECONDS = 12
LICENSE_SOURCE = Path(__file__).resolve().parent / "licenses" / "Vosk-Apache-2.0.txt"
LICENSE_SIZE = 10173
LICENSE_SHA256 = "bb618af833c817ab01c1564bd3ec75ba4b4f9cf30910a4a2168e30a674ee8cc6"
MODEL_FILES = {
    "am/final.mdl": (15962575, "75370a0137f9daf8f469dedd7daa4513ae7a621f03240c6e512e2b50b656a7b6"),
    "graph/disambig_tid.int": (102, "9ad87cc166d0998f08f758f47a6223a120dfffcbee805c4849c3aa5e6bb3c0fc"),
    "graph/HCLr.fst": (22416994, "5caafba3081e1646545ac6bff0dd7a318e53dcbdc86f237909ce1d2ac1293d34"),
    "graph/Gr.fst": (24013795, "023c8b7e30704a9e37765c635c252e608a02f361235bf94abdcf2a5225d85b20"),
    "graph/phones/word_boundary.int": (1761, "da199d9c991e0e84681ddbb34627b915b26302d50a8fdaa23c51e2bc3a50b5c3"),
    "conf/model.conf": (290, "8f14cb1eeb07c762c371db648c6be688d347236155ca0f64fb13b6567a8ce81f"),
    "conf/mfcc.conf": (131, "1e2228006d01d805ad1c267fee9f79709ca87ac51bd82b0e3f5c69ba543f0fc4"),
    "ivector/splice.conf": (35, "9f0c5f7c82d18eaf25d8bce470efa9f7741f88411fe428774bc0a9bb69a24756"),
    "ivector/final.dubm": (168048, "8c5d7dd69d2122313baaf19f61f35dd3fa18b70c62ac0687e311e1c46e6daca7"),
    "ivector/global_cmvn.stats": (1080, "33be09afcc80059847a275c3d043b51f1ab954c7c2438ddbbf4745e8ba144ff9"),
    "ivector/final.ie": (8288887, "3f37faf90c375b9e4740b569398b5829ed9cc07d19be6d441f72c3b71d7efcc6"),
    "ivector/online_cmvn.conf": (95, "a2f3571754b64297cb7efb2e7ca3df61995c5a45fcbb97188f90613552bb2dfe"),
    "ivector/final.mat": (44975, "ddd83586dc5f928cda8738b922c85ffe38fc789cb5f9151a712ca12f37265382"),
    "README": (199, "c0cf286e4f7783306c5f6469b37db69228fb16803b03cae661edb2d7bba64ebb"),
}
MODEL_NOTICE = """Vosk small US English model 0.15, published by Alpha Cephei.
Official model: https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
Publisher model/license listing: https://alphacephei.com/vosk/models
Publisher-listed license: Apache License 2.0.
Full license preserved as LICENSE_APACHE_2.txt.
License text: https://www.apache.org/licenses/LICENSE-2.0
Archive/file SHA-256 values were observed over official verified HTTPS on
2026-10-28. These are reproducibility pins, not publisher signatures.
This optional model produces wake candidates only; it cannot authorize actions.
No audio, private state, recordings or application credentials are included.
"""


def default_model_dir():
    return Path.home() / "JarvisData" / "publicmodels" / MODEL_NAME


def _canonical(path):
    path = Path(path)
    raw = str(path)
    if (not path.is_absolute() or ".." in path.parts or raw.startswith(("\\\\", "//"))
            or "://" in raw or "\x00" in raw or path.resolve(strict=True) != path
            or path.is_symlink() or getattr(path, "is_junction", lambda: False)()):
        raise ValueError("Unsafe model path")
    return path


def _directories():
    return {str(parent).replace("\\", "/") for name in MODEL_FILES
            for parent in PurePosixPath(name).parents if str(parent) != "."}


def _known_hash(source, size):
    digest, total = hashlib.sha256(), 0
    while True:
        chunk = source.read(min(1024 * 1024, size + 1 - total))
        if not chunk:
            return digest.hexdigest() if total == size else None
        total += len(chunk)
        if total > size:
            return None
        digest.update(chunk)


def model_ready(path):
    """Hash all fourteen native inputs and reject unknown/redirected contents."""
    try:
        root = _canonical(path)
        if not root.is_dir():
            return False
        directories = _directories()
        for relative in ("", *directories):
            directory = _canonical(root / relative)
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = (directory / entry.name).relative_to(root).as_posix()
                    if entry.is_symlink() or name not in {*MODEL_FILES, *directories, "NOTICE.txt", "LICENSE_APACHE_2.txt"}:
                        return False
                    _canonical(directory / entry.name)
                    if name in directories and not entry.is_dir(follow_symlinks=False):
                        return False
        for relative, (size, expected) in MODEL_FILES.items():
            file = _canonical(root / relative)
            if not file.is_file() or file.stat().st_size != size:
                return False
            with file.open("rb") as source:
                before = os.fstat(source.fileno())
                digest = _known_hash(source, size)
                after = os.fstat(source.fileno())
            if (digest != expected or (before.st_size, before.st_mtime_ns, before.st_ino)
                    != (after.st_size, after.st_mtime_ns, after.st_ino)
                    or (after.st_size, after.st_mtime_ns, after.st_ino)
                    != (file.stat().st_size, file.stat().st_mtime_ns, file.stat().st_ino)):
                return False
        notice = root / "NOTICE.txt"
        if notice.exists() and (notice.stat().st_size != len(MODEL_NOTICE.encode()) or notice.read_bytes() != MODEL_NOTICE.encode()):
            return False
        license_file = root / "LICENSE_APACHE_2.txt"
        if license_file.exists() and (license_file.stat().st_size != LICENSE_SIZE or hashlib.sha256(license_file.read_bytes()).hexdigest() != LICENSE_SHA256):
            return False
        return True
    except (OSError, ValueError, TypeError):
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _progress(callback, message):
    try:
        if callable(callback):
            callback(message)
    except Exception:
        pass


def _download(path, callback):
    deadline, total, digest = time.monotonic() + 120, 0, hashlib.sha256()
    client = build_opener(ProxyHandler({}), _NoRedirect())
    with client.open(MODEL_URL, timeout=10) as response, path.open("xb") as output:
        if response.geturl() != MODEL_URL or response.status != 200:
            raise ValueError("Unexpected model download origin")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) != ARCHIVE_SIZE:
            raise ValueError("Model archive length changed")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("English model download deadline")
            # read1 performs at most one buffered socket read, so a server
            # dribbling bytes cannot hide the deadline inside read(size).
            stream = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(stream, "_sock", None)
            if sock is not None:
                sock.settimeout(min(10, remaining))
            chunk = response.read1(min(65536, min(MAX_DOWNLOAD, ARCHIVE_SIZE) + 1 - total))
            if time.monotonic() > deadline:
                raise TimeoutError("English model download deadline")
            if not chunk:
                break
            total += len(chunk)
            if total > min(MAX_DOWNLOAD, ARCHIVE_SIZE):
                raise ValueError("Model download exceeded its size")
            output.write(chunk)
            digest.update(chunk)
        if total != ARCHIVE_SIZE or digest.hexdigest() != ARCHIVE_SHA256:
            raise ValueError("English model archive validation failed")
    _progress(callback, "Английская модель загружена; проверяю содержимое…")


def _extract(archive_path, destination):
    directories = _directories()
    with zipfile.ZipFile(archive_path) as archive:
        members, seen, files = archive.infolist(), set(), set()
        if not 1 <= len(members) <= 200 or sum(member.file_size for member in members) > MAX_UNPACKED:
            raise ValueError("Model archive limits exceeded")
        for member in members:
            path = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (not path.parts or path.parts[0] != MODEL_NAME or path.is_absolute()
                    or any(part in {".", ".."} or ":" in part for part in path.parts)
                    or "\\" in member.filename or "\x00" in member.filename
                    or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFDIR, stat.S_IFREG}
                    or member.external_attr & 0x400 or member.flag_bits & 1
                    or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                raise ValueError("Unsafe English model archive member")
            key = path.as_posix().casefold()
            if key in seen:
                raise ValueError("Duplicate English model archive member")
            seen.add(key)
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if member.is_dir():
                if relative not in {".", *directories}:
                    raise ValueError("Unknown English model directory")
            elif relative not in MODEL_FILES or member.file_size != MODEL_FILES[relative][0]:
                raise ValueError("Unknown or resized English model asset")
            else:
                files.add(relative)
        if files != set(MODEL_FILES):
            raise ValueError("English model archive is incomplete")
        destination.mkdir()
        for member in members:
            path = PurePosixPath(member.filename)
            target = destination.joinpath(*path.parts[1:])
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            total, digest = 0, hashlib.sha256()
            with archive.open(member) as source, target.open("xb") as output:
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > member.file_size:
                        raise ValueError("Unpacked model asset exceeded its size")
                    digest.update(chunk)
                    output.write(chunk)
            if total != member.file_size or digest.hexdigest() != MODEL_FILES[PurePosixPath(*path.parts[1:]).as_posix()][1]:
                raise ValueError("English model file validation failed")


def install_model(target_dir=None, progress_callback=None):
    """Explicit download only; preserve any existing incomplete installation."""
    target = Path(target_dir) if target_dir is not None else default_model_dir()
    if model_ready(target):
        return target
    if target.exists() or target.is_symlink():
        raise FileExistsError("Existing English model directory is preserved")
    if not target.is_absolute() or ".." in target.parts or str(target).startswith(("\\\\", "//")):
        raise ValueError("Choose a local model directory")
    ancestor = target.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    _canonical(ancestor)
    target.parent.mkdir(parents=True, exist_ok=True)
    _canonical(target.parent)
    _progress(progress_callback, "Загружаю необязательную модель английского вызова…")
    with tempfile.TemporaryDirectory(prefix=".jarvis-en-wake-", dir=target.parent) as temporary:
        archive, stage = Path(temporary) / "model.zip", Path(temporary) / "verified-model"
        _download(archive, progress_callback)
        _extract(archive, stage)
        license_text = LICENSE_SOURCE.read_bytes()
        if len(license_text) != LICENSE_SIZE or hashlib.sha256(license_text).hexdigest() != LICENSE_SHA256:
            raise ValueError("Bundled Apache license validation failed")
        (stage / "LICENSE_APACHE_2.txt").write_bytes(license_text)
        (stage / "NOTICE.txt").write_bytes(MODEL_NOTICE.encode("utf-8"))
        if not model_ready(stage):
            raise ValueError("Extracted English model validation failed")
        if target.exists() or target.is_symlink():
            raise FileExistsError("A concurrent model directory is preserved")
        stage.rename(target)
    _progress(progress_callback, "Модель английского вызова установлена. Микрофон выключен.")
    return target


def _object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Repeated recognition field")
        data[key] = value
    return data


def _finite(value):
    return type(value) in {float, int} and math.isfinite(value)


class EnglishWakeDetector:
    """One bounded original utterance; positive output is a candidate only."""
    def __init__(self, model_dir=None):
        self.model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self.reason, self._model, self._vosk, self._sample_rate = "missing", None, None, None
        self.reset()

    def _recognizer(self):
        recognizer = self._vosk.KaldiRecognizer(self._model, self._sample_rate, '["jarvis","[unk]"]')
        recognizer.SetWords(True)
        return recognizer

    def prepare(self, sample_rate, *, vosk_module=None):
        self.close()
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 48000:
            return False
        if not model_ready(self.model_dir):
            try:
                present = self.model_dir.exists()
            except OSError:
                present = True
            self.reason = "unavailable" if present else "missing"
            return False
        try:
            sdk = vosk_module if vosk_module is not None else importlib.import_module("vosk")
            if callable(getattr(sdk, "SetLogLevel", None)):
                sdk.SetLogLevel(-1)
            model = sdk.Model(str(self.model_dir))
            lookup = getattr(model, "vosk_model_find_word", None) or getattr(model, "find_word", None)
            identifier = lookup("jarvis") if callable(lookup) else None
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("English wake vocabulary is unavailable")
            self._vosk, self._model, self._sample_rate = sdk, model, sample_rate
            self._recognition = self._recognizer()
            self.reason = "ready"
            return True
        except Exception:
            self.close()
            return False

    def reset(self):
        self._recognition, self._segments = None, []
        self._pcm_bytes = self._result_bytes = 0
        self._failed = self._finished = False
        if self._model is not None:
            self.reason = "ready"

    def close(self):
        self.reset()
        self._model = self._vosk = self._sample_rate = None
        self.reason = "unavailable"

    def _fail(self):
        self._failed, self.reason, self._recognition = True, "unavailable", None
        self._segments.clear()

    def _result(self, raw):
        if not isinstance(raw, str) or len(raw) > MAX_RESULT_BYTES:
            raise ValueError("Invalid English recognition result")
        self._result_bytes += len(raw.encode("utf-8"))
        if self._result_bytes > MAX_RESULT_BYTES or len(self._segments) >= 32:
            raise ValueError("English recognition result limit")
        data = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(data, dict) or set(data) - {"text", "result"} or not isinstance(data.get("text"), str):
            raise ValueError("Invalid English recognition fields")
        tokens, words = data["text"].split(), data.get("result", [])
        if not isinstance(words, list) or len(words) > 128 or len(tokens) != len(words):
            raise ValueError("Invalid English recognition words")
        previous = -1.0
        for token, word in zip(tokens, words):
            if (token not in {"jarvis", "[unk]"} or not isinstance(word, dict)
                    or set(word) != {"word", "conf", "start", "end"} or word["word"] != token
                    or not all(_finite(word[key]) for key in ("conf", "start", "end"))
                    or not 0 <= word["conf"] <= 1 or not 0 <= word["start"] < word["end"] <= MAX_UTTERANCE_SECONDS + 0.1
                    or word["start"] < previous):
                raise ValueError("Invalid English recognition metadata")
            previous = word["start"]
        if tokens:
            self._segments.append(data)

    def accept(self, pcm):
        if self._finished or self._failed or self._model is None:
            return
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            self._fail()
            return
        self._pcm_bytes += len(pcm)
        if self._pcm_bytes > self._sample_rate * MAX_UTTERANCE_SECONDS * 2:
            self._fail()
            return
        try:
            if self._recognition is None:
                self._recognition = self._recognizer()
            accepted = self._recognition.AcceptWaveform(pcm)
            if type(accepted) not in {bool, int} or accepted not in {False, True, 0, 1}:
                raise ValueError("Invalid English recognizer status")
            if accepted:
                self._result(self._recognition.Result())
        except Exception:
            self._fail()

    def finish(self):
        if self._finished or self._failed or self._recognition is None or not self._pcm_bytes:
            return False
        self._finished = True
        try:
            self._result(self._recognition.FinalResult())
            if not self._segments:
                return False
            first = self._segments[0]["result"][0]
            return first["word"] == "jarvis" and first["conf"] >= 0.95
        except Exception:
            self._fail()
            return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit optional English wake model installation; no microphone")
    parser.add_argument("--install-model", action="store_true")
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.install_model:
        parser.print_help()
        return 0
    install_model(args.model_dir)
    # Setup also runs under legacy Windows console code pages.
    print("English wake model installed. Microphone remains off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
