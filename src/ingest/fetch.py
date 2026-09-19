"""Download and content-address Taipower source snapshots.

The daily dataset is a rolling window. Every distinct response is therefore
archived by SHA-256 before the latest copy is replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import tempfile
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import certifi

from ingest.validate import PROJECT_ROOT

BASE_URL = "https://service.taipower.com.tw/data/opendata/apply/file/{resource}/001.{suffix}"


@dataclass(frozen=True)
class Dataset:
    name: str
    resource: str
    filename: str
    description: str
    suffix: str = "csv"
    """官方端點的副檔名；`d006001` 只提供 JSON。"""

    @property
    def url(self) -> str:
        return BASE_URL.format(resource=self.resource, suffix=self.suffix)


DATASETS = {
    dataset.name: dataset
    for dataset in (
        Dataset("units", "d004011", "units.csv", "水火力發電廠位置及機組設備"),
        Dataset("daily", "d006005", "daily.csv", "過去電力供需資訊（滾動視窗）"),
        Dataset("outage", "d006008", "outage.csv", "機組歲修排程"),
        Dataset("generation_cost", "d018001", "generation_cost.csv", "各種發電方式之發電成本"),
        Dataset("nuclear_units", "d056001", "nuclear_units.csv", "核能發電廠位置及機組設備"),
        Dataset("re_sites", "d693002", "re_sites.csv", "再生能源各場址資料"),
        Dataset("re_generation", "d693001", "re_generation.csv", "自建各類再生能源發電量"),
        Dataset(
            "units_generation",
            "d006001",
            "units_generation.json",
            "各機組發電量即時資訊（含外購電力）",
            suffix="json",
        ),
    )
}


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "PowerQuery-TW/0.1"})
    tls_context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(  # noqa: S310
        request, timeout=120, context=tls_context
    ) as response:
        payload = response.read()
    if not payload:
        raise ValueError(f"下載結果為空：{url}")
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def download_datasets(
    datasets: Iterable[Dataset],
    *,
    data_root: Path,
    downloader: Callable[[str], bytes] = fetch_bytes,
) -> dict[str, object]:
    fetched_at = datetime.now(UTC).isoformat()
    resources: list[dict[str, object]] = []
    for dataset in datasets:
        payload = downloader(dataset.url)
        digest = hashlib.sha256(payload).hexdigest()
        archive_path = data_root / "archive" / dataset.name / f"{digest}.{dataset.suffix}"
        latest_path = data_root / "raw" / dataset.filename
        if not archive_path.exists():
            _atomic_write(archive_path, payload)
        _atomic_write(latest_path, payload)
        resources.append(
            {
                **asdict(dataset),
                "url": dataset.url,
                "bytes": len(payload),
                "sha256": digest,
                "latest_path": latest_path.relative_to(data_root).as_posix(),
                "archive_path": archive_path.relative_to(data_root).as_posix(),
            }
        )

    manifest: dict[str, object] = {
        "fetched_at": fetched_at,
        "license": "政府資料開放授權條款－第 1 版",
        "resources": resources,
    }
    _atomic_write(
        data_root / "raw" / "manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        action="append",
        choices=sorted(DATASETS),
        help="只下載指定資料集；可重複使用。預設下載全部三份。",
    )
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "data")
    args = parser.parse_args(argv)
    selected = args.dataset or list(DATASETS)
    manifest = download_datasets(
        (DATASETS[name] for name in selected), data_root=args.data_root.resolve()
    )
    for resource in manifest["resources"]:
        print(f"{resource['name']}: {resource['bytes']} bytes, sha256={resource['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
