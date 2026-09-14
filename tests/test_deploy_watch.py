"""Tests for orchestrator.deploy_watch (issue #38)."""
import json
import subprocess

import pytest
import requests

from orchestrator.deploy_watch import (
    DeployStrategy,
    get_deploy_strategy,
    report_smoke_check_failure,
    run_smoke_check,
)
from orchestrator.events import EventType, query_events
from orchestrator.github_client import GitHubClient
from orchestrator.models import TaskState
from orchestrator.persistence import Store
from orchestrator.project_resolver import ProjectContext


def _project(**overrides):
    defaults = dict(
        canonical_id="hub", root="/repo", repository="owner/repo",
        default_branch="main",
    )
    defaults.update(overrides)
    return ProjectContext(**defaults)


# --- get_deploy_strategy ----------------------------------------------------

def test_structured_deploy_with_production_url_wins_over_freeform_text():
    project = _project(
        commands={"deploy": "npm run deploy (scripts/deploy.mjs)"},
        deploy={"provider": "Vercel", "production_url": "https://hub.vercel.app",
               "trigger": "automatic on git push to master"},
    )
    strategy = get_deploy_strategy(project)
    assert strategy == DeployStrategy(
        kind="web", trigger="auto_on_push", provider="Vercel",
        production_url="https://hub.vercel.app", notes="automatic on git push to master",
    )


def test_freeform_vercel_auto_deploy_is_classified_web():
    project = _project(commands={"deploy": "automatic on every `git push` to master via Vercel"})
    strategy = get_deploy_strategy(project)
    assert strategy.kind == "web"
    assert strategy.trigger == "auto_on_push"
    assert strategy.provider == "Vercel"
    assert strategy.production_url is None  # no structured object - nothing to call


def test_not_applicable_deploy_text_is_kind_none():
    project = _project(commands={"deploy": "not applicable - runs locally on this machine only"})
    strategy = get_deploy_strategy(project)
    assert strategy.kind == "none"
    assert strategy.trigger == "none"


def test_unresolved_deploy_text_is_kind_none():
    project = _project(commands={"deploy": "UNRESOLVED - no deploy script found"})
    assert get_deploy_strategy(project).kind == "none"


def test_missing_deploy_key_entirely_is_kind_none():
    assert get_deploy_strategy(_project(commands={})).kind == "none"


def test_mobile_commands_classify_as_app_regardless_of_deploy_text():
    project = _project(commands={"android": "npm run android", "ios": "npm run ios",
                                  "deploy": "manual App Store submission"})
    strategy = get_deploy_strategy(project)
    assert strategy.kind == "app"


def test_manual_non_vercel_deploy_text_is_unknown_kind():
    project = _project(commands={"deploy": "run scripts/deploy.sh by hand"})
    strategy = get_deploy_strategy(project)
    assert strategy.kind == "unknown"
    assert strategy.trigger == "manual"


# --- run_smoke_check ---------------------------------------------------------

class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_smoke_check_success():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(200))
    assert result.ok is True
    assert result.emergency is False


def test_smoke_check_common_failure_does_not_escalate():
    # TEST PLAN: "smoke check falha comum -> Issue BUG_FOUND, sem escalar"
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(502))
    assert result.ok is False
    assert result.emergency is False


def test_smoke_check_unreachable_target_is_an_emergency():
    # TEST PLAN: "smoke check falha grave (producao fora do ar simulado) -> escala"
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")

    def get_fn(url, timeout):
        raise requests.exceptions.ConnectionError("connection refused")

    result = run_smoke_check(_project(), strategy, get_fn=get_fn)
    assert result.ok is False
    assert result.emergency is True


def test_smoke_check_api_kind_uses_same_http_path():
    strategy = DeployStrategy(kind="api", trigger="manual", production_url="https://api.example/health")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(200))
    assert result.ok is True
    assert result.kind == "api"


def test_smoke_check_app_confirms_build_command():
    project = _project(commands={"build": "npm run build:web"})
    strategy = DeployStrategy(kind="app", trigger="unknown")
    result = run_smoke_check(project, strategy)
    assert result.ok is True


def test_smoke_check_app_fails_when_build_command_unresolved():
    project = _project(commands={"build": "UNRESOLVED - no build script"})
    strategy = DeployStrategy(kind="app", trigger="unknown")
    result = run_smoke_check(project, strategy)
    assert result.ok is False
    assert result.emergency is False


def test_smoke_check_none_kind_is_a_trivial_success():
    strategy = DeployStrategy(kind="none", trigger="none")
    result = run_smoke_check(_project(), strategy)
    assert result.ok is True


def test_smoke_check_unknown_kind_never_manufactures_a_failure():
    strategy = DeployStrategy(kind="unknown", trigger="manual")
    result = run_smoke_check(_project(), strategy)
    assert result.ok is True


def test_smoke_check_web_without_production_url_is_a_no_op_success():
    strategy = DeployStrategy(kind="web", trigger="manual")
    result = run_smoke_check(_project(), strategy)
    assert result.ok is True


# --- report_smoke_check_failure ----------------------------------------------

class _FakeGitHub:
    def __init__(self):
        self.issues, self.posts = [], []

    def run(self, args, **kwargs):
        method = args[args.index('--method') + 1]
        if method == 'GET':
            return subprocess.CompletedProcess(args, 0, json.dumps([self.issues]), '')
        body = json.loads(kwargs['input'])
        self.posts.append(body)
        issue = {'number': len(self.posts), **body, 'state': 'open'}
        self.issues.append(issue)
        return subprocess.CompletedProcess(args, 0, json.dumps(issue), '')


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.db")
    yield instance
    instance.close()


def test_common_failure_creates_bug_found_task_and_issue_without_escalating(store):
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )

    task = report_smoke_check_failure(_project(), result, store, client=client)

    assert task.state == TaskState.BUG_FOUND
    assert task.origin == "post_deploy_check"
    assert len(github.posts) == 1
    assert query_events(store, event_types=[EventType.DECISION_REQUIRED]) == []


def test_emergency_failure_creates_needs_lucas_task_and_notifies(store, monkeypatch):
    sent = []
    monkeypatch.setattr("orchestrator.decisions.send_control_message", lambda message, **k: (sent.append(message), (True, None))[1])

    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    def get_fn(url, timeout):
        raise requests.exceptions.ConnectionError("connection refused")

    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=get_fn,
    )

    task = report_smoke_check_failure(_project(), result, store, client=client)

    assert task.state == TaskState.NEEDS_LUCAS
    assert len(github.posts) == 1
    assert len(sent) == 1  # Lucas was actually notified
    assert store.get_pending_decisions() != []


def test_repeated_identical_failure_does_not_duplicate_the_issue(store):
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )

    report_smoke_check_failure(_project(), result, store, client=client)
    report_smoke_check_failure(_project(), result, store, client=client)

    assert len(github.posts) == 1
