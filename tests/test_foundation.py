from pathlib import Path

import yaml

from project_tasks import GENERATED_DIRECTORIES, init_dirs

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_required_configuration_files_are_valid_yaml() -> None:
    config_dir = PROJECT_ROOT / "configs"
    expected = {
        "config.yaml",
        "align.yaml",
        "outage_overrides.yaml",
        "renewable_overrides.yaml",
        "llm.yaml",
        "retriever.yaml",
        "guard.yaml",
    }
    assert {path.name for path in config_dir.glob("*.yaml")} == expected
    for path in config_dir.glob("*.yaml"):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None


def test_init_dirs_is_idempotent(tmp_path: Path) -> None:
    first = init_dirs(tmp_path)
    second = init_dirs(tmp_path)

    assert first == second
    assert {path.relative_to(tmp_path).as_posix() for path in first} == set(GENERATED_DIRECTORIES)
    assert all(path.is_dir() for path in second)
