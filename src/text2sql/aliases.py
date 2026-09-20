"""Resolve user-facing plant and unit aliases without guessing ambiguities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from align.naming import chinese_number


@dataclass(frozen=True)
class AliasResolution:
    value: str | None
    candidates: tuple[str, ...]
    ambiguous: bool = False


_UNIT_PLANTS = "台中|林口|大林|興達"
_CHINESE_DIGITS = "一二三四五六七八九十"


def _unit_number(token: str) -> int | None:
    """機組編號 token → 整數。阿拉伯與中文寫法都接受，超出 1～12 回 None。"""

    if token.isdigit():
        return int(token)
    for number in range(1, 13):
        if chinese_number(number) == token:
            return number
    return None


PLANT_ALIASES = {
    "中火": "台中發電廠",
    "台中電廠": "台中發電廠",
    "興達電廠": "興達發電廠",
    "林口電廠": "林口發電廠",
    "大潭電廠": "大潭發電廠",
}


def resolve_plant(question: str, plants: set[str]) -> AliasResolution:
    for alias, formal in PLANT_ALIASES.items():
        if alias in question and formal in plants:
            return AliasResolution(formal, (formal,))
    matches = tuple(sorted(plant for plant in plants if plant.removesuffix("發電廠") in question))
    return (
        AliasResolution(matches[0], matches)
        if len(matches) == 1
        else AliasResolution(None, matches, len(matches) > 1)
    )


def resolve_peak_column(question: str, columns: set[str]) -> AliasResolution:
    ambiguous_hsinta = re.search(r"興達\s*#?\s*(?:3|(?:第)?三)(?:號|機|部|相關|是|的|\b)", question)
    if ambiguous_hsinta:
        candidates = tuple(column for column in ("興達#3", "興達 (#1-#5)") if column in columns)
        if len(candidates) > 1:
            return AliasResolution(None, candidates, True)

    exact = tuple(
        sorted((column for column in columns if column in question), key=len, reverse=True)
    )
    if exact:
        longest = exact[0]
        ties = tuple(column for column in exact if len(column) == len(longest))
        if len(ties) > 1:
            # 同長度的多個候選不必然是歧義：「比較台中#1和台中#2」是兩台機組各出現一
            # 次，佔的是問句的不同位置。真正的歧義是同一段文字有兩種讀法。
            #
            # 實測：不分辨的話，凡是用「#」寫法比較兩台機組都會被擋成
            # AMBIGUOUS_UNIT_NAME，而同一句改寫成「台中一號二號」卻會通過 —— 同一個
            # 問題兩種寫法結果相反，且被擋的那種完全答不出來。
            spans = sorted((question.find(column), column) for column in ties)
            overlapping = any(
                start + len(column) > spans[index + 1][0]
                for index, (start, column) in enumerate(spans[:-1])
            )
            if overlapping:
                return AliasResolution(None, ties, True)
            return AliasResolution(spans[0][1], ties)
        return AliasResolution(longest, ties)

    match = re.search(r"(台中|林口|大林|興達)\s*#?(\d{1,2})\s*(?:號|機)?", question)
    if match:
        plant, number = match.groups()
        candidate = f"{plant}#{int(number)}"
        if plant == "興達" and int(number) == 3:
            candidates = tuple(column for column in ("興達#3", "興達 (#1-#5)") if column in columns)
            return AliasResolution(None, candidates, True)
        if candidate in columns:
            return AliasResolution(candidate, (candidate,))

    chinese = re.search(rf"({_UNIT_PLANTS})\s*(?:第)?([{_CHINESE_DIGITS}]+)(?:號|部)", question)
    if chinese:
        number = _unit_number(chinese.group(2))
        if number is not None:
            candidate = f"{chinese.group(1)}#{number}"
            if chinese.group(1) == "興達" and number == 3:
                candidates = tuple(
                    column for column in ("興達#3", "興達 (#1-#5)") if column in columns
                )
                return AliasResolution(None, candidates, True)
            if candidate in columns:
                return AliasResolution(candidate, (candidate,))
    return AliasResolution(None, ())


def resolve_peak_columns(question: str, columns: set[str]) -> tuple[str, ...]:
    """Resolve multiple comparison targets while preserving mention order."""

    matches: list[tuple[int, str]] = []
    for column in columns:
        position = question.find(column)
        if position >= 0:
            matches.append((position, column))
            continue
        base = re.sub(r"\s*\(.*", "", column)
        if base and base in question and "(" in column and "彙總" in question:
            matches.append((question.find(base), column))

    numbered = list(re.finditer(rf"({_UNIT_PLANTS})\s*#?(\d{{1,2}})", question))
    for match in numbered:
        candidate = f"{match.group(1)}#{int(match.group(2))}"
        if candidate in columns:
            matches.append((match.start(), candidate))
    if numbered:
        prefix = numbered[-1].group(1)
        for match in re.finditer(r"[和與及、]\s*#?(\d{1,2})", question):
            candidate = f"{prefix}#{int(match.group(1))}"
            if candidate in columns:
                matches.append((match.start(), candidate))

    # 中文寫法的機組編號。阿拉伯數字可以不接後綴（「查興達3機」），中文**一定要**接
    # 「號／部／機」——少了它，「找單一機組一段時間的極值」的「一」會被讀成 1 號機。
    chinese_numbered = list(
        re.finditer(rf"({_UNIT_PLANTS})\s*(?:第)?([{_CHINESE_DIGITS}]+)\s*(?:號|部|機)", question)
    )
    for match in chinese_numbered:
        number = _unit_number(match.group(2))
        if number is not None:
            candidate = f"{match.group(1)}#{number}"
            if candidate in columns:
                matches.append((match.start(), candidate))
    if chinese_numbered:
        # 「林口二號與三號」「台中三號四號」的第二台只寫編號，沿用前一個廠名。這裡只
        # 收「號／部」，不收「機」——收了的話「單一機組」會被當成該廠的 1 號機。
        prefix = chinese_numbered[-1].group(1)
        for match in re.finditer(rf"(?:第)?([{_CHINESE_DIGITS}]+)\s*(?:號|部)", question):
            if any(seen.start() <= match.start() < seen.end() for seen in chinese_numbered):
                continue
            number = _unit_number(match.group(1))
            if number is not None:
                candidate = f"{prefix}#{number}"
                if candidate in columns:
                    matches.append((match.start(), candidate))

    unique: list[str] = []
    for _position, column in sorted(set(matches)):
        if column not in unique:
            unique.append(column)
    return tuple(unique)
