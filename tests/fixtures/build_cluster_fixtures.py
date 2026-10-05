# trace: ignore-file
"""Генератор фікстур `c_*` фічі 003 з незалежними оракулами (T-063, T-064).

Не імпортує `unmask`; «граф після відсікання» бере з оракулів генератора 002
(`build_graph_fixtures`: `Scenario`, `build_ingest`, `build_expected`).
Оракули 003: вікно-ланцюг, BFS, noisy-OR, share через `Fraction`, risk_score,
смуги, правило неповноти — незалежно від коду `src/unmask/clusters`.

Запуск: `uv run python tests/fixtures/build_cluster_fixtures.py [--check]`.
Правило стабільності: нові сценарії/оракули не змінюють уже закомічені `expected.json`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from fractions import Fraction
from pathlib import Path

_HERE = Path(__file__).parent
_G = _HERE / "build_graph_fixtures.py"
_spec = importlib.util.spec_from_file_location("build_graph_fixtures", _G)
bgf = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(bgf)

OUT_DIR = _HERE / "clusters"
SOL = 10**9

CLUSTER_CONFIG = {
    "version": 1,
    "link_min_amount_lamports": 10_000_000,
    "funding_window_seconds": 3600,
    "seconds_per_slot": 0.4,
    "link_through_flagged_buyers": False,
    "link_assets": ["sol"],
    "indirect_enabled": True,
    "same_amount_natural_max": 3,
    "same_slot_natural_max": 5,
    "same_slot_window_slots": 0,
    "evidence_weights": {
        "shared_funder": 0.6,
        "direct_transfer": 0.5,
        "delegated_buy": 0.6,
        "recovered_edge": 0.4,
        "indirect_link": 0.3,
        "same_amounts": 0.2,
        "same_slot": 0.15,
    },
    "slot_fallback_multiplier": 0.8,
    "artifact_buyer_share": 0.5,
    "artifact_confidence_multiplier": 0.5,
    "band_clean_max": 20,
    "band_suspicious_max": 50,
}

HUB_THRESHOLDS_V3 = {
    "degree_threshold": 100,
    "one_off_senders_share": 0.8,
    "one_off_min_senders": 50,
    "giant_component_warn_share": 0.5,
    "prune_off_curve": True,
    "prune_ingest_high_degree": True,
    "dust_amount_lamports": 1_000_000,
    "dust_min_fanout": 5,
}

LINK_TYPES = ("shared_funder", "direct_transfer", "delegated_buy", "recovered_edge", "indirect_link")


def _canonical_digest(data) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ref_key(r: dict):
    path = r["instruction_path"]
    key = () if path is None else tuple(int(p) for p in path.split("."))
    return (r["slot"], r["signature"], key)


def _window_chain(times_edges, max_gap: int):
    ordered = sorted(times_edges, key=lambda it: (it[0], json.dumps(it[1], sort_keys=True, default=str)))
    groups = [[ordered[0]]]
    prev = ordered[0][0]
    for t, payload in ordered[1:]:
        if t - prev > max_gap:
            groups.append([(t, payload)])
        else:
            groups[-1].append((t, payload))
        prev = t
    return [[p for _, p in g] for g in groups]


def _slot_window_seconds() -> int:
    from math import floor
    return floor(Fraction(CLUSTER_CONFIG["funding_window_seconds"], 1) / Fraction(str(CLUSTER_CONFIG["seconds_per_slot"])))


def oracle_links(ingest: dict, exp002: dict) -> list[dict]:
    """Зв'язки проходу 1 (+відновлені) за research R-2: незалежно від `links.py`."""
    cfg = CLUSTER_CONFIG
    buyers = {b["wallet"] for b in ingest["buyers"]}
    flagged = {f["address"] for f in exp002["prune"]["buyer_flags"]}
    after_nodes = set(exp002["prune"]["after"]["node_addresses"])
    edges = [e for e in exp002["graph"]["edges"]
             if e["sender"] in after_nodes and e["receiver"] in after_nodes]

    def ok_transfer(e: dict) -> bool:
        return (e["kind"] == "transfer" and e["asset"] in cfg["link_assets"]
                and e["amount"] is not None and e["amount"] >= cfg["link_min_amount_lamports"])

    def ends_ok(a: str, b: str) -> bool:
        return cfg["link_through_flagged_buyers"] or (a not in flagged and b not in flagged)

    def basis_of(group: list[dict]) -> str:
        return "slot" if any(e["first_time"] is None for e in group) else "block_time"

    def time_of(e: dict, basis: str) -> int:
        return e["first_slot"] if basis == "slot" else e["first_time"]

    links = []
    for e in edges:
        if e["kind"] == "transfer" and ok_transfer(e) and e["sender"] in buyers and e["receiver"] in buyers \
                and ends_ok(e["sender"], e["receiver"]):
            basis = "slot" if e["first_time"] is None else "block_time"
            start = e["first_slot"] if basis == "slot" else e["first_time"]
            end = e["last_slot"] if basis == "slot" else (e["last_time"] if e["last_time"] is not None else e["first_time"])
            links.append({"type": "direct_transfer", "buyers": sorted((e["sender"], e["receiver"])),
                          "via": [], "refs": sorted(e["refs"], key=_ref_key),
                          "window": {"basis": basis, "start": start, "end": end}, "basis": basis})

    by_sender: dict[str, list[dict]] = {}
    for e in edges:
        if not ok_transfer(e) or e["sender"] in buyers or e["receiver"] not in buyers:
            continue
        if not ends_ok(e["sender"], e["receiver"]):
            continue
        by_sender.setdefault(e["sender"], []).append(e)
    for sender in sorted(by_sender):
        group_edges = by_sender[sender]
        basis = basis_of(group_edges)
        gap = cfg["funding_window_seconds"] if basis == "block_time" else _slot_window_seconds()
        for group in _window_chain([(time_of(e, basis), e) for e in group_edges], gap):
            distinct = sorted({e["receiver"] for e in group})
            if len(distinct) < 2:
                continue
            refs = sorted({json.dumps(r, sort_keys=True) for e in group for r in e["refs"]})
            refs = sorted([json.loads(r) for r in refs], key=_ref_key)
            times = [time_of(e, basis) for e in group]
            links.append({"type": "shared_funder", "buyers": distinct, "via": [sender], "refs": refs,
                          "window": {"basis": basis, "start": min(times), "end": max(times)}, "basis": basis})

    for e in edges:
        if e["kind"] != "delegated_buy":
            continue
        if e["sender"] in buyers and e["receiver"] in buyers and ends_ok(e["sender"], e["receiver"]):
            basis = "slot" if e["first_time"] is None else "block_time"
            start = e["first_slot"] if basis == "slot" else e["first_time"]
            end = e["last_slot"] if basis == "slot" else (e["last_time"] if e["last_time"] is not None else e["first_time"])
            links.append({"type": "delegated_buy", "buyers": sorted((e["sender"], e["receiver"])),
                          "via": [], "refs": sorted(e["refs"], key=_ref_key),
                          "window": {"basis": basis, "start": start, "end": end}, "basis": basis})

    for record in exp002["prune"]["records"]:
        if not any(c["criterion"] == "dust_fanout" for c in record["criteria"]):
            continue
        addr = record["address"]
        cands = [e for e in record["incident_edges"]
                 if e["kind"] == "transfer" and e["sender"] == addr and e["receiver"] in buyers
                 and e["asset"] in cfg["link_assets"] and e["amount"] is not None
                 and e["amount"] >= cfg["link_min_amount_lamports"] and ends_ok(e["sender"], e["receiver"])]
        if not cands:
            continue
        basis = basis_of(cands)
        gap = cfg["funding_window_seconds"] if basis == "block_time" else _slot_window_seconds()
        for group in _window_chain([(time_of(e, basis), e) for e in cands], gap):
            distinct = sorted({e["receiver"] for e in group})
            if len(distinct) < 2:
                continue
            refs = sorted({json.dumps(r, sort_keys=True) for e in group for r in e["refs"]})
            refs = sorted([json.loads(r) for r in refs], key=_ref_key)
            times = [time_of(e, basis) for e in group]
            links.append({"type": "recovered_edge", "buyers": distinct, "via": [addr], "refs": refs,
                          "window": {"basis": basis, "start": min(times), "end": max(times)}, "basis": basis})

    links.sort(key=lambda l: (l["type"], l["via"], l["buyers"], _ref_key(l["refs"][0])))
    return links


