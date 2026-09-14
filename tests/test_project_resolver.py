"""Tests for orchestrator.project_resolver (issue #15)."""
import json
import time

from orchestrator.project_resolver import ProjectContext, ProjectResolver, ResolveError, resolve

_SAMPLE_INDEX = {
    "canonical_ids": {
        "argos_hub": ["Hub", "Argos Hub", "hub central"],
        "wd_pdr": ["WD PDR", "PDR", "WD-PDR-Quote"],
        "masya_growth_agent": ["Masya Growth", "Masya Growth Agent", "growth agent"],
        "jarvis": ["Jarvis", "assistente de voz"],
    },
    "projects": {
        "argos_hub": {
            "root": "A:\\Argos-Hub",
            "repository_owner_repo": "luucasrod/argos-hub",
            "primary_context": "A:\\Argos-Hub\\AGENTS.md",
            "always_read": ["A:\\Argos-Hub\\AGENTS.md"],
            "commands": {"dev": "open index.html"},
            "default_branch_seen": "master",
        },
        "wd_pdr": {
            "root": "A:\\CLIENTES\\APPS-WEBSITE\\WD\\WD-PDR-Quote",
            "repository": "https://github.com/luucasrod/wd-pdr-quote.git",
            "task_source": "docs\\ai\\FILA.md",
        },
        "masya_growth_agent": {
            "root": "A:\\masya-growth-agent",
        },
        # 'jarvis' intentionally has NO 'projects' entry to test the
        # inconsistent-index case.
    },
}


def _write_index(tmp_path, data=None):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(data if data is not None else _SAMPLE_INDEX), encoding="utf-8")
    return index_path


def test_resolve_by_canonical_id(tmp_path):
    index_path = _write_index(tmp_path)
    result = ProjectResolver(str(index_path)).resolve("argos_hub")
    assert isinstance(result, ProjectContext)
    assert result.canonical_id == "argos_hub"
    assert result.root == "A:\\Argos-Hub"


def test_resolve_by_alias_case_insensitive(tmp_path):
    index_path = _write_index(tmp_path)
    for alias in ("Hub", "hub", "HUB", "hub central"):
        result = ProjectResolver(str(index_path)).resolve(alias)
        assert isinstance(result, ProjectContext), f"failed for alias {alias!r}"
        assert result.canonical_id == "argos_hub"


def test_resolve_multiple_known_aliases(tmp_path):
    index_path = _write_index(tmp_path)
    resolver = ProjectResolver(str(index_path))
    assert resolver.resolve("PDR").canonical_id == "wd_pdr"
    assert resolver.resolve("growth agent").canonical_id == "masya_growth_agent"
    assert resolver.resolve("Masya Growth").canonical_id == "masya_growth_agent"


def test_resolve_unknown_project_returns_error_with_suggestion(tmp_path):
    index_path = _write_index(tmp_path)
    result = ProjectResolver(str(index_path)).resolve("hu")
    assert isinstance(result, ResolveError)
    assert "nao encontrado" in result.reason
    assert result.suggestion in ("hub", "hub central") or result.suggestion is not None


def test_resolve_completely_unrelated_query_no_suggestion(tmp_path):
    index_path = _write_index(tmp_path)
    result = ProjectResolver(str(index_path)).resolve("xyz-nao-existe-em-lugar-nenhum")
    assert isinstance(result, ResolveError)
    assert result.suggestion is None


def test_convenience_resolve_function_returns_none_on_failure(tmp_path):
    index_path = _write_index(tmp_path)
    assert resolve("xyz-nao-existe", str(index_path)) is None
    assert resolve("Hub", str(index_path)) is not None


