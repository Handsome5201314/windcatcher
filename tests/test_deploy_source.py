"""Keep the offline ModelScope release bundle aligned with its Docker build."""

from pathlib import Path
import shlex

import pytest

from scripts import deploy_modelscope


REQUIRED_FILES = (
    "deploy/login.html",
    "deploy/patient-view.html",
    "deploy/reference-view.html",
    "deploy/start.sh",
    "scripts/backup_identity.py",
)


def test_offline_release_contains_docker_and_runtime_inputs(tmp_path):
    sources = deploy_modelscope.copy_source(tmp_path)
    copied = {path.relative_to(deploy_modelscope.ROOT).as_posix() for path in sources}
    allowlist = set((tmp_path / ".dockerignore").read_text(encoding="utf-8").splitlines())

    for relative in REQUIRED_FILES:
        assert relative in copied
        assert (tmp_path / relative).is_file()
        assert f"!{relative}" in allowlist
    assert (tmp_path / "pipeline" / "access_identity.py").is_file()
    assert (tmp_path / "static" / "access-admin.js").is_file()
    for line in (tmp_path / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            for source in shlex.split(line)[1:-1]:
                assert (tmp_path / source.rstrip("/")).exists(), source
    assert not (tmp_path / "deploy" / "share-admin.html").exists()
    assert not (tmp_path / ".env").exists()


def test_missing_required_source_fails_before_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_modelscope, "FILES", ("missing-required.html",))
    monkeypatch.setattr(deploy_modelscope, "ROOT", Path(tmp_path))
    with pytest.raises(RuntimeError, match="发布包缺少必需文件：missing-required.html"):
        deploy_modelscope.copy_source(tmp_path / "stage")