def oracle_indirect(ingest: dict, exp002: dict, components: dict[str, str]) -> list[dict]:
    """Непрямі зв'язки C→M→P / C→P; лише між ≥2 компонентами проходу 1."""
    cfg = CLUSTER_CONFIG
    if not cfg["indirect_enabled"]:
        return []
    buyers = {b["wallet"] for b in ingest["buyers"]}
    flagged = {f["address"] for f in exp002["prune"]["buyer_flags"]}
    after_nodes = set(exp002["prune"]["after"]["node_addresses"])
    edges = [e for e in exp002["graph"]["edges"]
             if e["sender"] in after_nodes and e["receiver"] in after_nodes]

    def ok(e: dict) -> bool:
        return (e["kind"] == "transfer" and e["asset"] in cfg["link_assets"]
                and e["amount"] is not None and e["amount"] >= cfg["link_min_amount_lamports"])

    def buyer_ok(p: str) -> bool:
        return p in buyers and (cfg["link_through_flagged_buyers"] or p not in flagged)

    from_mid: dict[str, list[dict]] = {}
    for e in edges:
        if ok(e) and e["sender"] not in buyers and buyer_ok(e["receiver"]):
            from_mid.setdefault(e["sender"], []).append(e)
    mids_with_buyers = set(from_mid)

    by_source: dict[str, list[dict]] = {}
    for e in edges:
        if not ok(e) or e["sender"] in buyers:
            continue
        if e["receiver"] in buyers:
            if buyer_ok(e["receiver"]):
                by_source.setdefault(e["sender"], []).append(e)
        elif e["receiver"] in mids_with_buyers:
            by_source.setdefault(e["sender"], []).append(e)

    out = []
    for source in sorted(by_source):
        c_edges = by_source[source]
        basis = "slot" if any(e["first_time"] is None for e in c_edges) else "block_time"
        gap = cfg["funding_window_seconds"] if basis == "block_time" else _slot_window_seconds()

        def t_of(e: dict) -> int:
            return e["first_slot"] if basis == "slot" else e["first_time"]

        for group in _window_chain([(t_of(e), e) for e in c_edges], gap):
            reached: dict[str, list[dict]] = {}
            mids: set[str] = set()
            for ce in group:
                if ce["receiver"] in buyers:
                    reached.setdefault(ce["receiver"], []).append(ce)
                else:
                    for ie in from_mid.get(ce["receiver"], []):
                        reached.setdefault(ie["receiver"], []).append(ce)
                        reached[ie["receiver"]].append(ie)
                        mids.add(ce["receiver"])
            if len({components.get(p, p) for p in reached}) < 2 or len(reached) < 2:
                continue
            refs = sorted({json.dumps(r, sort_keys=True) for lst in reached.values() for e in lst for r in e["refs"]})
            refs = sorted([json.loads(r) for r in refs], key=_ref_key)
            times = [t_of(e) for e in group]
            out.append({"type": "indirect_link", "buyers": sorted(reached),
                        "via": sorted([source] + sorted(mids)), "refs": refs,
                        "window": {"basis": basis, "start": min(times), "end": max(times)}, "basis": basis})
    out.sort(key=lambda l: (l["type"], l["via"], l["buyers"], _ref_key(l["refs"][0])))
    return out


