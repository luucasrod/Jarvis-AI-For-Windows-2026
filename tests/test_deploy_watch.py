"""Tests for orchestrator.deploy_watch (issue #38, round-1 fix)."""
import json
import subprocess

import pytest
import requests

from orchestrator.deploy_watch import (
    DeployStrategy,
    get_deploy_strategy,
    report_smoke_check_failure,
    resolve_episode,
    run_smoke_check,
)
from orchestrator.events import EventType, query_events
from orchestrator.github_client import GitHubClient
from orchestrator.models import Task, TaskState
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


def test_structured_deploy_kind_override_reaches_api():
    project = _project(
        deploy={"provider": "Render", "production_url": "https://api.example/health",
               "trigger": "automatic", "kind": "api"},
    )
    assert get_deploy_strategy(project).kind == "api"


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


def test_auto_deploy_marker_without_trailing_space_is_recognized():
    project = _project(commands={"deploy": "auto-deploy via Vercel on push"})
    strategy = get_deploy_strategy(project)
    assert strategy.trigger == "auto_on_push"


# --- run_smoke_check ---------------------------------------------------------

class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_smoke_check_success():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(200))
    assert result.ok is True
    assert result.emergency is False
    assert result.verified is True


def test_smoke_check_common_failure_does_not_escalate():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(502))
    assert result.ok is False
    assert result.emergency is False


def test_smoke_check_404_status_is_a_failure():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(404))
    assert result.ok is False
    assert result.emergency is False
    assert "404" in result.observed


def test_smoke_check_custom_expected_status_predicate():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(
        _project(), strategy, get_fn=lambda url, timeout: _Response(404),
        expected_status=lambda code: code == 404,
    )
    assert result.ok is True


def test_smoke_check_connection_error_is_an_emergency_with_safe_observed_text():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")

    def get_fn(url, timeout):
        raise requests.exceptions.ConnectionError(
            "https://x.example/?token=SECRET failed: Authorization: Bearer SENTINEL_SECRET"
        )

    result = run_smoke_check(_project(), strategy, get_fn=get_fn)
    assert result.ok is False
    assert result.emergency is True
    assert "SENTINEL_SECRET" not in result.observed
    assert "SECRET" not in result.observed
    assert "ConnectionError" in result.observed


def test_smoke_check_timeout_is_an_emergency():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")

    def get_fn(url, timeout):
        raise requests.exceptions.Timeout("timed out")

    result = run_smoke_check(_project(), strategy, get_fn=get_fn)
    assert result.emergency is True


def test_smoke_check_non_connectivity_request_exception_is_not_an_emergency():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")

    def get_fn(url, timeout):
        raise requests.exceptions.InvalidURL("bad url: token=SECRET123")

    result = run_smoke_check(_project(), strategy, get_fn=get_fn)
    assert result.ok is False
    assert result.emergency is False
    assert "SECRET123" not in result.observed


def test_smoke_check_api_kind_uses_same_http_path():
    strategy = DeployStrategy(kind="api", trigger="manual", production_url="https://api.example/health")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(200))
    assert result.ok is True
    assert result.kind == "api"


def test_smoke_check_app_uses_injected_build_result_when_provided():
    project = _project(commands={"build": "npm run build:web"})
    strategy = DeployStrategy(kind="app", trigger="unknown")
    result = run_smoke_check(project, strategy, build_result=False)
    assert result.ok is False
    assert result.verified is True


def test_smoke_check_app_falls_back_to_weak_proxy_marked_unverified():
    project = _project(commands={"build": "npm run build:web"})
    strategy = DeployStrategy(kind="app", trigger="unknown")
    result = run_smoke_check(project, strategy)
    assert result.ok is True
    assert result.verified is False


def test_smoke_check_app_fails_when_build_command_unresolved():
    project = _project(commands={"build": "UNRESOLVED - no build script"})
    strategy = DeployStrategy(kind="app", trigger="unknown")
    result = run_smoke_check(project, strategy)
    assert result.ok is False
    assert result.emergency is False
    assert result.verified is False


def test_smoke_check_none_kind_is_a_trivial_unverified_success():
    strategy = DeployStrategy(kind="none", trigger="none")
    result = run_smoke_check(_project(), strategy)
    assert result.ok is True
    assert result.verified is False


def test_smoke_check_unknown_kind_never_manufactures_a_failure():
    strategy = DeployStrategy(kind="unknown", trigger="manual")
    result = run_smoke_check(_project(), strategy)
    assert result.ok is True
    assert result.verified is False


