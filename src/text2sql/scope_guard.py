"""Two-tier account authorisation for the approved semantic views.

台電的組織架構把水力與火力放在同一個「水火力發電事業部／發電處」底下，兩者之間沒有
組織界線，真正的界線是電廠。因此帳號只分兩種：`plant` 只看得到自己電廠的資料，`all`
看得到全部。水力／火力是 `dim_plant_scope.plant_type` 上的標籤，不是權限層級。

SQLite 沒有 GRANT 或使用者系統，授權只能在應用層執行。這個模組的做法是把已通過
`SqlGuard` 驗證的 SQL 重寫一次：每個受管檢視的參照都被包進一層只含本帳號可見列的
子查詢，因此 JOIN、子查詢與聚合都會自動套用，不必修改提示詞、語料或檢視定義。

`v_system`、`v_generation_cost` 與 `v_re_generation` 不受管。全系統尖峰負載與備轉容量屬
輸供電事業部電力調度處，發電成本屬會計處，兩者都沒有電廠欄位；再生能源場站屬再生能源
處，不在 34 筆電廠主檔的組織範圍內，也沒有可對應的電廠。

`SqlGuard` 放行的每一張檢視都必須出現在 `SCOPE_KEYS` 或 `SHARED_VIEWS`。兩邊都查不到的
檢視會被拒絕，不是原封不動放行——否則新增一張帶電廠欄位的檢視就會讓電廠帳號讀到全部列，
而且不會有任何錯誤訊息。這與建庫時「新的每日欄位沒指定 `access_scope` 就中止建庫」是同一
條原則：授權範圍未知時停下來，不要猜。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

SHARED_VIEWS = frozenset({"v_system", "v_generation_cost", "v_re_generation"})

SCOPE_KEYS = {
    "v_unit": "電廠",
    "v_peak": "機組欄位",
    "v_outage": "歲修編號",
}

_MARKER = re.compile(r":(__[a-z]\d+)")


class ScopeError(Exception):
    """Base class for authorisation failures that are safe to show a user."""


class UnknownPlantError(ScopeError):
    """The account names a plant that is not in the authorisation roster."""

    def __init__(self, plant: str):
        self.plant = plant
        super().__init__(f"帳號綁定的電廠「{plant}」不在授權名冊中。")


class ScopeRewriteError(ScopeError):
    """The validated SQL could not be restricted to the account's rows."""


class UnclassifiedViewError(ScopeRewriteError):
    """A queryable view carries no authorisation classification."""

    def __init__(self, view: str):
        self.view = view
        super().__init__(
            f"檢視「{view}」沒有授權分類，無法判斷這個帳號看得到哪些列。"
            "請在 SCOPE_KEYS 指定範圍欄位，或在 SHARED_VIEWS 記錄它不受管。"
        )


@dataclass(frozen=True)
class PlantScope:
    """Everything one plant account may read, resolved once at startup."""

    plant_id: int
    plant_name: str
    plant_type: str
    ownership: str
    peak_columns: tuple[str, ...]
    shared_columns: tuple[str, ...]
    outage_ids: tuple[int, ...]

    @property
    def own_peak_columns(self) -> tuple[str, ...]:
        shared = set(self.shared_columns)
        return tuple(column for column in self.peak_columns if column not in shared)

    @property
    def has_own_daily_column(self) -> bool:
        """False when the plant's output only survives inside a shared bucket column."""
        return bool(self.own_peak_columns)


@dataclass(frozen=True)
class ScopeCatalog:
    plants: dict[str, PlantScope]

    @classmethod
    def from_database(cls, database: Path) -> ScopeCatalog:
        uri = f"{Path(database).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            roster = connection.execute(
                """SELECT plant_id, plant_name, plant_type, ownership
                     FROM dim_plant_scope ORDER BY plant_id"""
            ).fetchall()
            shared = tuple(
                row[0]
                for row in connection.execute(
                    """SELECT c.b_column
                         FROM b_column_scope AS s
                         JOIN dim_b_column AS c ON c.id = s.b_column_id
                        WHERE s.access_scope = 'shared'
                        ORDER BY c.id"""
                )
            )
            owned: dict[int, list[str]] = {}
            for plant_id, b_column in connection.execute(
                """SELECT bp.plant_id, c.b_column
                     FROM bridge_b_column_plant AS bp
                     JOIN b_column_scope AS s ON s.b_column_id = bp.b_column_id
                     JOIN dim_b_column AS c ON c.id = bp.b_column_id
                    WHERE s.access_scope = 'plant'
                    ORDER BY bp.plant_id, c.id"""
            ):
                owned.setdefault(plant_id, []).append(b_column)
            outages: dict[int, list[int]] = {}
            for plant_id, outage_id in connection.execute(
                "SELECT plant_id, outage_id FROM outage_scope ORDER BY plant_id, outage_id"
            ):
                outages.setdefault(plant_id, []).append(outage_id)

        plants = {
            plant_name: PlantScope(
                plant_id=plant_id,
                plant_name=plant_name,
                plant_type=plant_type,
                ownership=ownership,
                peak_columns=(*owned.get(plant_id, ()), *shared),
                shared_columns=shared,
                outage_ids=tuple(outages.get(plant_id, ())),
            )
            for plant_id, plant_name, plant_type, ownership in roster
        }
        return cls(plants)

    def scope_for(self, plant: str) -> PlantScope:
        scope = self.plants.get(plant)
        if scope is None:
            raise UnknownPlantError(plant)
        return scope

    def plant_names_by_id(self) -> dict[int, str]:
        """Map the stable authorisation identifier to today's plant name.

        帳號名冊綁的是編號，改寫用的是名稱；這個對照表是兩者之間唯一的轉換點。
        """

        return {scope.plant_id: scope.plant_name for scope in self.plants.values()}