def oracle_behavioral(members: list[str], links: list[dict], ingest: dict, exp002: dict) -> list[dict]:
    """Поведінкові докази чернетки; незалежно від `behavior.py`."""
    cfg = CLUSTER_CONFIG
    buyers = {b["wallet"]: b for b in ingest["buyers"]}
    flagged = {f["address"] for f in exp002["prune"]["buyer_flags"]}
    member_set = set(members)
    after_nodes = set(exp002["prune"]["after"]["node_addresses"])
    edges = [e for e in exp002["graph"]["edges"]
             if e["sender"] in after_nodes and e["receiver"] in after_nodes]
    out = []

    funding: list[tuple[int, str, dict, int]] = []
    for e in edges:
        if e["kind"] != "transfer" or e["asset"] not in cfg["link_assets"]:
            continue
        if e["amount"] is None or e["amount"] < cfg["link_min_amount_lamports"]:
            continue
        if e["receiver"] not in member_set:
            continue
        if not cfg["link_through_flagged_buyers"] and (e["sender"] in flagged or e["receiver"] in flagged):
            continue
        for r in e["refs"]:
            funding.append((e["amount"], e["receiver"], r, r["slot"]))
    if funding:
        from collections import Counter
        counts = Counter(a for a, _, _, _ in funding)
        cands = [a for a, c in counts.items() if c > cfg["same_amount_natural_max"]]
        if cands:
            best = min(cands, key=lambda a: (-counts[a], a))
            rows = [(w, r, s) for a, w, r, s in funding if a == best]
            wallets = sorted({w for w, _, _ in rows})
            if len(wallets) >= 2:
                refs = sorted({json.dumps(r, sort_keys=True) for _, r, _ in rows})
                refs = sorted([json.loads(r) for r in refs], key=_ref_key)
                slots = [s for _, _, s in rows]
                out.append({"type": "same_amounts", "wallets": wallets, "via": [], "refs": refs,
                            "window": {"basis": "slot", "start": min(slots), "end": max(slots)},
                            "basis": "slot", "value": best, "detail": "funding",
                            "weight": cfg["evidence_weights"]["same_amounts"]})

    spent_rows: list[tuple[int, str, dict, int]] = []
    for w in members:
        b = buyers[w]
        for sp in b["spent"]:
            if sp["asset"] != "sol":
                continue
            spent_rows.append((sp["amount"], w,
                               {"signature": b["first_buy_signature"], "slot": b["first_buy_slot"],
                                "instruction_path": None},
                               b["first_buy_slot"]))
    if spent_rows:
        from collections import Counter
        counts = Counter(a for a, _, _, _ in spent_rows)
        cands = [a for a, c in counts.items() if c > cfg["same_amount_natural_max"]]
        if cands:
            best = min(cands, key=lambda a: (-counts[a], a))
            rows = [(w, r, s) for a, w, r, s in spent_rows if a == best]
            wallets = sorted({w for w, _, _ in rows})
            if len(wallets) >= 2:
                refs = sorted({json.dumps(r, sort_keys=True) for _, r, _ in rows})
                refs = sorted([json.loads(r) for r in refs], key=_ref_key)
                slots = [s for _, _, s in rows]
                out.append({"type": "same_amounts", "wallets": wallets, "via": [], "refs": refs,
                            "window": {"basis": "slot", "start": min(slots), "end": max(slots)},
                            "basis": "slot", "value": best, "detail": "first_buy_spent",
                            "weight": cfg["evidence_weights"]["same_amounts"]})

    slots_of = sorted({buyers[w]["first_buy_slot"] for w in members})
    width = cfg["same_slot_window_slots"]
    best_s, best_n = None, 0
    for s in slots_of:
        n = sum(1 for w in members if s <= buyers[w]["first_buy_slot"] <= s + width)
        if n > best_n:
            best_n, best_s = n, s
    if best_s is not None and best_n > cfg["same_slot_natural_max"]:
        in_w = sorted(w for w in members if best_s <= buyers[w]["first_buy_slot"] <= best_s + width)
        if len(in_w) >= 2:
            raw = [{"signature": buyers[w]["first_buy_signature"], "slot": buyers[w]["first_buy_slot"],
                    "instruction_path": None} for w in in_w]
            refs = sorted({json.dumps(r, sort_keys=True) for r in raw})
            refs = sorted([json.loads(r) for r in refs], key=_ref_key)
            out.append({"type": "same_slot", "wallets": in_w, "via": [], "refs": refs,
                        "window": {"basis": "slot", "start": best_s, "end": best_s + width},
                        "basis": "slot", "value": best_s, "detail": None,
                        "weight": cfg["evidence_weights"]["same_slot"]})
    out.sort(key=lambda e: (e["type"], e["via"], e["wallets"], _ref_key(e["refs"][0])))
    return out


