# impl: FR-003-08, FR-003-09, FR-003-10
"""Поведінкові докази кластерів і діагностичні сигнали токена."""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from unmask.clusters.cluster import ClusterDraft
from unmask.clusters.config import ClusterConfig
from unmask.clusters.model import (
    DiagnosticKind,
    DiagnosticSignal,
    Evidence,
    EvidenceType,
    TimeBasis,
    Window,
    diagnostic_sort_key,
    evidence_sort_key,
)
from unmask.graph.model import EdgeKind, EdgeRef, NodeRole, ref_sort_key

__all__ = ["behavioral_evidence", "diagnostics"]


def _buyer_index(ingest_result) -> dict[str, object]:
    return {b.wallet: b for b in ingest_result.buyers}


def _link_amounts_to_members(draft: ClusterDraft, graph_result, config: ClusterConfig) -> list[tuple[int, str, EdgeRef, int]]:
    """Ребра-кандидати до членів: `(amount, receiver, ref, slot)`."""
    members = set(draft.wallets)
    return [(a, w, r, s) for a, w, r, s in _all_funding_rows(graph_result, config) if w in members]


def _all_funding_rows(graph_result, config: ClusterConfig) -> list[tuple[int, str, EdgeRef, int]]:
    """Усі ребра-кандидати графа: `(amount, receiver, ref, slot)` — один прохід для всього аналізу."""
    flagged = {f.address for f in graph_result.buyer_flags}
    assets = set(config.link_assets)
    buyers = {n.address for n in graph_result.graph.nodes if NodeRole.BUYER in n.roles}
    out = []
    for e in graph_result.graph.edges:
        if e.kind != EdgeKind.TRANSFER:
            continue
        if e.asset not in assets:
            continue
        if e.amount is None or e.amount < config.link_min_amount_lamports:
            continue
        if e.receiver not in buyers:
            continue
        if not config.link_through_flagged_buyers and (e.sender in flagged or e.receiver in flagged):
            continue
        for r in e.refs:
            out.append((e.amount, e.receiver, r, r.slot))
    return out


def _best_over_threshold(counts: Counter, threshold: int):
    candidates = [a for a, c in counts.items() if c > threshold]
    if not candidates:
        return None
    return min(candidates, key=lambda a: (-counts[a], a))


def behavioral_evidence(draft: ClusterDraft, graph_result, ingest_result, config: ClusterConfig,
                        *, _funding_rows=None) -> tuple[Evidence, ...]:
    """Поведінкові докази чернетки: `same_amounts/funding`, `same_amounts/first_buy_spent`, `same_slot`."""
    buyers = _buyer_index(ingest_result)
    found: list[Evidence] = []

    if _funding_rows is None:
        amounts = _link_amounts_to_members(draft, graph_result, config)
    elif isinstance(_funding_rows, dict):
        members = set(draft.wallets)
        amounts = [row for w in members for row in _funding_rows.get(w, ())]
    else:
        members = set(draft.wallets)
        amounts = [(a, w, r, s) for a, w, r, s in _funding_rows if w in members]
    if amounts:
        counts = Counter(a for a, _, _, _ in amounts)
        best_amount = _best_over_threshold(counts, config.same_amount_natural_max)
        if best_amount is not None:
            rows = [(w, r, s) for a, w, r, s in amounts if a == best_amount]
            wallets = tuple(sorted({w for w, _, _ in rows}))
            if len(wallets) >= 2:
                refs = tuple(sorted({r for _, r, _ in rows}, key=ref_sort_key))
                slots = [s for _, _, s in rows]
                found.append(Evidence(
                    type=EvidenceType.SAME_AMOUNTS, wallets=wallets, via=(), refs=refs,
                    window=Window(basis=TimeBasis.SLOT, start=min(slots), end=max(slots)),
                    basis=TimeBasis.SLOT, value=best_amount, detail="funding",
                    weight=float(config.evidence_weights.same_amounts),
                ))

    spent_rows: list[tuple[int, str, EdgeRef, int]] = []
    for wallet in draft.wallets:
        buyer = buyers.get(wallet)
        if buyer is None:
            continue
        for spend in buyer.spent:
            if str(spend.asset) != "sol":
                continue
            spent_rows.append((spend.amount, wallet,
                               EdgeRef(signature=buyer.first_buy_signature, slot=buyer.first_buy_slot,
                                       instruction_path=None),
                               buyer.first_buy_slot))
    if spent_rows:
        counts = Counter(a for a, _, _, _ in spent_rows)
        best = _best_over_threshold(counts, config.same_amount_natural_max)
        if best is not None:
            rows = [(w, r, s) for a, w, r, s in spent_rows if a == best]
            wallets = tuple(sorted({w for w, _, _ in rows}))
            if len(wallets) >= 2:
                refs = tuple(sorted({r for _, r, _ in rows}, key=ref_sort_key))
                slots = [s for _, _, s in rows]
                found.append(Evidence(
                    type=EvidenceType.SAME_AMOUNTS, wallets=wallets, via=(), refs=refs,
                    window=Window(basis=TimeBasis.SLOT, start=min(slots), end=max(slots)),
                    basis=TimeBasis.SLOT, value=best, detail="first_buy_spent",
                    weight=float(config.evidence_weights.same_amounts),
                ))

    slots_of = sorted({buyers[w].first_buy_slot for w in draft.wallets if w in buyers})
    if slots_of:
        width = config.same_slot_window_slots
        best_s: int | None = None
        best_n = 0
        for s in slots_of:
            n = sum(1 for w in draft.wallets if w in buyers and s <= buyers[w].first_buy_slot <= s + width)
            if n > best_n:
                best_n, best_s = n, s
        if best_s is not None and best_n > config.same_slot_natural_max:
            in_window = sorted(w for w in draft.wallets if w in buyers and best_s <= buyers[w].first_buy_slot <= best_s + width)
            if len(in_window) >= 2:
                refs = tuple(sorted(
                    {EdgeRef(signature=buyers[w].first_buy_signature, slot=buyers[w].first_buy_slot,
                             instruction_path=None) for w in in_window},
                    key=ref_sort_key))
                found.append(Evidence(
                    type=EvidenceType.SAME_SLOT, wallets=tuple(in_window), via=(), refs=refs,
                    window=Window(basis=TimeBasis.SLOT, start=best_s, end=best_s + width),
                    basis=TimeBasis.SLOT, value=best_s, detail=None,
                    weight=float(config.evidence_weights.same_slot),
                ))

    return tuple(sorted(found, key=evidence_sort_key))


