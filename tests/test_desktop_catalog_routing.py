"""Original requests resolve approved names before AI, without native launches.

These tests exercise the whole DesktopCore.handle boundary. The catalog doubles
contain only explicitly approved synthetic aliases; opening a known entry merely
records its ID. No folder discovery, executable, model or input device is used.
"""
from types import SimpleNamespace

import pytest

from desktop.core import DesktopCore


class ApprovedCatalog:
    def __init__(self, kind, records):
        self.kind, self.records = kind, records
        self.lookups, self.opened = [], []

    def resolve(self, name):
        self.lookups.append(name)
        key = name.strip().casefold().replace("ё", "е")
        matches = [item for item in self.records if key in {
            str(value).casefold().replace("ё", "е") for value in [item["name"], *item["aliases"]]}]
        status = "found" if len(matches) == 1 else "ambiguous" if matches else "missing"
        return {"status": status, self.kind: matches[0] if status == "found" else None,
                "suggestions": matches if status == "ambiguous" else []}

    def _open(self, name, *, authorization_guard=None):
        result = self.resolve(name)
        if result["status"] == "found" and (authorization_guard is None or authorization_guard() is True):
            self.opened.append(result[self.kind]["id"])
            return {**result, "status": "requested", "message": "Synthetic approved dispatch."}
        return {**result, "message": "Synthetic catalog clarification: " + result["status"]}

    def open_project(self, name, editor=None, *, authorization_guard=None):
        assert editor is None  # A chat cannot silently replace the owner's chosen editor.
        return self._open(name, authorization_guard=authorization_guard)

    def open_app(self, name, *, authorization_guard=None):
        return self._open(name, authorization_guard=authorization_guard)

    def projects(self):
        return list(self.records)

    def apps(self):
        return list(self.records)

    def _forbidden_mutation(self, *_args, **_kwargs):
        raise AssertionError("Chat text cannot approve, scan or register local capabilities")

    register_app = register_root = add_project = add_alias = scan = remove = _forbidden_mutation


@pytest.fixture
def routed(tmp_path, monkeypatch):
    actions, model_calls, model_answer = [], [], [None]
    core = DesktopCore(tmp_path / "synthetic-state", executor=lambda action: actions.append(action) or "Synthetic OS action.")
    core.store.set_preferences({"local_chat_enabled": True})
    projects = ApprovedCatalog("project", [
        {"id": 1, "name": "Alpha", "aliases": ["Альфа"], "editor": "pycharm", "available": True},
        {"id": 2, "name": "Research and Development", "aliases": ["Исследования и разработка"], "editor": "explorer", "available": True},
        {"id": 3, "name": "First shared", "aliases": ["Shared", "Общий"], "editor": "vscode", "available": True},
        {"id": 4, "name": "Second shared", "aliases": ["Shared", "Общий"], "editor": "idea", "available": True},
        {"id": 5, "name": "Jarvis", "aliases": ["Джарвис"], "editor": "pycharm", "available": True},
        {"id": 6, "name": "Open Source Research", "aliases": [], "editor": "explorer", "available": True},
        {"id": 7, "name": "Please Reply", "aliases": [], "editor": "explorer", "available": True},
    ])
    apps = ApprovedCatalog("app", [
        {"id": 10, "name": "Editor", "aliases": ["Редактор"], "state": "registered"},
        {"id": 11, "name": "First studio", "aliases": ["Studio", "Студия"], "state": "registered"},
        {"id": 12, "name": "Second studio", "aliases": ["Studio", "Студия"], "state": "registered"},
    ])
    core.projects, core.apps = projects, apps

    class Model:
        last_usage = None

        def __init__(self, _model):
            pass

        def answer(self, text, **kwargs):
            model_calls.append((text, kwargs))
            if model_answer[0] is None:
                raise AssertionError("A deterministic catalog command reached the model")
            return model_answer[0]

    monkeypatch.setattr("desktop.chat.LocalChat", Model)
    yield SimpleNamespace(core=core, projects=projects, apps=apps, actions=actions,
                          model_calls=model_calls, model_answer=model_answer)
    core.close()