def oracle_diagnostics(clusters_members: list[list[str]], ingest: dict, exp002: dict) -> list[dict]:
    cfg = CLUSTER_CONFIG
    comp: dict[str, str] = {}
    for members in clusters_members:
        root = min(members)
        for w in members:
            comp[w] = root
    buyers_idx = {b["wallet"]: b for b in ingest["buyers"]}
    for b in sorted(buyers_idx):
        comp.setdefault(b, b)
    flagged = list(exp002["prune"]["buyer_flags"])
    flagged_set = {f["address"] for f in flagged}
    out = []
    if not cfg["link_through_flagged_buyers"] and flagged:
        wallets = sorted(f["address"] for f in flagged)
        detail = sorted(f"{f['address']}:{c['criterion']}" for f in flagged for c in f["criteria"])
        out.append({"kind": "flagged_buyers_excluded", "wallets": wallets, "value": None,
                    "count": len(wallets), "detail": detail})

    after_nodes = set(exp002["prune"]["after"]["node_addresses"])
    edges = [e for e in exp002["graph"]["edges"]
             if e["sender"] in after_nodes and e["receiver"] in after_nodes]
    fund_groups: dict[int, set[str]] = {}
    for e in edges:
        if e["kind"] != "transfer" or e["asset"] not in cfg["link_assets"]:
            continue
        if e["amount"] is None or e["amount"] < cfg["link_min_amount_lamports"]:
            continue
        if e["receiver"] not in comp:
            continue
        if not cfg["link_through_flagged_buyers"] and (e["sender"] in flagged_set or e["receiver"] in flagged_set):
            continue
        fund_groups.setdefault(e["amount"], set()).add(e["receiver"])
    for amount in sorted(fund_groups, key=lambda a: (-len(fund_groups[a]), a)):
        wallets = fund_groups[amount]
        if len(wallets) <= cfg["same_amount_natural_max"]:
            continue
        if len({comp[w] for w in wallets}) < 2:
            continue
        out.append({"kind": "same_amounts_unlinked", "wallets": sorted(wallets), "value": amount,
                    "count": len(wallets), "detail": ["funding"]})

    active = [w for w in comp if cfg["link_through_flagged_buyers"] or w not in flagged_set]
    spent_groups: dict[int, set[str]] = {}
    for w in active:
        for sp in buyers_idx[w]["spent"]:
            if sp["asset"] != "sol":
                continue
            spent_groups.setdefault(sp["amount"], set()).add(w)
    for amount in sorted(spent_groups, key=lambda a: (-len(spent_groups[a]), a)):
        wallets = spent_groups[amount]
        if len(wallets) <= cfg["same_amount_natural_max"]:
            continue
        if len({comp[w] for w in wallets}) < 2:
            continue
        out.append({"kind": "same_amounts_unlinked", "wallets": sorted(wallets), "value": amount,
                    "count": len(wallets), "detail": ["first_buy_spent"]})

    slot_of = {w: buyers_idx[w]["first_buy_slot"] for w in active}
    width = cfg["same_slot_window_slots"]
    seen_sets: set[frozenset] = set()
    for s in sorted(set(slot_of.values())):
        in_w = {w for w, sl in slot_of.items() if s <= sl <= s + width}
        if len(in_w) <= cfg["same_slot_natural_max"]:
            continue
        if len({comp[w] for w in in_w}) < 2:
            continue
        if frozenset(in_w) in seen_sets:
            continue
        seen_sets.add(frozenset(in_w))
        out.append({"kind": "same_slot_unlinked", "wallets": sorted(in_w), "value": s,
                    "count": len(in_w), "detail": []})
    out.sort(key=lambda d: (d["kind"], -1 if d["value"] is None else d["value"], d["wallets"]))
    return out


def _bfs_components(buyers: list[str], links: list[dict]) -> list[set[str]]:
    adj: dict[str, set[str]] = {b: set() for b in buyers}
    for link in links:
        members = sorted(link["buyers"])
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                adj[members[i]].add(members[j])
                adj[members[j]].add(members[i])
    seen: set[str] = set()
    comps = []
    for b in sorted(buyers):
        if b in seen:
            continue
        stack, members = [b], set()
        seen.add(b)
        while stack:
            x = stack.pop()
            members.add(x)
            for y in sorted(adj[x]):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        if len(members) >= 2:
            comps.append(members)
    comps.sort(key=min)
    return comps


def _noisy_or(weights: list[float]) -> float:
    prod = 1.0
    for w in weights:
        prod *= 1.0 - w
    return round(1.0 - prod, 4)


def _weight(etype: str, basis: str) -> float:
    w = CLUSTER_CONFIG["evidence_weights"][etype]
    if etype in LINK_TYPES and basis == "slot":
        w = round(w * CLUSTER_CONFIG["slot_fallback_multiplier"], 4)
    return w