def _components(drafts: Sequence[ClusterDraft], graph_result) -> dict[str, str]:
    comp: dict[str, str] = {}
    for d in drafts:
        root = min(d.wallets)
        for w in d.wallets:
            comp[w] = root
    buyers = {n.address for n in graph_result.graph.nodes if NodeRole.BUYER in n.roles}
    for b in sorted(buyers):
        comp.setdefault(b, b)
    return comp


def _criterion_value(hit) -> str:
    criterion = hit.criterion
    return criterion.value if hasattr(criterion, "value") else str(criterion)


def diagnostics(drafts: Sequence[ClusterDraft], graph_result, ingest_result, config: ClusterConfig,
                *, _funding_rows=None) -> tuple[DiagnosticSignal, ...]:
    """Діагностика токена: `flagged_buyers_excluded`, `same_amounts_unlinked`, `same_slot_unlinked`."""
    out: list[DiagnosticSignal] = []
    flagged = list(graph_result.buyer_flags)
    flagged_set = {f.address for f in flagged}
    if not config.link_through_flagged_buyers and flagged:
        wallets = tuple(sorted(f.address for f in flagged))
        detail = tuple(sorted(f"{f.address}:{_criterion_value(c)}" for f in flagged for c in f.criteria))
        out.append(DiagnosticSignal(
            kind=DiagnosticKind.FLAGGED_BUYERS_EXCLUDED, wallets=wallets,
            value=None, count=len(wallets), detail=detail,
        ))

    comp = _components(drafts, graph_result)
    buyers = _buyer_index(ingest_result)
    if _funding_rows is None:
        _funding_rows = _all_funding_rows(graph_result, config)
    if isinstance(_funding_rows, dict):
        funding_iter = (row for rows in _funding_rows.values() for row in rows)
    else:
        funding_iter = iter(_funding_rows)

    fund_groups: dict[int, set[str]] = {}
    for amount, receiver, _, _ in funding_iter:
        if receiver not in comp:
            continue
        fund_groups.setdefault(amount, set()).add(receiver)
    for amount in sorted(fund_groups, key=lambda a: (-len(fund_groups[a]), a)):
        wallets = fund_groups[amount]
        if len(wallets) <= config.same_amount_natural_max:
            continue
        if len({comp[w] for w in wallets}) < 2:
            continue
        out.append(DiagnosticSignal(
            kind=DiagnosticKind.SAME_AMOUNTS_UNLINKED, wallets=tuple(sorted(wallets)),
            value=amount, count=len(wallets), detail=("funding",),
        ))

    active = [w for w in comp if config.link_through_flagged_buyers or w not in flagged_set]
    spent_groups: dict[int, set[str]] = {}
    for w in active:
        b = buyers.get(w)
        if b is None:
            continue
        for spend in b.spent:
            if str(spend.asset) != "sol":
                continue
            spent_groups.setdefault(spend.amount, set()).add(w)
    for amount in sorted(spent_groups, key=lambda a: (-len(spent_groups[a]), a)):
        wallets = spent_groups[amount]
        if len(wallets) <= config.same_amount_natural_max:
            continue
        if len({comp[w] for w in wallets}) < 2:
            continue
        out.append(DiagnosticSignal(
            kind=DiagnosticKind.SAME_AMOUNTS_UNLINKED, wallets=tuple(sorted(wallets)),
            value=amount, count=len(wallets), detail=("first_buy_spent",),
        ))

    slot_of = {w: buyers[w].first_buy_slot for w in active if w in buyers}
    if slot_of:
        width = config.same_slot_window_slots
        seen_sets: set[frozenset[str]] = set()
        for s in sorted(set(slot_of.values())):
            in_w = {w for w, sl in slot_of.items() if s <= sl <= s + width}
            if len(in_w) <= config.same_slot_natural_max:
                continue
            if len({comp[w] for w in in_w}) < 2:
                continue
            key = frozenset(in_w)
            if key in seen_sets:
                continue
            seen_sets.add(key)
            out.append(DiagnosticSignal(
                kind=DiagnosticKind.SAME_SLOT_UNLINKED, wallets=tuple(sorted(in_w)),
                value=s, count=len(in_w), detail=(),
            ))

    return tuple(sorted(out, key=diagnostic_sort_key))
