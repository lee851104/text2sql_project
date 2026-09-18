"""Run reproducible alignment validation and write a machine-readable report."""

from __future__ import annotations

import csv
import json

import yaml

from align.crosswalk import parse_crosswalk, validate_crosswalk
from align.outage_align import align_outage_rows, summarize_outage_alignment
from align.pitfalls import generate_pitfalls
from align.renewable import (
    align_stations,
    chinese_part,
    is_subtotal,
    normalize_station,
    parse_county,
    parse_generation,
    repair_generation,
    summarize_alignment,
)
from ingest.validate import PROJECT_ROOT

RENEWABLE_CROSSWALK_COLUMNS = (
    "station_key",
    "status",
    "energy_type",
    "county",
    "site_name",
    "generation_name",
    "site_count",
    "capacity_kw",
    "months",
    "total_kwh",
    "missing_months",
)


def _read_bilingual_csv(path):
    """Read one of the renewable CSVs, keyed by the Chinese side of each header."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return [], {}
    return rows, {chinese_part(name): name for name in rows[0]}


def _renewable_alignment(overrides):
    """Clean 17141／17140 and build the auditable station crosswalk."""
    align_root = PROJECT_ROOT / "taipower_align"
    sites, site_key = _read_bilingual_csv(align_root / "re_sites.csv")
    generation, gen_key = _read_bilingual_csv(align_root / "re_generation.csv")
    supplement_path = align_root / "re_sites_supplement.csv"
    supplement: list[dict[str, str]] = []
    if supplement_path.is_file():
        with supplement_path.open(encoding="utf-8-sig", newline="") as handle:
            supplement = list(csv.DictReader(handle))

    subtotals = [row for row in sites if is_subtotal(row[site_key["發電站編號"]])]
    detail = [row for row in sites if not is_subtotal(row[site_key["發電站編號"]])]

    repairs = overrides.get("generation_repairs") or {}
    parsed = [
        (row, repair_generation(parse_generation(row[gen_key["發電量(度)"]]), repairs))
        for row in generation
    ]

    links = align_stations(
        [row[site_key["發電站名稱"]] for row in detail],
        [row[gen_key["發電站名稱"]] for row in generation],
        aliases=overrides.get("station_aliases") or {},
        supplement_names=[row["station_name"] for row in supplement],
    )
    aliases = overrides.get("station_aliases") or {}
    supplement_by_key = {normalize_station(row["station_name"]): row for row in supplement}
    site_rows: dict[str, list] = {}
    for row in detail:
        site_rows.setdefault(normalize_station(row[site_key["發電站名稱"]]), []).append(row)
    gen_rows: dict[str, list] = {}
    for row, value in parsed:
        key = normalize_station(row[gen_key["發電站名稱"]])
        gen_rows.setdefault(aliases.get(key, key), []).append((row, value))

    crosswalk = []
    for link in links:
        owned = site_rows.get(link.key, [])
        produced = gen_rows.get(link.key, [])
        extra = supplement_by_key.get(link.key)
        counties = {
            county for county in (parse_county(row[site_key["地址"]]) for row in owned) if county
        } or ({extra["county"]} if extra else set())
        energies = (
            {chinese_part(row[site_key["能源別"]]) for row in owned}
            or ({extra["energy_type"]} if extra else set())
            or {chinese_part(row[gen_key["能源別"]]) for row, _ in produced}
        )
        crosswalk.append(
            {
                "station_key": link.key,
                "status": link.status,
                "energy_type": "|".join(sorted(energies)),
                "county": "|".join(sorted(counties)),
                "site_name": link.site_name or "",
                "generation_name": link.generation_name or "",
                "site_count": len(owned),
                "capacity_kw": sum(int(row[site_key["裝置容量(瓩)"]]) for row in owned)
                or (int(extra["capacity_kw"]) if extra else 0),
                "months": len(produced),
                "total_kwh": sum(value.kwh for _, value in produced if value.kwh is not None),
                "missing_months": sum(value.kwh is None for _, value in produced),
            }
        )

    with (align_root / "re_station_crosswalk.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=RENEWABLE_CROSSWALK_COLUMNS)
        writer.writeheader()
        writer.writerows(crosswalk)

    status_counts: dict[str, int] = {}
    for _, value in parsed:
        status_counts[value.status] = status_counts.get(value.status, 0) + 1
    return {
        "sites": {
            "rows": len(sites),
            "detail": len(detail),
            "subtotal": len(subtotals),
            "capacity_kw": sum(int(row[site_key["裝置容量(瓩)"]]) for row in detail),
            "subtotal_capacity_kw": sum(int(row[site_key["裝置容量(瓩)"]]) for row in subtotals),
            "supplement": len(supplement),
        },
        "generation": {
            "rows": len(generation),
            "by_status": status_counts,
            "total_kwh": sum(value.kwh for _, value in parsed if value.kwh is not None),
        },
        "stations": summarize_alignment(links),
    }


def main() -> int:
    with (PROJECT_ROOT / "configs/align.yaml").open(encoding="utf-8") as handle:
        align_config = yaml.safe_load(handle)
    with (PROJECT_ROOT / "configs/outage_overrides.yaml").open(encoding="utf-8") as handle:
        overrides = yaml.safe_load(handle).get("overrides", {})
    with (PROJECT_ROOT / "taipower_align/crosswalk.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        crosswalk = parse_crosswalk(csv.DictReader(handle))
    with (PROJECT_ROOT / "taipower_align/units.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        units = list(csv.DictReader(handle))
    with (PROJECT_ROOT / "taipower_align/outage.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        outages = list(csv.DictReader(handle))

    ratio = align_config["ratio"]
    issues = validate_crosswalk(
        crosswalk,
        ratio_min=float(ratio["expected_min"]),
        ratio_max=float(ratio["expected_max"]),
    )
    outage_results = align_outage_rows(outages, units, overrides=overrides)
    outage_summary = summarize_outage_alignment(outage_results)
    pitfalls = generate_pitfalls(crosswalk, ratio_max=float(ratio["expected_max"]))
    with (PROJECT_ROOT / "configs/renewable_overrides.yaml").open(encoding="utf-8") as handle:
        renewable_overrides = yaml.safe_load(handle) or {}
    renewable = _renewable_alignment(renewable_overrides)
    report = {
        "status": "pass"
        if not issues
        and outage_summary["match_rate"] >= 0.9
        and renewable["stations"]["match_rate"] >= 0.9
        else "fail",
        "crosswalk": {"count": len(crosswalk), "issues": issues},
        "outage": outage_summary,
        "renewable": renewable,
        "pitfalls": {
            "count": len(pitfalls),
            "by_code": {
                code: sum(pitfall.code == code for pitfall in pitfalls)
                for code in sorted({pitfall.code for pitfall in pitfalls})
            },
        },
    }
    reports = PROJECT_ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "alignment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    unresolved = [
        f"{result.source_name}\t{result.status}\t{'|'.join(result.candidates)}"
        for result in outage_results
        if result.status not in {"matched", "override"}
    ]
    (reports / "outage_unmatched.txt").write_text(
        "source_name\tstatus\tcandidates\n" + "\n".join(dict.fromkeys(unresolved)) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