def test_smoke_check_web_without_production_url_is_a_no_op_success():
    strategy = DeployStrategy(kind="web", trigger="manual")
    result = run_smoke_check(_project(), strategy)
    assert result.ok is True
    assert result.verified is False


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

    report = report_smoke_check_failure(_project(), result, store, client=client)

    assert report.task.state == TaskState.BUG_FOUND
    assert report.task.origin == "post_deploy_check"
    assert report.is_new_episode is True
    assert report.issue_available is True
    assert len(github.posts) == 1
    assert query_events(store, event_types=[EventType.BUG_FOUND]) != []
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

    report = report_smoke_check_failure(_project(), result, store, client=client)

    assert report.task.state == TaskState.NEEDS_LUCAS
    assert len(github.posts) == 1
    assert len(sent) == 1  # Lucas was actually notified
    assert report.notified is True
    assert store.get_pending_decisions() != []


def test_repeated_identical_failure_reuses_the_same_task_and_does_not_duplicate_the_issue(store):
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )

    first = report_smoke_check_failure(_project(), result, store, client=client)
    second = report_smoke_check_failure(_project(), result, store, client=client)

    assert len(github.posts) == 1
    assert first.task.id == second.task.id
    assert first.episode_id == second.episode_id
    assert second.is_new_episode is False
    # Only one BUG_FOUND event - repeated calls for an open episode don't
    # re-count the same failure.
    assert len(query_events(store, event_types=[EventType.BUG_FOUND])) == 1


def test_resolved_episode_reopens_as_a_new_episode_with_a_new_task_and_issue(store):
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )

    first = report_smoke_check_failure(_project(), result, store, client=client)
    resolve_episode(store, _project(), result.kind, result.detail)
    second = report_smoke_check_failure(_project(), result, store, client=client)

    assert second.is_new_episode is True
    assert second.task.id != first.task.id
    assert second.episode_id != first.episode_id
    assert len(github.posts) == 2
    assert len(query_events(store, event_types=[EventType.BUG_FOUND])) == 2


def test_different_failures_get_different_episodes(store):
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")

    result_a = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(502))
    result_b = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(404))

    report_a = report_smoke_check_failure(_project(), result_a, store, client=client)
    report_b = report_smoke_check_failure(_project(), result_b, store, client=client)

    assert report_a.task.id != report_b.task.id
    assert len(github.posts) == 2


def test_no_client_and_no_repository_never_attempts_an_issue(store):
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )
    report = report_smoke_check_failure(_project(repository=None), result, store)
    assert report.issue_available is None


def test_client_defaults_to_a_real_github_client_when_repository_is_set(store, monkeypatch):
    created = {}

    def fake_init(self, store_arg, **kwargs):
        created["called"] = True
        self.store = store_arg

        def fake_create_issue(*a, **k):
            return {"available": True, "number": 1}
        self.create_issue = fake_create_issue

    monkeypatch.setattr(GitHubClient, "__init__", fake_init)
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )
    report = report_smoke_check_failure(_project(), result, store)
    assert created.get("called") is True
    assert report.issue_available is True


def test_bug_body_never_contains_raw_exception_text():
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")

    def get_fn(url, timeout):
        raise requests.exceptions.ConnectionError("secret=SENTINEL_LEAK in url")

    result = run_smoke_check(_project(), strategy, get_fn=get_fn)
    from orchestrator.deploy_watch import _format_bug_body
    body = _format_bug_body(_project(), result, commit="abc123", logs=None)
    assert "SENTINEL_LEAK" not in body
    assert "abc123" in body


# --- round-2 fixes (Review Task #143, round-2 findings) ---------------------

def test_structured_trigger_canonical_auto_on_push_is_recognized():
    # Finding #5: the index may already store the canonical trigger value
    # itself ("auto_on_push"), not just a freeform sentence containing an
    # auto-deploy marker word.
    project = _project(deploy={"production_url": "https://x.example", "trigger": "auto_on_push"})
    assert get_deploy_strategy(project).trigger == "auto_on_push"


def test_production_url_credentials_and_query_never_reach_detail_or_evidence():
    # Finding #3: a leaked password/token in the URL must never reach any
    # persisted/published text - only scheme+host+path are safe.
    strategy = DeployStrategy(
        kind="web", trigger="auto_on_push",
        production_url="https://user:PASSWORD_SENTINEL@test.invalid/health?token=QUERY_SENTINEL",
    )
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(503))
    assert "PASSWORD_SENTINEL" not in result.detail
    assert "QUERY_SENTINEL" not in result.detail
    assert "test.invalid/health" in result.detail

    from orchestrator.deploy_watch import _format_bug_body
    body = _format_bug_body(_project(), result, commit=None, logs=None)
    assert "PASSWORD_SENTINEL" not in body
    assert "QUERY_SENTINEL" not in body