def _allowed_values(scope: PlantScope, view: str) -> Sequence[object]:
    if view == "v_unit":
        return (scope.plant_name,)
    if view == "v_peak":
        return scope.peak_columns
    if view == "v_outage":
        return scope.outage_ids
    # SCOPE_KEYS 新增一張檢視卻沒有對應規則時，沿用上一張的值會放行錯的列。
    raise ScopeRewriteError(f"受管檢視「{view}」沒有可見列規則，無法套用授權範圍。")


def _name_original_placeholders(
    tree: exp.Expression, params: Sequence[object]
) -> dict[str, object]:
    """Give every ``?`` a unique name so its parameter survives the rewrite.

    Tree traversal order is not guaranteed to match the rendered text order, and SQLite
    binds ``?`` positionally by text order.  Naming first and re-reading the rendered SQL
    establishes the true order before any scope predicate is inserted.
    """

    placeholders = list(tree.find_all(exp.Placeholder))
    for index, node in enumerate(placeholders):
        if node.this:
            raise ScopeRewriteError("SQL 已使用具名參數，授權改寫只支援位置參數。")
        node.set("this", f"__p{index}")
    order = _MARKER.findall(tree.sql(dialect="sqlite"))
    if len(order) != len(params):
        raise ScopeRewriteError(
            f"SQL placeholder 數量（{len(order)}）與參數數量（{len(params)}）不一致。"
        )
    return dict(zip(order, params, strict=True))


def _scoped_subquery(table: exp.Table, marker_names: Sequence[str]) -> exp.Subquery:
    if marker_names:
        placeholders = ", ".join(f":{name}" for name in marker_names)
        predicate = f'"{SCOPE_KEYS[table.name]}" IN ({placeholders})'
    else:
        # 該電廠在這張檢視完全沒有可見列；保留合法 SQL 而不是產生 IN ()。
        predicate = "1 = 0"
    inner = parse_one(f"SELECT * FROM {table.name} WHERE {predicate}", read="sqlite")
    return exp.Subquery(
        this=inner,
        alias=exp.TableAlias(this=exp.to_identifier(table.alias_or_name)),
    )


class ScopeGuard:
    """Restrict validated SQL to the rows one account may read."""

    def __init__(self, catalog: ScopeCatalog):
        self.catalog = catalog

    @classmethod
    def from_database(cls, database: Path) -> ScopeGuard:
        return cls(ScopeCatalog.from_database(database))

    def apply(
        self, sql: str, params: Sequence[object], *, plant: str | None
    ) -> tuple[str, tuple[object, ...]]:
        """Rewrite ``sql`` for a plant account; ``plant=None`` is the all-plants scope."""

        if plant is None:
            return sql, tuple(params)
        scope = self.catalog.scope_for(plant)
        try:
            tree = parse_one(sql, read="sqlite")
        except ParseError as error:
            raise ScopeRewriteError(f"授權改寫無法解析 SQL：{error}") from error

        bindings = _name_original_placeholders(tree, params)
        counter = 0

        def rewrite(node: exp.Expression) -> exp.Expression:
            nonlocal counter
            if not isinstance(node, exp.Table):
                return node
            if node.name in SHARED_VIEWS:
                return node
            if node.name not in SCOPE_KEYS:
                raise UnclassifiedViewError(node.name)
            names = []
            for value in _allowed_values(scope, node.name):
                name = f"__s{counter}"
                counter += 1
                bindings[name] = value
                names.append(name)
            return _scoped_subquery(node, names)

        scoped = tree.transform(rewrite, copy=True)
        rendered = scoped.sql(dialect="sqlite")
        ordered = _MARKER.findall(rendered)
        missing = [name for name in ordered if name not in bindings]
        if missing:
            raise ScopeRewriteError(f"授權改寫遺失參數：{missing}")
        return _MARKER.sub("?", rendered), tuple(bindings[name] for name in ordered)
