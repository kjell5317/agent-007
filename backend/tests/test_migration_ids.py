"""Alembic stores the current revision in `alembic_version.version_num`,
a VARCHAR(32): a longer id fails the upgrade (and the container start)."""

import ast
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def test_revision_ids_fit_alembic_version_column():
    too_long = []
    for path in sorted(VERSIONS.glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "revision" for t in node.targets
            ):
                revision = ast.literal_eval(node.value)
                if len(revision) > 32:
                    too_long.append(f"{path.name}: {revision} ({len(revision)})")
    assert too_long == []
