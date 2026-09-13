"""Real Windows process lifecycle in a disposable project, never the voice app."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows process commands')
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'Jarvis.ps1'


def command(root, action, *, ok=True):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-File', str(SCRIPT), '-Action', action, '-ProjectRoot', str(root)],
                            capture_output=True, text=True, timeout=35)
    if ok:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return result


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'Jarvis test with spaces'
    root.mkdir()
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')], check=True)
    (root / 'main.py').write_text(
        'import os,time,pathlib\n'
        'root=pathlib.Path(__file__).parent\n'
        '(root/".jarvis.pid").write_text(str(os.getpid()))\n'
        '(root/"jarvis.log").write_text("fixture running")\n'
        'time.sleep(120)\n', encoding='utf-8')
    yield root
    command(root, 'stop', ok=False)


def test_real_start_status_idempotent_start_restart_stop_logs(project):
    assert command(project, 'status')['state'] == 'stopped'
    first = command(project, 'start')
    assert first['state'] == 'running'
    assert command(project, 'start')['process_ids'] == first['process_ids']
    restarted = command(project, 'restart')
    assert restarted['state'] == 'running'
    assert set(restarted['process_ids']).isdisjoint(first['process_ids'])
    logs = command(project, 'logs')
    assert logs['exists'] and logs['log_file'] == str(project / 'jarvis.log')
    assert command(project, 'stop')['state'] == 'stopped'
    assert command(project, 'stop')['state'] == 'stopped'


def test_foreign_pid_and_similar_command_are_never_killed(project):
    sleeper = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)', str(project / 'main.py')])
    try:
        (project / '.jarvis.pid').write_text(str(sleeper.pid))
        assert command(project, 'status')['state'] == 'identity_mismatch'
        assert command(project, 'start', ok=False).returncode != 0
        command(project, 'stop')
        assert sleeper.poll() is None
        assert (project / '.jarvis.pid').read_text() == str(sleeper.pid)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_stop_finds_orphan_and_duplicate_main_without_pid(project):
    python = project / '.venv' / 'Scripts' / 'pythonw.exe'
    processes = [subprocess.Popen([str(python), str(project / 'main.py')], cwd=project) for _ in range(2)]
    try:
        time.sleep(1)
        (project / '.jarvis.pid').unlink(missing_ok=True)
        status = command(project, 'status')
        assert len(status['process_ids']) >= 2
        assert command(project, 'stop')['state'] == 'stopped'
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def test_stale_pid_and_missing_environment_are_reported(tmp_path):
    (tmp_path / '.jarvis.pid').write_text('not a pid')
    assert command(tmp_path, 'status')['pid_state'] == 'invalid'
    assert command(tmp_path, 'start', ok=False).returncode != 0
    assert not command(tmp_path, 'logs')['exists']


def test_concurrent_start_creates_one_instance(project):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: command(project, 'start'), range(2)))
    assert results[0]['process_ids'] == results[1]['process_ids']
    # Windows venv can have both launcher and child; PID identifies the worker.
    assert command(project, 'status')['pid_state'] == 'matched'


def test_legacy_relative_main_is_stopped_but_sibling_project_survives(project, tmp_path):
    sibling = tmp_path / 'different project'
    sibling.mkdir()
    (sibling / 'main.py').write_text('import time;time.sleep(120)', encoding='utf-8')
    foreign = subprocess.Popen([sys.executable, str(sibling / 'main.py')])
    legacy = subprocess.Popen([str(project / '.venv' / 'Scripts' / 'pythonw.exe'), 'main.py'], cwd=project)
    try:
        time.sleep(1)
        assert command(project, 'status')['state'] == 'running'
        assert command(project, 'stop')['state'] == 'stopped'
        assert foreign.poll() is None
    finally:
        for process in (foreign, legacy):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