def assert_no_dispatch(routed):
    assert routed.projects.opened == routed.apps.opened == routed.actions == []
    assert routed.model_calls == []


@pytest.mark.parametrize("source", ["typed", "voice"])
@pytest.mark.parametrize("command", [
    "открой проект Альфа", "Открыть проект Альфа!", "запусти проект Альфа",
    "Jarvis, open project Alpha.", "launch project Alpha", "Пожалуйста, открой проект Альфа.",
])
def test_exact_approved_project_opens_one_owner_selected_entry_before_model(routed, source, command):
    reply = routed.core.handle(command, source=source)
    assert routed.projects.opened == [1]
    assert routed.apps.opened == routed.actions == routed.model_calls == []
    assert reply.text == "Synthetic approved dispatch." and reply.confirmation_id is None


@pytest.mark.parametrize("command", [
    "Можешь открыть проект Альфа?", "Можешь ты, пожалуйста, открыть проект Альфа?",
    "Будь добр, открой проект Альфа.", "Can you open project Alpha, please?",
    "Could you open project Alpha?", "Would you launch project Alpha?",
])
def test_polite_frames_preserve_a_single_exact_project_request(routed, command):
    routed.core.handle(command, source="voice")
    assert routed.projects.opened == [1]
    assert routed.apps.opened == routed.actions == routed.model_calls == []


@pytest.mark.parametrize("source", ["typed", "voice"])
@pytest.mark.parametrize("command", [
    "открой приложение Редактор", "открыть приложение Редактор", "запусти Редактор",
    "включи Редактор", "open app Editor", "launch Editor", "start app Editor",
])
def test_exact_approved_app_opens_one_entry_without_extra_builtin_or_model_call(routed, source, command):
    routed.core.handle(command, source=source)
    assert routed.apps.opened == [10]
    assert routed.projects.opened == routed.actions == routed.model_calls == []


@pytest.mark.parametrize("command", [
    "Можешь открыть приложение Редактор?", "Будь добр, запусти Редактор.",
    "Can you open app Editor, please?", "Could you launch Editor?", "Would you start app Editor?",
])
def test_polite_frames_preserve_a_single_exact_app_request(routed, command):
    routed.core.handle(command, source="voice")
    assert routed.apps.opened == [10]
    assert routed.projects.opened == routed.actions == routed.model_calls == []


@pytest.mark.parametrize("command", ["open project Research and Development",
                                     "Jarvis, please open project Research and Development.",
                                     "открой проект Исследования и разработка"])
def test_conjunction_inside_exact_approved_alias_is_one_project_not_two_actions(routed, command):
    routed.core.handle(command)
    assert routed.projects.opened == [2]
    assert routed.apps.opened == routed.actions == routed.model_calls == []


@pytest.mark.parametrize("command,project_id", [("open project Jarvis", 5),
                                                ("Jarvis, open project Jarvis.", 5),
                                                ("открой проект Джарвис", 5),
                                                ("open project Open Source Research", 6),
                                                ("open project Please Reply", 7)])
def test_approved_project_name_is_not_stripped_as_address_politeness_or_extra_verb(routed, command, project_id):
    routed.core.handle(command)
    assert routed.projects.opened == [project_id]
    assert routed.apps.opened == routed.actions == routed.model_calls == []


@pytest.mark.parametrize("command", [
    "open project Missing", "открой проект Неизвестный", "open project Alph",
    "открой приложение Неизвестное", "open app Missing", "launch Missing", "start app Missing",
    "open project Shared", "открой проект Общий", "open app Studio", "включи Студия",
])
def test_missing_partial_or_ambiguous_aliases_clarify_without_guessing_or_model(routed, command):
    reply = routed.core.handle(command)
    assert reply.text and reply.confirmation_id is None
    assert_no_dispatch(routed)


