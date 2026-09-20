"""Generation-cost hierarchy: which rows are aggregates, and when to list the rest."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from ingest.validate import PROJECT_ROOT

# 問句要的是一覽而不是某一種。指名問單一種類仍走原本的路（`_generation_cost_type`）。
OVERVIEW_WORDS: tuple[str, ...] = (
    "各種",
    "各發電方式",
    "各類發電",
    "所有發電方式",
    "全部發電方式",
    "每種發電方式",
    "發電方式有哪些",
    "哪些發電方式",
    "成本一覽",
    "成本排名",
    "成本排序",
    "成本由高到低",
    "成本比較",
)


@lru_cache(maxsize=1)
def aggregate_rows(root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """「發電方式」欄裡屬於彙總層的值，由 configs/generation_cost.yaml 裁決。

    讀不到設定時回空 tuple：一覽式問句就會退回原本的「請指定發電方式」，而不是把
    彙總與明細混在一起列出去。少列比列錯好。
    """

    try:
        payload = yaml.safe_load(
            (root / "configs/generation_cost.yaml").read_text(encoding="utf-8")
        )
        rows = payload["aggregate_rows"]
    except (OSError, TypeError, KeyError, yaml.YAMLError):
        return ()
    if not isinstance(rows, list) or not all(isinstance(item, str) for item in rows):
        return ()
    return tuple(rows)


def wants_overview(question: str) -> bool:
    """這句成本問句要的是全部明細的一覽嗎？"""

    return any(word in question for word in OVERVIEW_WORDS)
