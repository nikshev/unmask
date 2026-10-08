#!/usr/bin/env python3
# impl: FR-006-02, FR-006-05
"""006 screening worker (NOT committed): fixture-profile live run over a mint shard.

Usage: UNMASK_RPC_URL=<key> uv run python screen_worker.py <mints.jsonl> <out.csv> <key_index>
Reads ONLY mint strings from the input; appends one summary row per token.
Resume-safe: skips mints already present in out.csv. Never prints the URL.
"""
import csv
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unmask.delivery.cache import DeliveryCache
from unmask.delivery.service import DeliveryService
from unmask.clusters.config import load_cluster_config
from unmask.clusters.service import ClusterService
from unmask.graph.service import GraphService
from unmask.hubs.config import load_hub_config
from unmask.ingest.config import load_config
from unmask.ingest.rpc.http import HttpRpcSource
from unmask.ingest.service import IngestService


def main() -> int:
    mint_file, out_csv, key_index = sys.argv[1], sys.argv[2], sys.argv[3]
    mints = [json.loads(line)["mint"] for line in
             Path(mint_file).read_text().splitlines() if line.strip()]
    done = set()
    out = Path(out_csv)
    if out.exists():
        with open(out) as f:
            done = {row["mint"] for row in csv.DictReader(f)}
    new_file = not out.exists()
    cfg = dataclasses.replace(
        load_config(ROOT / "config" / "ingest.yaml"),
        first_buyers_n=30, max_signatures_per_wallet=30,
        collect_spl_inbound=False)
    hub = load_hub_config(ROOT / "config" / "hubs.yaml",
                          ROOT / "config" / "hub_addresses.yaml")
    clu = load_cluster_config(ROOT / "config" / "clusters.yaml")
    src = HttpRpcSource(os.environ["UNMASK_RPC_URL"], cfg.rpc,
                        commitment=cfg.commitment, profile="helius_free")
    svc = DeliveryService(IngestService(cfg, src), GraphService(hub),
                          ClusterService(clu), DeliveryCache())
    with open(out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mint", "risk_score", "band",
                                          "clusters_count", "max_share",
                                          "coordination_category", "wallets",
                                          "graph_status", "reasons",
                                          "elapsed_s", "error", "key_index"])
        if new_file:
            w.writeheader()
        for i, mint in enumerate(mints):
            if mint in done:
                continue
            t0 = time.time()
            last_risk = "?"
            try:
                doc = svc.analyze(mint)
                last_risk = doc.get("risk_score", "?")
                err = doc.get("error")
                clusters = doc.get("clusters", []) if err is None else []
                w.writerow({
                    "mint": mint,
                    "risk_score": doc.get("risk_score", ""),
                    "band": doc.get("band", ""),
                    "clusters_count": len(clusters),
                    "max_share": max([c["supply_share"] for c in clusters],
                                     default=0),
                    "coordination_category": doc.get("coordination_category", ""),
                    "wallets": doc.get("wallets_analyzed", ""),
                    "graph_status": (doc.get("provenance") or {}).get("graph_status", ""),
                    "reasons": ";".join((doc.get("provenance") or {})
                                        .get("completeness_reasons", [])),
                    "elapsed_s": round(time.time() - t0, 1),
                    "error": json.dumps(err, ensure_ascii=False) if err else "",
                    "key_index": key_index,
                })
            except Exception as exc:  # never die: record and continue
                w.writerow({"mint": mint, "elapsed_s": round(time.time() - t0, 1),
                            "error": f"EXC:{type(exc).__name__}",
                            "key_index": key_index,
                            "risk_score": "", "band": "", "clusters_count": "",
                            "max_share": "", "coordination_category": "",
                            "wallets": "", "graph_status": "", "reasons": ""})
            f.flush()
            print(f"[{key_index}] {i + 1}/{len(mints)} {mint[:8]} "
                  f"risk={last_risk} "
                  f"elapsed={round(time.time() - t0, 1)}s", flush=True)
    src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