def test_bug_found_event_is_committed_atomically_with_the_task_reservation(store):
    # Finding #1: BUG_FOUND must land in the SAME transaction that creates
    # the Task/episode row - simulated here by making the GitHub call
    # (which happens AFTER _reserve_episode returns) blow up, and checking
    # the event still exists despite the caller never getting a result.
    class _ExplodingClient:
        def create_issue(self, *a, **k):
            raise RuntimeError("simulated crash during publish")

    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )
    with pytest.raises(RuntimeError):
        report_smoke_check_failure(_project(), result, store, client=_ExplodingClient())

    events = query_events(store, event_types=[EventType.BUG_FOUND])
    assert len(events) == 1


def test_repeated_calls_with_different_commit_and_logs_replay_the_same_issue_payload(store):
    # Finding #2: GitHubClient's own idempotency compares the FULL payload
    # (title+body+labels+correlation_id) for a correlation_id and refuses
    # ('correlation_conflict') on any mismatch - so a second call with
    # different commit/logs for the SAME open episode must replay the
    # original payload verbatim, not recompute a different one.
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)
    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=lambda url, timeout: _Response(502),
    )

    first = report_smoke_check_failure(
        _project(), result, store, client=client, commit="commit-one", logs="first attempt logs",
    )
    second = report_smoke_check_failure(
        _project(), result, store, client=client, commit="commit-two", logs="second attempt logs",
    )

    assert len(github.posts) == 1
    assert first.issue_available is True
    assert second.issue_available is True
    assert second.issue_reason is None  # never 'correlation_conflict'
    assert "commit-two" not in github.posts[0]["body"]


def test_pre_existing_legacy_task_from_before_the_episode_table_is_adopted_not_duplicated(store):
    # Round-3 finding #4 redux: #142's original scheme used the bare
    # sha256([project, kind, detail]) hash as correlation_id directly, with
    # no episodes table at all - and by the time this code runs, its OWN
    # detail-text formatting has already changed (round 2's naive re-hash
    # of the CURRENT wording never matches the legacy correlation_id at
    # all). Migration must match structurally (open post_deploy_check
    # Task for this project, not yet linked to an episode row), not by
    # recomputing a hash from today's wording.
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    # Simulate #142's own pre-episode-table code: a Task with a bare-hash
    # correlation_id, and its Issue created for real through GitHubClient
    # - exactly as #142 itself would have done, with #142's own wording.
    legacy_task = Task(
        title="[smoke check] hub: URL respondeu com erro de servidor",
        objective="URL respondeu com erro de servidor", project_id="hub",
        origin="post_deploy_check", state=TaskState.BUG_FOUND,
    )
    store.save_task(legacy_task)
    client.create_issue(
        "owner/repo", legacy_task.title, "corpo legado original",
        ["origin:post_deploy_check"], legacy_task.correlation_id,
    )
    assert len(github.posts) == 1

    # The SAME real failure recurs, but today's run_smoke_check produces
    # different wording/hash for it than #142's version did.
    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(502))

    report = report_smoke_check_failure(_project(), result, store, client=client)

    assert report.task.id == legacy_task.id
    assert report.task.correlation_id == legacy_task.correlation_id
    assert report.is_new_episode is False
    assert len(store.list_tasks(state=TaskState.BUG_FOUND)) == 1  # no second Task minted
    assert len(github.posts) == 1  # replayed the original payload verbatim, no new POST
    assert github.posts[0]["body"].startswith("corpo legado original")
    assert report.issue_available is True  # not correlation_conflict

    second = report_smoke_check_failure(_project(), result, store, client=client)
    assert second.task.id == legacy_task.id
    assert len(github.posts) == 1