def test_missing_index_file_returns_structured_error(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    result = ProjectResolver(str(missing_path)).resolve("Hub")
    assert isinstance(result, ResolveError)
    assert "nao encontrado" in result.reason


def test_malformed_json_returns_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text("{ isto nao e json valido", encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("Hub")
    assert isinstance(result, ResolveError)
    assert "malformado" in result.reason


def test_missing_optional_fields_use_safe_defaults(tmp_path):
    index_path = _write_index(tmp_path)
    result = ProjectResolver(str(index_path)).resolve("PDR")
    assert isinstance(result, ProjectContext)
    # wd_pdr entry has no always_read/conditional_context/commands/warnings
    assert result.always_read == []
    assert result.conditional_context == []
    assert result.commands == {}
    assert result.warnings == []


def test_canonical_id_present_but_no_projects_entry_is_structured_error(tmp_path):
    index_path = _write_index(tmp_path)
    result = ProjectResolver(str(index_path)).resolve("Jarvis")
    assert isinstance(result, ResolveError)
    assert "inconsistente" in result.reason


def test_index_reload_on_mtime_change(tmp_path):
    index_path = _write_index(tmp_path)
    resolver = ProjectResolver(str(index_path))
    assert resolver.resolve("Hub").root == "A:\\Argos-Hub"

    updated = json.loads(json.dumps(_SAMPLE_INDEX))
    updated["projects"]["argos_hub"]["root"] = "A:\\Argos-Hub-Moved"
    time.sleep(0.05)
    index_path.write_text(json.dumps(updated), encoding="utf-8")

    assert resolver.resolve("Hub").root == "A:\\Argos-Hub-Moved"


# --- Regression tests from Codex's review (Review Task #60, PR #59) ---------
# 4 corrupt-index reproductions that used to crash with AttributeError/
# UnicodeDecodeError instead of returning a structured ResolveError.

def test_canonical_ids_as_list_instead_of_dict_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps([]), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("Hub")
    assert isinstance(result, ResolveError)


def test_aliases_as_string_instead_of_list_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps({"canonical_ids": ["cashy"]}), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("cashy")
    assert isinstance(result, ResolveError)


def test_project_entry_as_int_instead_of_dict_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(
        json.dumps({"canonical_ids": {"cashy": ["Cashy"]}, "projects": {"cashy": 42}}),
        encoding="utf-8",
    )
    result = ProjectResolver(str(index_path)).resolve("Cashy")
    assert isinstance(result, ResolveError)


def test_invalid_utf8_bytes_is_structured_error_not_unicode_decode_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_bytes(b"\xff\xfe\xfa")
    result = ProjectResolver(str(index_path)).resolve("Hub")
    assert isinstance(result, ResolveError)


def test_recovery_with_same_instance_after_index_is_fixed(tmp_path):
    """A corrupt index must not poison the cache - the SAME resolver
    instance must recover once the file is corrected, without needing a
    new ProjectResolver()."""
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps([]), encoding="utf-8")
    resolver = ProjectResolver(str(index_path))

    broken = resolver.resolve("Hub")
    assert isinstance(broken, ResolveError)

    time.sleep(0.05)
    index_path.write_text(json.dumps(_SAMPLE_INDEX), encoding="utf-8")

    fixed = resolver.resolve("Hub")
    assert isinstance(fixed, ProjectContext)
    assert fixed.canonical_id == "argos_hub"


# --- Regression tests from Codex's 2nd review pass on #60 (PR #71) --------
# _validate_structure checked top-level shape but not each project's
# container-typed fields, which _build_context wraps unconditionally in
# list()/dict() - a scalar there (e.g. "commands": 42) crashed with
# TypeError despite the earlier structural checks passing.

def _index_with_bad_project_field(field_name: str, bad_value) -> dict:
    return {
        "canonical_ids": {"cashy": ["Cashy"]},
        "projects": {"cashy": {field_name: bad_value}},
    }


def test_always_read_as_int_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(_index_with_bad_project_field("always_read", 42)), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("cashy")
    assert isinstance(result, ResolveError)


def test_conditional_context_as_int_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(_index_with_bad_project_field("conditional_context", 42)), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("cashy")
    assert isinstance(result, ResolveError)


def test_deploy_as_scalar_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(_index_with_bad_project_field("deploy", "vercel")), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("cashy")
    assert isinstance(result, ResolveError)


def test_structured_deploy_object_is_read_through(tmp_path):
    data = json.loads(json.dumps(_SAMPLE_INDEX))
    data["projects"]["argos_hub"]["deploy"] = {
        "provider": "Vercel", "production_url": "https://argos-hub.vercel.app",
        "trigger": "automatic on git push to master",
    }
    index_path = _write_index(tmp_path, data)
    result = ProjectResolver(str(index_path)).resolve("hub")
    assert isinstance(result, ProjectContext)
    assert result.deploy == {
        "provider": "Vercel", "production_url": "https://argos-hub.vercel.app",
        "trigger": "automatic on git push to master",
    }


def test_missing_deploy_object_defaults_to_empty_dict(tmp_path):
    index_path = _write_index(tmp_path)
    result = ProjectResolver(str(index_path)).resolve("hub")
    assert isinstance(result, ProjectContext)
    assert result.deploy == {}


def test_commands_as_int_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(_index_with_bad_project_field("commands", 42)), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("cashy")
    assert isinstance(result, ResolveError)


def test_warnings_as_int_is_structured_error(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(_index_with_bad_project_field("warnings", 42)), encoding="utf-8")
    result = ProjectResolver(str(index_path)).resolve("cashy")
    assert isinstance(result, ResolveError)


def test_recovery_after_bad_container_field_is_fixed(tmp_path):
    index_path = tmp_path / "project_context_index.json"
    index_path.write_text(json.dumps(_index_with_bad_project_field("commands", 42)), encoding="utf-8")
    resolver = ProjectResolver(str(index_path))

    assert isinstance(resolver.resolve("cashy"), ResolveError)

    time.sleep(0.05)
    index_path.write_text(
        json.dumps({"canonical_ids": {"cashy": ["Cashy"]}, "projects": {"cashy": {"commands": {"dev": "npm start"}}}}),
        encoding="utf-8",
    )
    fixed = resolver.resolve("cashy")
    assert isinstance(fixed, ProjectContext)
    assert fixed.commands == {"dev": "npm start"}
# --- resolve_from_text (added in #22) -------------------------------------

def test_resolve_from_text_finds_alias_in_sentence(tmp_path):
    index_path = _write_index(tmp_path)
    resolver = ProjectResolver(str(index_path))
    result = resolver.resolve_from_text("cria uma tela de X no Hub")
    assert isinstance(result, ProjectContext)
    assert result.canonical_id == "argos_hub"


def test_resolve_from_text_picks_longest_match(tmp_path):
    index_path = _write_index(tmp_path)
    resolver = ProjectResolver(str(index_path))
    result = resolver.resolve_from_text("melhora o hub central do Argos")
    assert result.canonical_id == "argos_hub"


def test_resolve_from_text_does_not_match_substring_inside_word(tmp_path):
    index_path = _write_index(tmp_path)
    resolver = ProjectResolver(str(index_path))
    # 'PDR' nao deve casar dentro de outra palavra que a contenha
    result = resolver.resolve_from_text("uma palavra qualquerPDRoutra sem espaco")
    assert isinstance(result, ResolveError)


def test_resolve_from_text_no_known_project_mentioned(tmp_path):
    index_path = _write_index(tmp_path)
    resolver = ProjectResolver(str(index_path))
    result = resolver.resolve_from_text("faz uma coisa generica sem projeto nenhum")
    assert isinstance(result, ResolveError)
