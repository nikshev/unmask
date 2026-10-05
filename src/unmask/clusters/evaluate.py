# impl: FR-003-22, FR-003-23
"""Оцінювальна команда: 001→002→003 на збережених результатах, таблиця й зведення критерію PRD (без мережі)."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path

import yaml

from unmask.clusters.config import load_cluster_config
from unmask.clusters.serialize import to_dict
from unmask.clusters.service import ClusterService
from unmask.graph.service import GraphService
from unmask.hubs.config import load_hub_config
from unmask.ingest.model import IngestResult
from unmask.ingest.serialize import from_dict

__all__ = ["evaluate", "main"]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CLUSTERS_CONFIG = REPO_ROOT / "config" / "clusters.yaml"
DEFAULT_HUBS_CONFIG = REPO_ROOT / "config" / "hubs.yaml"
DEFAULT_LISTS_CONFIG = REPO_ROOT / "config" / "hub_addresses.yaml"


def _load_manifest(fixtures_dir: Path) -> dict:
    path = fixtures_dir / "manifest.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"manifest.yaml: cannot read: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ValueError(f"manifest.yaml: invalid YAML ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
        raise ValueError("manifest.yaml: expected mapping with 'tokens' list")
    return data


def evaluate(fixtures_dir: Path, *, clusters_config: Path, hubs_config: Path,
             lists_config: Path | None) -> str:
    """Markdown-звіт по всіх токенах маніфесту в його порядку. Без мережі, годинника, абсолютних шляхів."""
    fixtures_dir = Path(fixtures_dir)
    manifest = _load_manifest(fixtures_dir)
    cluster_cfg = load_cluster_config(clusters_config)
    hub_cfg = load_hub_config(hubs_config, lists_config)
    graph_service = GraphService(hub_cfg)
    cluster_service = ClusterService(cluster_cfg)

    lines = [
        "# Оцінювання кластерів 003",
        "",
        f"clusters.yaml v{cluster_cfg.version} sha256 {cluster_cfg.digest}; "
        f"hubs.yaml v{hub_cfg.thresholds.version}; "
        f"hub_addresses.yaml v{hub_cfg.lists.version if hub_cfg.lists is not None else 'none'}; "
        f"ingest_config_version {manifest.get('collection', {}).get('ingest_config_version', '?')}; "
        "schema 003.1",
        "band_if_complete = computed_band: смуга за повних даних при тому самому risk_score (Q1 (а)); "
        "усі 9 результатів зібрані без аналізу делегованих купівель.",
        "",
        "| label | class | mint | clusters | largest_share | risk_score | computed_band | band | "
        "band_if_complete | status | warnings |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    insider_above = 0
    insider_total = 0
    clean_within = 0
    clean_total = 0
    clean_shown = 0
    for token in manifest["tokens"]:
        for key in ("label", "class", "file", "mint", "sha256"):
            if key not in token:
                raise ValueError(f"manifest.yaml: token entry missing {key!r}")
        path = fixtures_dir / token["file"]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"{token['file']}: cannot read: {exc}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != token["sha256"]:
            raise ValueError(f"{token['file']}: sha256 mismatch: file {digest} != manifest {token['sha256']}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"{token['file']}: invalid JSON ({type(exc).__name__})") from None
        try:
            ingest = from_dict(data)
        except Exception as exc:
            raise ValueError(f"{token['file']}: invalid ingest document ({type(exc).__name__})") from None
        if not isinstance(ingest, IngestResult):
            raise ValueError(f"{token['file']}: not an IngestResult (rejection)")
        graph_result = graph_service.analyze(ingest)
        result = cluster_service.analyze(graph_result, ingest)
        doc = to_dict(result)
        clusters = doc["clusters"]
        largest = f"{clusters[0]['share']:.4f}" if clusters else "0.0000"
        band_if_complete = doc["computed_band"]
        status = "empty" if doc["metadata"]["wallets_analyzed"] == 0 else doc["completeness"]["graph_status"]
        seen: list[str] = []
        for w in list(doc["completeness"]["graph_warnings"]) + [
                w for c in clusters for w in c["warnings"]]:
            if w not in seen:
                seen.append(w)
        warnings = ";".join(seen) if seen else "-"
        lines.append(
            f"| {token['label']} | {token['class']} | {token['mint'][:8]} | {len(clusters)} | "
            f"{largest} | {doc['risk_score']} | {doc['computed_band']} | {doc['band']} | "
            f"{band_if_complete} | {status} | {warnings} |"
        )
        if token["class"] == "insider":
            insider_total += 1
            if doc["risk_score"] > cluster_cfg.band_clean_max:
                insider_above += 1
        elif token["class"] == "clean":
            clean_total += 1
            if doc["risk_score"] <= cluster_cfg.band_clean_max:
                clean_within += 1
            if doc["band"] == "clean":
                clean_shown += 1
    met = "yes" if (insider_above >= 3 and clean_within >= 3) else "no"
    lines += [
        "",
        f"insider_above_clean {insider_above}/{insider_total}; clean_within_clean {clean_within}/{clean_total}; "
        f"clean_band_shown {clean_shown}/{clean_total}; prd_criterion_met {met}",
        "",
        "Застереження:",
        "- мітки MELT цінові/модельні, не підтверджене інсайдерство;",
        "- вибірка 9 токенів не статистична; калібрування на тому ж наборі, що й перевірка;",
        "- усі результати без аналізу делегованих купівель → смуга insufficient_data замість clean;",
        "- cln4 неповний на рівні 001 (missing 3, corrupt_data).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оцінювання кластерів 003 на збережених результатах (без мережі)")
    parser.add_argument("--fixtures", default=str(REPO_ROOT / "tests" / "fixtures" / "real"))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--clusters-config", default=str(DEFAULT_CLUSTERS_CONFIG))
    parser.add_argument("--hubs-config", default=str(DEFAULT_HUBS_CONFIG))
    parser.add_argument("--lists-config", default=str(DEFAULT_LISTS_CONFIG))
    args = parser.parse_args(argv)
    fixtures_dir = Path(args.fixtures)
    try:
        report = evaluate(fixtures_dir, clusters_config=Path(args.clusters_config),
                          hubs_config=Path(args.hubs_config), lists_config=Path(args.lists_config))
    except ValueError as exc:
        print(f"evaluate: error: {exc}", file=sys.stderr)
        return 2
    expected_path = fixtures_dir / "expected_table.md"
    if args.write:
        expected_path.write_text(report, encoding="utf-8")
        print(report)
        return 0
    if args.check:
        try:
            expected = expected_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"evaluate: error: cannot read expected_table.md: {exc}", file=sys.stderr)
            return 2
        if report == expected:
            return 0
        for line in difflib.unified_diff(expected.splitlines(), report.splitlines(), lineterm=""):
            print(line)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