def test_adopting_a_legacy_emergency_task_with_an_unanswered_page_does_not_page_again(store, monkeypatch):
    # Round-3 finding #3: #142's own notify_needs_lucas wording has also
    # changed since - a fresh page under new wording would not be
    # recognized as the same question by decisions.py's content-keyed
    # dedup, producing a second confirmed page for one real, STILL-UNANSWERED
    # episode. Adoption must not page again while the original question is
    # still outstanding.
    sent = []
    monkeypatch.setattr(
        "orchestrator.decisions.send_control_message",
        lambda message, **k: (sent.append(message), (True, None))[1],
    )
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    legacy_task = Task(
        title="[smoke check] hub: alvo inalcancavel", objective="alvo inalcancavel",
        project_id="hub", origin="post_deploy_check", state=TaskState.NEEDS_LUCAS,
    )
    store.save_task(legacy_task)
    client.create_issue(
        "owner/repo", legacy_task.title, "corpo legado original",
        ["origin:post_deploy_check"], legacy_task.correlation_id,
    )
    # Represents #142's own original (unresolved) page for this Task.
    store.save_decision(
        correlation_id=f"{legacy_task.correlation_id}:legacy", task_id=legacy_task.id,
        message="pergunta legada original",
    )

    def get_fn(url, timeout):
        raise requests.exceptions.ConnectionError("connection refused")

    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=get_fn,
    )
    report = report_smoke_check_failure(_project(), result, store, client=client)

    assert report.task.id == legacy_task.id
    assert report.notified is False
    assert sent == []
    assert len(store.get_pending_decisions()) == 1  # still only the original


def test_adopting_a_legacy_task_whose_old_decision_was_already_answered_still_pages_for_a_new_emergency(store, monkeypatch):
    # Independent-review finding: matching legacy Tasks by project_id alone
    # (kind/detail aren't recoverable from an old Task) means an adopted
    # Task might be an UNRELATED, already-resolved old incident - a
    # genuinely new "production is down" emergency must still page Lucas,
    # never silently swallowed just because some old Task got adopted.
    sent = []
    monkeypatch.setattr(
        "orchestrator.decisions.send_control_message",
        lambda message, **k: (sent.append(message), (True, None))[1],
    )
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    legacy_task = Task(
        title="[smoke check] hub: velho e ja resolvido", objective="velho e ja resolvido",
        project_id="hub", origin="post_deploy_check", state=TaskState.NEEDS_LUCAS,
    )
    store.save_task(legacy_task)
    client.create_issue(
        "owner/repo", legacy_task.title, "corpo legado antigo",
        ["origin:post_deploy_check"], legacy_task.correlation_id,
    )
    store.save_decision(
        correlation_id=f"{legacy_task.correlation_id}:legacy", task_id=legacy_task.id,
        message="pergunta legada ja respondida",
    )
    store.resolve_decision(f"{legacy_task.correlation_id}:legacy", "ja resolvido ha meses")

    def get_fn(url, timeout):
        raise requests.exceptions.ConnectionError("connection refused")

    result = run_smoke_check(
        _project(), DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example"),
        get_fn=get_fn,
    )
    report = report_smoke_check_failure(_project(), result, store, client=client)

    assert report.task.id == legacy_task.id  # identity reuse still happened
    assert report.notified is True  # but the NEW emergency still got paged
    assert len(sent) == 1


def test_resolving_an_adopted_legacy_episode_is_not_undone_by_re_adoption(store):
    # Round-3 finding #4: resolve_episode() on an adopted row used to be
    # silently ignored - the next call re-ran the SAME structural legacy
    # lookup, found the still-BUG_FOUND legacy Task again, and re-adopted
    # it under the same identity, undoing the resolution. Once adopted, a
    # legacy task_id must never be discoverable as "unmigrated" again.
    github = _FakeGitHub()
    client = GitHubClient(store, run_fn=github.run, timeout_seconds=5)

    legacy_task = Task(
        title="[smoke check] hub: URL respondeu com erro de servidor",
        objective="URL respondeu com erro de servidor", project_id="hub",
        origin="post_deploy_check", state=TaskState.BUG_FOUND,
    )
    store.save_task(legacy_task)
    client.create_issue(
        "owner/repo", legacy_task.title, "corpo legado original",
        ["origin:post_deploy_check"], legacy_task.correlation_id,
    )

    strategy = DeployStrategy(kind="web", trigger="auto_on_push", production_url="https://x.example")
    result = run_smoke_check(_project(), strategy, get_fn=lambda url, timeout: _Response(502))

    first = report_smoke_check_failure(_project(), result, store, client=client)
    assert first.task.id == legacy_task.id

    resolve_episode(store, _project(), result.kind, result.detail)
    second = report_smoke_check_failure(_project(), result, store, client=client)

    assert second.task.id != legacy_task.id  # a genuinely new episode, not the resolved legacy one
    assert second.is_new_episode is True
