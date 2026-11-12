"""Pytest must not write endplane evidence into the operator's local ledger."""

from pipeline import endplane_history


def test_endplane_write_uses_per_test_learning_directory(tmp_path):
    path = endplane_history._path()
    assert path == tmp_path / "learning" / endplane_history.DATABASE_NAME
    assert not path.exists()

    plan = {"format": "oblique_planes_v1", "scope": "main_airway",
            "mask_version": 1, "anterior_index": 20, "posterior_index": 8}
    revision_id = endplane_history.begin_revision(
        "synthetic-storage-isolation", "main_airway", 1, None, plan)
    endplane_history.commit_revision(revision_id)

    assert path.exists()
    assert len(endplane_history.list_revisions("synthetic-storage-isolation")) == 1