def oracle_result(s, ingest: dict, exp002: dict, declared: list[dict]) -> dict:
    """Повний очікуваний `ClusterResult` у формі схеми 003.1."""
    cfg = CLUSTER_CONFIG
    buyers_idx = {b["wallet"]: b for b in ingest["buyers"]}
    buyer_wallets = sorted(buyers_idx)
    links = oracle_links(ingest, exp002)
    comps = _bfs_components(buyer_wallets, links)
    comp_of = {}
    for c in comps:
        root = min(c)
        for w in c:
            comp_of[w] = root
    for b in buyer_wallets:
        comp_of.setdefault(b, b)
    indirect = oracle_indirect(ingest, exp002, comp_of)
    if indirect:
        links = sorted(links + indirect,
                       key=lambda l: (l["type"], l["via"], l["buyers"], _ref_key(l["refs"][0])))
        comps = _bfs_components(buyer_wallets, links)

    assert len(comps) == len(declared), (
        f"{s.name}: oracle found {len(comps)} clusters, declared {len(declared)}")
    for members, decl in zip([sorted(c) for c in comps], declared):
        assert members == sorted(decl["members"]), f"{s.name}: members {members} != declared {decl['members']}"
        got_types = sorted({l["type"] for l in links if set(l["buyers"]) <= set(members)})
        assert got_types == sorted(decl["link_types"]), f"{s.name}: link types {got_types} != {decl['link_types']}"

    denominator = sum(b["received_amount"] for b in ingest["buyers"])
    clusters = []
    for members in [sorted(c) for c in comps]:
        group_links = [l for l in links if set(l["buyers"]) <= set(members)]
        evidences = []
        for l in group_links:
            evidences.append({"type": l["type"], "wallets": sorted(l["buyers"]), "via": list(l["via"]),
                              "refs": l["refs"],
                              "window": dict(l["window"]), "basis": l["basis"],
                              "value": None, "detail": None, "weight": _weight(l["type"], l["basis"])})
        for e in oracle_behavioral(members, group_links, ingest, exp002):
            evidences.append(e)
        evidences.sort(key=lambda e: (e["type"], e["via"], e["wallets"], _ref_key(e["refs"][0])))

        warnings = []
        plain = _noisy_or([e["weight"] for e in evidences])
        artifact = (
            denominator > 0
            and len(members) / len(buyer_wallets) > cfg["artifact_buyer_share"]
            and ("giant_component" in exp002["report"]["warnings"]
                 or sum(1 for l in group_links if l["type"] == "indirect_link")
                 > sum(1 for l in group_links if l["type"] != "indirect_link"))
        )
        conf = round(plain * cfg["artifact_confidence_multiplier"], 4) if artifact else plain
        if artifact:
            warnings.append("possible_pruning_artifact")
        if any(e["basis"] == "slot" and e["type"] in LINK_TYPES for e in evidences):
            warnings.append("slot_time_fallback")
        warnings.sort()
        num = sum(buyers_idx[w]["received_amount"] for w in members)
        share = round(float(Fraction(num, denominator)), 4) if denominator else 0.0
        by_rank = sorted(members, key=lambda w: buyers_idx[w]["rank"])
        clusters.append({
            "cluster_id": "c-" + hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:16],
            "members": [{"wallet": w, "buyer_rank": buyers_idx[w]["rank"],
                         "received_amount": buyers_idx[w]["received_amount"]} for w in by_rank],
            "share_numerator": num, "share_denominator": denominator, "share": share,
            "confidence": conf, "evidence": evidences, "warnings": warnings,
        })
    clusters.sort(key=lambda c: (-c["share_numerator"], c["cluster_id"]))

    diags = oracle_diagnostics([sorted(c) for c in comps], ingest, exp002)

    comp = exp002["completeness"]
    completeness = {
        "graph_status": comp["status"],
        "ingest_status": comp["ingest_status"],
        "missing_count": len(comp["missing"]),
        "delegated_complete": comp["delegated_complete"],
        "delegated_reason": comp["delegated_reason"],
        "graph_warnings": sorted(exp002["report"]["warnings"]),
        "can_be_clean": comp["status"] == "complete" and len(buyer_wallets) >= 1,
    }
    score = sum(c["share"] * c["confidence"] for c in clusters)
    import math as _math
    risk = _math.floor(100 * score + 0.5)
    if risk <= cfg["band_clean_max"]:
        computed = "clean"
    elif risk <= cfg["band_suspicious_max"]:
        computed = "suspicious"
    else:
        computed = "high_concentration"
    n = len(buyer_wallets)
    if n == 0:
        band, reasons = "insufficient_data", ["empty_input"]
    elif computed == "clean" and not completeness["can_be_clean"]:
        band, reasons = "insufficient_data", []
        if comp["ingest_status"] != "complete":
            reasons.append("ingest_incomplete")
        if len(comp["missing"]) > 0:
            reasons.append(f"missing_histories:{len(comp['missing'])}")
        if not comp["delegated_complete"]:
            reasons.append("delegated_incomplete")
    elif computed == "clean":
        band = "clean"
        reasons = ["no_clusters_on_complete_data"] if not clusters else ["clusters_within_clean_threshold"]
    else:
        band, reasons = computed, ["clusters_share_weighted"]

    gm = exp002
    meta_ingest = ingest["metadata"]
    return {
        "metadata": {
            "mint": meta_ingest["mint"], "schema_version": "003.1", "cluster_config_version": 1,
            "thresholds": {k: v for k, v in cfg.items() if k != "version"},
            "graph_schema_version": "002.1", "hub_config_version": 3,
            "address_lists_version": 1, "lists_applied": True,
            "ingest_config_version": meta_ingest["config_version"],
            "ingest_analyzed_at": meta_ingest["analyzed_at"], "ingest_source": meta_ingest["source"],
            "wallets_analyzed": len(buyer_wallets), "share_denominator": denominator,
            "share_denominator_kind": "analyzed_buyers_received_amount",
        },
        "clusters": clusters, "diagnostics": diags, "risk_score": risk,
        "computed_band": computed, "band": band, "band_reasons": reasons,
        "completeness": completeness,
    }


# ---------------------------------------------------------------------------------------
# Сценарії
# ---------------------------------------------------------------------------------------

THRESHOLDS_V3 = dict(bgf.DEFAULT_THRESHOLDS, one_off_min_senders=50)


def _base(name: str, desc: str, n: int) -> "bgf.Scenario":
    return bgf.Scenario(name, desc, first_buyers_n=n, thresholds=dict(THRESHOLDS_V3), lists=bgf.DEFAULT_LISTS)


