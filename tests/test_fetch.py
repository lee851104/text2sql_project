from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ingest.fetch import Dataset, download_datasets


def test_download_archives_by_content_hash_and_updates_latest(tmp_path: Path) -> None:
    dataset = Dataset("fixture", "d000000", "fixture.csv", "fixture")
    payload = b"date,value\n20260101,1\n"

    manifest = download_datasets([dataset], data_root=tmp_path, downloader=lambda _url: payload)
    digest = hashlib.sha256(payload).hexdigest()

    assert (tmp_path / "raw/fixture.csv").read_bytes() == payload
    assert (tmp_path / f"archive/fixture/{digest}.csv").read_bytes() == payload
    saved_manifest = json.loads((tmp_path / "raw/manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest == manifest
    assert saved_manifest["resources"][0]["sha256"] == digest


def test_same_payload_reuses_archive_snapshot(tmp_path: Path) -> None:
    dataset = Dataset("fixture", "d000000", "fixture.csv", "fixture")
    payload = b"date,value\n20260101,1\n"

    download_datasets([dataset], data_root=tmp_path, downloader=lambda _url: payload)
    download_datasets([dataset], data_root=tmp_path, downloader=lambda _url: payload)

    assert len(list((tmp_path / "archive/fixture").glob("*.csv"))) == 1


def test_json_endpoint_keeps_its_own_suffix(tmp_path: Path) -> None:
    """`d006001` 只提供 JSON；封存副檔名必須跟著 suffix，不能寫死 .csv。"""
    dataset = Dataset("fixture", "d000000", "fixture.json", "fixture", suffix="json")
    payload = b'{"aaData": []}'

    assert dataset.url.endswith("/d000000/001.json")
    download_datasets([dataset], data_root=tmp_path, downloader=lambda _url: payload)
    digest = hashlib.sha256(payload).hexdigest()

    assert (tmp_path / "raw/fixture.json").read_bytes() == payload
    assert (tmp_path / f"archive/fixture/{digest}.json").read_bytes() == payload


def test_csv_remains_the_default_suffix() -> None:
    assert Dataset("fixture", "d000000", "fixture.csv", "fixture").url.endswith("001.csv")


EXPECTED_DATASETS = {
    "units": ("d004011", "units.csv"),
    "daily": ("d006005", "daily.csv"),
    "outage": ("d006008", "outage.csv"),
    "generation_cost": ("d018001", "generation_cost.csv"),
    "nuclear_units": ("d056001", "nuclear_units.csv"),
    "re_sites": ("d693002", "re_sites.csv"),
    "re_generation": ("d693001", "re_generation.csv"),
    "units_generation": ("d006001", "units_generation.json"),
}


def test_every_official_source_is_registered_with_its_resource_code() -> None:
    """資料源清單是可重現性的邊界：漏掉一份就代表那份無法重抓也無從驗 checksum。"""
    from ingest.fetch import DATASETS

    assert {name: (d.resource, d.filename) for name, d in DATASETS.items()} == EXPECTED_DATASETS


def test_every_registered_source_has_a_configured_path() -> None:
    """新增資料源卻忘了接進 configs/config.yaml，建庫時才會發現；在這裡先擋下。"""
    from ingest.fetch import DATASETS
    from ingest.validate import load_project_config

    configured = set(load_project_config()["paths"])
    missing = {
        name
        for name, dataset in DATASETS.items()
        if f"{Path(dataset.filename).stem}_{Path(dataset.filename).suffix.lstrip('.')}"
        not in configured
    }
    assert not missing, f"這些資料源沒有對應的 config path：{sorted(missing)}"
