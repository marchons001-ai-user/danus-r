"""Trusted control-package path when reusing an existing numerical interpreter.

No pip installs, processes, CUDA or external network required.
"""
from __future__ import annotations

import os

import pytest
from danus.execution import worker


def test_gateway_uses_repo_only_without_opt_in(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "danus" / "execution").mkdir(parents=True)
    monkeypatch.setattr(worker, "__file__", str(root / "danus" / "execution" / "worker.py"))
    monkeypatch.delenv("DANUS_TRUSTED_CONTROL_SITE", raising=False)
    assert worker._gateway_pythonpath() == str(root)


def test_gateway_accepts_only_shared_control_site_inside_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "danus" / "execution").mkdir(parents=True)
    shared = root / "runtime" / "venv" / "lib" / "python3.12" / "site-packages"
    shared.mkdir(parents=True)
    monkeypatch.setattr(worker, "__file__", str(root / "danus" / "execution" / "worker.py"))
    monkeypatch.setenv("DANUS_TRUSTED_CONTROL_SITE", str(shared))
    assert worker._gateway_pythonpath() == str(root) + os.pathsep + str(shared)

    outside = tmp_path / "other" / "site-packages"
    outside.mkdir(parents=True)
    monkeypatch.setenv("DANUS_TRUSTED_CONTROL_SITE", str(outside))
    with pytest.raises(ValueError, match="trusted control site"):
        worker._gateway_pythonpath()

    monkeypatch.setenv("DANUS_TRUSTED_CONTROL_SITE", str(shared / "missing"))
    with pytest.raises(ValueError, match="trusted control site"):
        worker._gateway_pythonpath()