def sc_shared():
    s = _base("c_shared", "S1→P1..P3 у вікні 1200 с; S1→P4 поза вікном; S2→P5,P6 з розривом; P7,P8 одинаки", 8)
    for label in ("S1", "S2", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"):
        s.wallet(label)
    buys = [("P1", 200, 600_000_000, 1_000_000_000), ("P2", 211, 700_000_000, 2_000_000_000),
            ("P3", 222, 800_000_000, 3_000_000_000), ("P4", 233, 900_000_000, 4_000_000_000),
            ("P5", 244, 1_000_000_000, 5_000_000_000), ("P6", 255, 1_100_000_000, 6_000_000_000),
            ("P7", 266, 1_200_000_000, 7_000_000_000), ("P8", 277, 1_300_000_000, 8_000_000_000)]
    for label, slot, spent, received in buys:
        s.buy(label, slot, spent=[("sol", spent)], received=received)
    s.transfer("s1_p1", "0", "S1", "P1", 100, 500_000_000, 1)
    s.transfer("s1_p2", "0", "S1", "P2", 700, 600_000_000, 1)
    s.transfer("s1_p3", "0", "S1", "P3", 1300, 700_000_000, 1)
    s.transfer("s1_p4", "0", "S1", "P4", 6000, 800_000_000, 1)
    s.transfer("s2_p5", "0", "S2", "P5", 150, 900_000_000, 1)
    s.transfer("s2_p6", "0", "S2", "P6", 5000, 950_000_000, 1)
    s.declare(["S1", "S2"], ("funder",), 1)
    s.declare(["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"], ("buyer",), 0)
    declared = [{"members": ["P1", "P2", "P3"], "link_types": ["shared_funder"]}]
    return s, declared


def sc_two_clusters():
    s = _base("c_two_clusters", "S1→P1,P2 і S2→P3,P4 у вікні; P5,P6 одинаки", 6)
    for label in ("S1", "S2", "P1", "P2", "P3", "P4", "P5", "P6"):
        s.wallet(label)
    buys = [("P1", 200, 600_000_000, 1_000_000_000), ("P2", 211, 700_000_000, 2_000_000_000),
            ("P3", 222, 800_000_000, 3_000_000_000), ("P4", 233, 900_000_000, 4_000_000_000),
            ("P5", 244, 1_000_000_000, 5_000_000_000), ("P6", 255, 1_100_000_000, 6_000_000_000)]
    for label, slot, spent, received in buys:
        s.buy(label, slot, spent=[("sol", spent)], received=received)
    s.transfer("s1_p1", "0", "S1", "P1", 100, 1_000_000_000, 1)
    s.transfer("s1_p2", "0", "S1", "P2", 500, 1_100_000_000, 1)
    s.transfer("s2_p3", "0", "S2", "P3", 200, 1_200_000_000, 1)
    s.transfer("s2_p4", "0", "S2", "P4", 600, 1_300_000_000, 1)
    s.declare(["S1", "S2"], ("funder",), 1)
    s.declare(["P1", "P2", "P3", "P4", "P5", "P6"], ("buyer",), 0)
    declared = [{"members": ["P1", "P2"], "link_types": ["shared_funder"]},
                {"members": ["P3", "P4"], "link_types": ["shared_funder"]}]
    return s, declared


def sc_incomplete():
    s, declared = sc_shared()
    s.name = "c_incomplete"
    s.description = "як c_shared, але з одним missing (неповний збір)"
    s.wallet("MX")
    s.missing.append({"wallet": "MX", "depth": 1, "reason": "rate_limited", "detail": "probe"})
    return s, declared


def sc_empty():
    s = _base("c_empty", "порожній вхід: 0 покупців", 1)
    declared = []
    return s, declared


def sc_direct_flagged():
    s = _base("c_direct_flagged", "P1..P5 платять PDA-покупцю POOL (позначений, зв'язку немає); P7→P8 прямий переказ", 12)
    s.pda("POOL")
    for label in ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11"):
        s.wallet(label)
    labels = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11", "POOL"]
    for i, label in enumerate(labels):
        s.buy(label, 200 + 11 * i, spent=[("sol", 600_000_000 + 101_000_003 * i)], received=1_000_000_000 * (i + 1))
    for i, label in enumerate(["P1", "P2", "P3", "P4", "P5"]):
        s.transfer(f"px_pool{i}", "0", label, "POOL", 100 + i, 210_000_000 + 10_000_000 * i, 1)
    s.transfer("p7_p8", "0", "P7", "P8", 110, 500_000_000, 1)
    s.declare(["P1", "P2", "P3", "P4", "P5", "P7"], ("buyer", "funder"), 0)
    s.declare(["P6", "P8", "P9", "P10", "P11", "POOL"], ("buyer",), 0)
    s.flags = {"POOL": ("known_list:address_type:off_curve",)}
    declared = [{"members": ["P7", "P8"], "link_types": ["direct_transfer"]}]
    return s, declared


def sc_delegated():
    s = _base("c_delegated", "A→B делегована (B не покупець, кластера немає); C→D делегована + переказ", 5)
    for label in ("A", "B", "C", "D", "E", "F"):
        s.wallet(label)
    for i, (label, slot) in enumerate([("A", 200), ("C", 211), ("D", 222), ("E", 233), ("F", 244)]):
        s.buy(label, slot, spent=[("sol", 600_000_000 + 97_000_011 * i)], received=1_000_000_000 * (5 - i))
    s.delegated("d_ab", "A", "B", 150)
    s.delegated("d_cd", "C", "D", 160)
    s.transfer("c_d", "0", "C", "D", 165, 500_000_000, 1)
    s.declare(["A"], ("buyer", "delegated_payer"), 0)
    s.declare(["B"], ("delegated_receiver",), 0)
    s.declare(["C"], ("buyer", "funder", "delegated_payer"), 0)
    s.declare(["D"], ("buyer", "delegated_receiver"), 0)
    s.declare(["E", "F"], ("buyer",), 0)
    declared = [{"members": ["C", "D"], "link_types": ["delegated_buy", "direct_transfer"]}]
    return s, declared


def sc_below_min():
    s = _base("c_below_min", "усі перекази нижче порога зв'язку: кластерів немає", 6)
    for label in ("F", "P1", "P2", "P3", "P4", "P5", "P6"):
        s.wallet(label)
    for i, label in enumerate(["P1", "P2", "P3", "P4", "P5", "P6"]):
        s.buy(label, 200 + 13 * i, spent=[("sol", 600_000_000 + 89_000_007 * i)], received=1_000_000_000 * (i + 1))
    s.transfer("f_p1", "0", "F", "P1", 100, 500_000, 1)
    s.transfer("f_p2", "0", "F", "P2", 101, 500_000, 1)
    s.transfer("f_p3", "0", "F", "P3", 102, 500_000, 1)
    s.transfer("p4_p5", "0", "P4", "P5", 110, 9_999_999, 1)
    s.declare(["F"], ("funder",), 1)
    s.declare(["P4"], ("buyer", "funder"), 0)
    s.declare(["P1", "P2", "P3", "P5", "P6"], ("buyer",), 0)
    declared = []
    return s, declared


def sc_single():
    s = _base("c_single", "один покупець без переказів (giant_component 1/1)", 1)
    s.wallet("P1")
    s.buy("P1", 200, spent=[("sol", 600_000_000)], received=1_000_000_000)
    s.declare(["P1"], ("buyer",), 0)
    declared = []
    return s, declared




def sc_behavior():
    s = _base("c_behavior", "A: S→P1..P7 у вікні (4×2.0 SOL + 3 різні), усі в слоті s, spent P1..P5 1.5 SOL; "
                             "Q1..Q4/R1..R3 без переказів, spent 0.7/0.9 SOL, усі в слоті s2", 14)
    s.wallet("S")
    for label in ("P1", "P2", "P3", "P4", "P5", "P6", "P7",
                  "Q1", "Q2", "Q3", "Q4", "R1", "R2", "R3"):
        s.wallet(label)
    for i, label in enumerate(["P1", "P2", "P3", "P4", "P5", "P6", "P7"]):
        spent = 1_500_000_000 if i < 5 else (1_600_000_000 if i == 5 else 1_700_000_000)
        s.buy(label, 300, spent=[("sol", spent)], received=1_000_000_000)
    for label in ("Q1", "Q2", "Q3", "Q4"):
        s.buy(label, 400, spent=[("sol", 700_000_000)], received=1_000_000_000)
    for label in ("R1", "R2", "R3"):
        s.buy(label, 400, spent=[("sol", 900_000_000)], received=1_000_000_000)
    amounts = [2_000_000_000] * 4 + [2_100_000_000, 2_200_000_000, 2_300_000_000]
    for i, label in enumerate(["P1", "P2", "P3", "P4", "P5", "P6", "P7"]):
        s.transfer(f"s_p{i}", "0", "S", label, 100 + i, amounts[i], 1)
    s.declare(["S"], ("funder",), 1)
    s.declare(["P1", "P2", "P3", "P4", "P5", "P6", "P7",
               "Q1", "Q2", "Q3", "Q4", "R1", "R2", "R3"], ("buyer",), 0)
    declared = [{"members": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"], "link_types": ["shared_funder"]}]
    return s, declared



def sc_diamond():
    s = _base("c_diamond", "ромб C→A→P1, C→B→P2 (непрямий зв'язок); варіант з хабом C'→A'(PDA)→P3: одинаки", 6)
    s.pda("A2")
    for label in ("C", "A", "B", "P1", "P2", "C2", "B2", "P3", "P4", "Q1", "Q2"):
        s.wallet(label)
    for i, label in enumerate(["P1", "P2", "P3", "P4", "Q1", "Q2"][:6]):
        s.buy(label, 200 + 17 * i, spent=[("sol", 600_000_000 + 103_000_007 * i)],
              received=1_000_000_000)
    s.transfer("c_a", "0", "C", "A", 100, 1_000_000_000, 2)
    s.transfer("c_b", "0", "C", "B", 150, 1_200_000_000, 2)
    s.transfer("a_p1", "0", "A", "P1", 200, 1_100_000_000, 1)
    s.transfer("b_p2", "0", "B", "P2", 210, 1_300_000_000, 1)
    s.transfer("c2_a2", "0", "C2", "A2", 110, 1_000_000_000, 2)
    s.transfer("c2_b2", "0", "C2", "B2", 160, 1_200_000_000, 2)
    s.transfer("a2_p3", "0", "A2", "P3", 220, 1_100_000_000, 1)
    s.transfer("b2_p4", "0", "B2", "P4", 230, 1_300_000_000, 1)
    s.declare(["C", "C2"], ("funder",), 2)
    s.declare(["A", "B", "B2"], ("funder",), 1)
    s.declare(["A2"], ("funder",), 1)
    s.declare(["P1", "P2", "P3", "P4", "Q1", "Q2"], ("buyer",), 0)
    s.hubs = {"A2": ("known_list:address_type:off_curve",)}
    s.flags = {}
    declared = [{"members": ["P1", "P2"], "link_types": ["indirect_link"]}]
    return s, declared


def sc_giant():
    s = _base("c_giant", "S→P1..P4; C1→M1..M3→P5,P6,P1; C2→M4..M8→P7..P10,P2: усі 10 в одному кластері, артефакт", 10)
    funders = ["S", "C1", "M1", "M2", "M3", "C2", "M4", "M5", "M6", "M7", "M8"]
    buyers = [f"P{i}" for i in range(1, 11)]
    for label in funders + buyers:
        s.wallet(label)
    for i, label in enumerate(buyers):
        s.buy(label, 300 + 19 * i, spent=[("sol", 600_000_000 + 107_000_011 * i)],
              received=1_000_000_000 + 1_000_000 * i)
    s.transfer("s_p1", "0", "S", "P1", 100, 1_000_000_000, 1)
    s.transfer("s_p2", "0", "S", "P2", 140, 1_100_000_000, 1)
    s.transfer("s_p3", "0", "S", "P3", 180, 1_200_000_000, 1)
    s.transfer("s_p4", "0", "S", "P4", 220, 1_300_000_000, 1)
    s.transfer("c1_m1", "0", "C1", "M1", 300, 2_000_000_000, 2)
    s.transfer("c1_m2", "0", "C1", "M2", 330, 2_100_000_000, 2)
    s.transfer("c1_m3", "0", "C1", "M3", 360, 2_200_000_000, 2)
    s.transfer("m1_p5", "0", "M1", "P5", 400, 4_000_000_000, 1)
    s.transfer("m2_p6", "0", "M2", "P6", 410, 4_100_000_000, 1)
    s.transfer("m3_p1", "0", "M3", "P1", 420, 4_200_000_000, 1)
    s.transfer("c2_m4", "0", "C2", "M4", 500, 3_000_000_000, 2)
    s.transfer("c2_m5", "0", "C2", "M5", 520, 3_100_000_000, 2)
    s.transfer("c2_m6", "0", "C2", "M6", 540, 3_200_000_000, 2)
    s.transfer("c2_m7", "0", "C2", "M7", 560, 3_300_000_000, 2)
    s.transfer("c2_m8", "0", "C2", "M8", 580, 3_400_000_000, 2)
    s.transfer("m4_p7", "0", "M4", "P7", 600, 4_300_000_000, 1)
    s.transfer("m5_p8", "0", "M5", "P8", 610, 4_400_000_000, 1)
    s.transfer("m6_p9", "0", "M6", "P9", 620, 4_500_000_000, 1)
    s.transfer("m7_p10", "0", "M7", "P10", 630, 4_600_000_000, 1)
    s.transfer("m8_p2", "0", "M8", "P2", 640, 4_700_000_000, 1)
    s.declare(["S"], ("funder",), 1)
    s.declare(["C1", "C2"], ("funder",), 2)
    s.declare(["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8"], ("funder",), 1)
    s.declare(buyers, ("buyer",), 0)
    declared = [{"members": buyers, "link_types": ["indirect_link", "shared_funder"]}]
    return s, declared


def sc_all_one():
    s = _base("c_all_one", "S фінансує всіх 6 покупців у вікні: один кластер, giant_component, артефакт", 6)
    s.wallet("S")
    for i in range(1, 7):
        s.wallet(f"P{i}")
    for i in range(1, 7):
        s.buy(f"P{i}", 200 + 23 * i, spent=[("sol", 600_000_000 + 109_000_013 * i)],
              received=1_000_000_000 + 2_000_000 * i)
    for i in range(1, 7):
        s.transfer(f"s_p{i}", "0", "S", f"P{i}", 100 + 50 * i, 1_000_000_000 + 100_000_000 * i, 1)
    s.declare(["S"], ("funder",), 1)
    s.declare([f"P{i}" for i in range(1, 7)], ("buyer",), 0)
    declared = [{"members": [f"P{i}" for i in range(1, 7)], "link_types": ["shared_funder"]}]
    return s, declared



def sc_no_time():
    s = _base("c_no_time", "S→P1..P3, один переказ без block_time: уся група в слотах, вага 0.48", 6)
    for label in ("S", "P1", "P2", "P3", "P4", "P5", "P6"):
        s.wallet(label)
    for i, label in enumerate(["P1", "P2", "P3", "P4", "P5", "P6"]):
        s.buy(label, 200 + 29 * i, spent=[("sol", 600_000_000 + 113_000_017 * i)],
              received=1_000_000_000)
    s.transfer("s_p1", "0", "S", "P1", 100, 500_000_000, 1)
    s.transfer("s_p2", "0", "S", "P2", 150, 600_000_000, 1, block_time=None)
    s.transfer("s_p3", "0", "S", "P3", 200, 700_000_000, 1)
    s.declare(["S"], ("funder",), 1)
    s.declare(["P1", "P2", "P3", "P4", "P5", "P6"], ("buyer",), 0)
    declared = [{"members": ["P1", "P2", "P3"], "link_types": ["shared_funder"]}]
    return s, declared


def sc_recovered():
    s = _base("c_recovered", "змішаний X (пил + 3×0.5 SOL) відсічений dust_fanout: справжні ребра → recovered_edge; "
                             "W увесь пиловий: нічого відновлювати", 10)
    for label in ("X", "W", "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10"):
        s.wallet(label)
    for i in range(1, 11):
        s.buy(f"B{i}", 200 + 31 * i, spent=[("sol", 600_000_000 + 127_000_019 * i)],
              received=1_000_000_000)
    s.transfer("x_b1", "0", "X", "B1", 100, 500_000, 1)
    s.transfer("x_b2", "0", "X", "B2", 101, 500_000, 1)
    s.transfer("x_b3", "0", "X", "B3", 102, 500_000, 1)
    s.transfer("x_b4", "0", "X", "B4", 103, 500_000, 1)
    s.transfer("x_b5", "0", "X", "B5", 110, 500_000_000, 1)
    s.transfer("x_b6", "0", "X", "B6", 120, 500_000_000, 1)
    s.transfer("x_b7", "0", "X", "B7", 130, 500_000_000, 1)
    s.transfer("w_b8", "0", "W", "B8", 140, 999_999, 1)
    s.transfer("w_b9", "0", "W", "B9", 141, 999_999, 1)
    s.transfer("w_b10", "0", "W", "B10", 142, 999_999, 1)
    s.transfer("w_b1", "0", "W", "B1", 143, 999_999, 1)
    s.transfer("w_b2", "0", "W", "B2", 144, 999_999, 1)
    s.declare(["X", "W"], ("funder",), 1)
    s.declare([f"B{i}" for i in range(1, 11)], ("buyer",), 0)
    s.hubs = {"W": ("dust_fanout:measured",), "X": ("dust_fanout:measured",)}
    s.flags = {}
    declared = [{"members": ["B5", "B6", "B7"], "link_types": ["recovered_edge"]}]
    return s, declared

SCENARIOS = {
    "c_shared": sc_shared,
    "c_two_clusters": sc_two_clusters,
    "c_incomplete": sc_incomplete,
    "c_empty": sc_empty,
    "c_direct_flagged": sc_direct_flagged,
    "c_delegated": sc_delegated,
    "c_below_min": sc_below_min,
    "c_single": sc_single,
    "c_behavior": sc_behavior,
    "c_diamond": sc_diamond,
    "c_giant": sc_giant,
    "c_all_one": sc_all_one,
    "c_no_time": sc_no_time,
    "c_recovered": sc_recovered,
}


def dump(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def build_all() -> dict[str, str]:
    assert _canonical_digest(CLUSTER_CONFIG) == "c8a0636918c1d08f1af87af88da4117a55c4ecbc4760adabe57e621fa7426b05", \
        "cluster_config generator diverged from shipped clusters.yaml v1"
    out = {}
    for name, builder in SCENARIOS.items():
        s, declared_raw = builder()
        assert s.name == name, f"builder {name} produced scenario {s.name}"
        ingest = bgf.build_ingest(s)
        exp002 = bgf.build_expected(s, ingest)
        declared = [{"members": sorted(s.a(m) for m in d["members"]),
                     "link_types": sorted(d["link_types"])} for d in declared_raw]
        result = oracle_result(s, ingest, exp002, declared)
        label_of = {addr: label for label, addr in s.wallets.items()}
        notes = {
            "scenario": name,
            "description": s.description,
            "declared_clusters": [
                {"members": sorted(label_of[a] for a in d["members"]), "link_types": d["link_types"]}
                for d in declared
            ],
        }
        out[f"{name}/ingest.json"] = dump(ingest)
        out[f"{name}/expected.json"] = dump({
            "config": CLUSTER_CONFIG,
            "hub_config": {"version": 3, "thresholds": HUB_THRESHOLDS_V3, "lists": bgf.DEFAULT_LISTS},
            "result": result,
            "notes": notes,
        })
    return out


def main(argv: list[str]) -> int:
    built = build_all()
    if "--check" in argv:
        stale = [rel for rel, text in built.items()
                 if not (OUT_DIR / rel).exists() or (OUT_DIR / rel).read_text(encoding="utf-8") != text]
        existing = ({p.relative_to(OUT_DIR).as_posix() for p in OUT_DIR.rglob("*") if p.is_file()}
                    if OUT_DIR.exists() else set())
        extra = sorted(existing - set(built))
        if stale:
            print("stale:", ", ".join(stale))
        if extra:
            print("extra:", ", ".join(extra))
        return 1 if stale or extra else 0
    for rel, text in built.items():
        path = OUT_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