@pytest.mark.parametrize("command", [
    "не открывай проект Альфа", "не надо открыть проект Альфа", "можешь не открывать проект Альфа?",
    "don't open project Alpha", "do not start app Editor", "please never launch project Alpha",
    "Could you not open project Alpha?", "Could you please not launch app Editor?",
    '"open project Alpha"', "«открой приложение Редактор»", "открой проект «Альфа»",
    "скажи 'open app Editor'", "в документе написано: открой проект Альфа",
    "The note says: “open app Editor”.", "He whispered ‘open project Alpha’.",
])
def test_negation_and_quoted_or_described_requests_neither_launch_nor_reach_model(routed, command):
    reply = routed.core.handle(command)
    assert reply.text and reply.confirmation_id is None
    assert_no_dispatch(routed)


@pytest.mark.parametrize("command", [
    "open project Alpha and open app Editor", "открой проект Альфа и запусти Редактор",
    "открой приложение Редактор и потом открой проект Альфа", "open app Editor; open project Alpha",
    "open project Alpha & open app Editor", "открой проект Альфа\nзатем выключи ноутбук",
    "Сначала открой проект Альфа потом запусти Редактор", "First open project Alpha, then start Editor",
    "Can you open project Alpha and then open app Editor?",
])
def test_compound_catalog_commands_require_separate_requests_without_model(routed, command):
    reply = routed.core.handle(command)
    assert reply.text and reply.confirmation_id is None
    assert_no_dispatch(routed)


@pytest.mark.parametrize("command", [
    r"open project C:\Unapproved\Project", r"open app C:\Unapproved\Editor.exe",
    "open project https://example.invalid/project", "open app ../Editor.exe",
    "open project Alpha --execute", "open app Editor --script", "open app powershell",
])
def test_chat_paths_urls_and_arguments_never_become_catalog_approval(routed, command):
    routed.core.handle(command)
    assert_no_dispatch(routed)


@pytest.mark.parametrize("source", ["model", "ai", "document", "import", "telegram", "web", "remote", "button", ""])
@pytest.mark.parametrize("command", ["open project Alpha", "open app Editor",
                                     r"register app C:\Unapproved\Editor.exe",
                                     r"add project C:\Unapproved\Project"])
def test_imports_and_model_sources_cannot_open_or_register_even_approved_names(routed, source, command):
    routed.core.handle(command, source=source)
    assert_no_dispatch(routed)
    assert routed.projects.lookups == routed.apps.lookups == []
    assert routed.core.snapshot()["recent"] == []


@pytest.mark.parametrize("command", ["open project Alpha", "open app Editor", "projects", "apps"])
def test_owner_verification_is_checked_before_catalog_read_or_dispatch(routed, command):
    routed.core.owner = SimpleNamespace(enabled=True, allowed=lambda: False, revoke=lambda: None)
    reply = routed.core.handle(command, source="voice")
    assert "Windows Hello" in reply.text
    assert_no_dispatch(routed)
    assert routed.projects.lookups == routed.apps.lookups == []


@pytest.mark.parametrize("command,kind", [("open project Alpha", "projects"), ("open app Editor", "apps")])
def test_owner_revocation_during_catalog_resolution_prevents_dispatch(routed, command, kind):
    allowed = [True]
    routed.core.owner = SimpleNamespace(enabled=True, allowed=lambda: allowed[0], revoke=lambda: None)
    catalog = getattr(routed, kind)
    resolve = catalog.resolve
    def revoke(name):
        result = resolve(name)
        allowed[0] = False
        return result
    catalog.resolve = revoke
    reply = routed.core.handle(command, source="voice")
    assert reply.text and not allowed[0]
    assert_no_dispatch(routed)


def test_stored_open_command_is_literal_note_data_never_a_new_request(routed):
    routed.core.handle("запомни open project Alpha and open app Editor")
    reply = routed.core.handle("заметки")
    assert "open project Alpha and open app Editor" in reply.text
    assert_no_dispatch(routed)


@pytest.mark.parametrize("model_text", ["open project Alpha", "open app Editor",
                                      '{"action":"open_project","name":"Alpha"}'])
def test_model_response_with_catalog_command_stays_display_only(routed, model_text):
    routed.model_answer[0] = model_text
    reply = routed.core.handle("Объясни принцип двоичного поиска")
    assert model_text in reply.text and reply.action is None and reply.confirmation_id is None
    assert len(routed.model_calls) == 1
    assert routed.projects.opened == routed.apps.opened == routed.actions == []
