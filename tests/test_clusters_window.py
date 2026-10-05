# verifies: FR-003-02
"""Тести вікна-ланцюга й базису часу (T-065)."""

from __future__ import annotations

import itertools

import pytest

from unmask.clusters.config import load_cluster_config
from unmask.clusters.links import basis_for, edge_time, group_by_window, window_of, window_slots
from unmask.clusters.model import TimeBasis
from unmask.graph.model import Edge, EdgeKind, EdgeRef
from pathlib import Path


def _edge(sig: str, slot: int, time: int | None, sender="s", receiver="p") -> Edge:
    refs = (EdgeRef(signature=sig, slot=slot, instruction_path="0"),)
    return Edge(kind=EdgeKind.TRANSFER, sender=sender, receiver=receiver, asset="sol",
                amount=10_000_000, decimals=None, count=1, first_slot=slot, last_slot=slot,
                first_time=time, last_time=time, refs=refs)


def test_gap_at_w_minus_one_w_and_w_plus_one() -> None:
    items = [(0, "a"), (3599, "b"), (7199, "c")]
    assert group_by_window(items, 3600) == (("a", "b", "c"),)
    items2 = [(0, "a"), (3600, "b")]
    assert group_by_window(items2, 3600) == (("a", "b"),)
    items3 = [(0, "a"), (3601, "b")]
    assert group_by_window(items3, 3600) == (("a",), ("b",))
    assert group_by_window([(0, "a"), (1, "b")], 1) == (("a", "b"),)
    assert group_by_window([(0, "a"), (2, "b")], 1) == (("a",), ("b",))


def test_chain_not_span() -> None:
    assert group_by_window([(0, "a"), (3000, "b"), (6000, "c")], 3600) == (("a", "b", "c"),)


def test_two_waves_split_like_ins0() -> None:
    times = [0, 0, 0, 1005, 1005, 1005, 1005, 7710]
    items = [(t, f"p{i}") for i, t in enumerate(times)]
    groups = group_by_window(items, 3600)
    assert [len(g) for g in groups] == [7, 1]


def test_window_slots_is_floor_of_w_over_seconds_per_slot() -> None:
    cfg = load_cluster_config(Path(__file__).resolve().parents[1] / "config" / "clusters.yaml")
    assert window_slots(cfg) == 9000
    from dataclasses import replace
    cfg2 = replace(cfg, seconds_per_slot=0.7)
    assert window_slots(cfg2) == 5142


def test_basis_is_slot_if_any_edge_lacks_time_else_block_time() -> None:
    assert basis_for([_edge("a", 1, 100), _edge("b", 2, 200)]) == TimeBasis.BLOCK_TIME
    assert basis_for([_edge("a", 1, 100), _edge("b", 2, None)]) == TimeBasis.SLOT


def test_grouping_is_order_independent() -> None:
    items = [(3, "c"), (1, "a"), (2, "b"), (10, "d"), (11, "e")]
    expected = group_by_window(items, 5)
    for perm in itertools.permutations(items):
        assert group_by_window(list(perm), 5) == expected


def test_single_item_is_its_own_group() -> None:
    assert group_by_window([(5, "x")], 3600) == (("x",),)
    assert group_by_window([], 3600) == ()


def test_window_of_is_min_max_in_chosen_basis() -> None:
    edges = [_edge("a", 10, 1000), _edge("b", 20, 2000)]
    w = window_of(edges, TimeBasis.BLOCK_TIME)
    assert (w.start, w.end, w.basis) == (1000, 2000, TimeBasis.BLOCK_TIME)
    w2 = window_of(edges, TimeBasis.SLOT)
    assert (w2.start, w2.end) == (10, 20)


def test_equal_times_tie_break_by_item_key_is_deterministic() -> None:
    items = [(0, f"p{i}") for i in range(5)]
    expected = group_by_window(items, 0)
    for perm in itertools.permutations(items):
        assert group_by_window(list(perm), 0) == expected


def test_edge_time_selects_basis() -> None:
    e = _edge("a", 42, 4242)
    assert edge_time(e, TimeBasis.BLOCK_TIME) == 4242
    assert edge_time(e, TimeBasis.SLOT) == 42
