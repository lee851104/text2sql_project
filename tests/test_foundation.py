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
        "b_column_capacity.yaml",
        "llm.yaml",
        "retriever.yaml",
        "guard.yaml",
        "accounts.example.yaml",
        "coverage.yaml",
        "generation_cost.yaml",
    }
    # 名冊是每台機器自己的檔案（不進版控），存在與否都不該影響這個清單檢查。
    local_only = {"accounts.yaml"}

    present = {path.name for path in config_dir.glob("*.yaml")}

    assert expected <= present, f"缺少設定檔：{sorted(expected - present)}"
    assert present - expected <= local_only, (
        f"configs/ 出現未預期的檔案：{sorted(present - expected - local_only)}"
    )
    for path in config_dir.glob("*.yaml"):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None


def test_init_dirs_is_idempotent(tmp_path: Path) -> None:
    first = init_dirs(tmp_path)
    second = init_dirs(tmp_path)

    assert first == second
    assert {path.relative_to(tmp_path).as_posix() for path in first} == set(GENERATED_DIRECTORIES)
    assert all(path.is_dir() for path in second)
