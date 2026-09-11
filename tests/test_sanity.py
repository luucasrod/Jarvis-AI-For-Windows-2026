"""Sanity checks proving the pytest infrastructure itself works.

`orchestrator/` does not exist yet (see issue #9) - the import below is
skipped gracefully until then, so this test stays green today and starts
actually validating the package the moment #9 lands.
"""
import pytest


def test_pytest_runs():
    assert True


def test_orchestrator_package_importable_once_it_exists():
    orchestrator = pytest.importorskip("orchestrator")
    assert orchestrator is not None
