# verifies: FR-003-01, FR-003-12
"""Тести union-find: компоненти, чернетки, детермінізм (T-067)."""

from __future__ import annotations

import itertools
import random

import pytest

from unmask.clusters.cluster import ClusterDraft, cluster_id, form_clusters
from unmask.clusters.links import Link, link_sort_key
from unmask.clusters.model import EvidenceType, TimeBasis, Window
from unmask.graph.model import EdgeRef


def _link(buyers, via=("s",), etype=EvidenceType.SHARED_FUNDER, tag="t") -> Link:
    refs = (EdgeRef(signature="sig_" + tag + "_" + "_".join(sorted(buyers)), slot=1, instruction_path="0"),)
    return Link(type=etype, buyers=frozenset(buyers), via=tuple(via), refs=refs,
                window=Window(basis=TimeBasis.BLOCK_TIME, start=1, end=1), basis=TimeBasis.BLOCK_TIME)


def _bfs(buyers, links):
    adj: dict[str, set[str]] = {b: set() for b in buyers}
    for link in links:
        members = sorted(link.buyers)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                adj[members[i]].add(members[j])
                adj[members[j]].add(members[i])
    seen: dict[str, int] = {}
    comp_id = 0
    for b in buyers:
        if b in seen:
            continue
        stack = [b]
        seen[b] = comp_id
        while stack:
            x = stack.pop()
            for y in adj[x]:
                if y not in seen:
                    seen[y] = comp_id
                    stack.append(y)
        comp_id += 1
    groups: dict[int, set[str]] = {}
    for b, c in seen.items():
        groups.setdefault(c, set()).add(b)
    return groups


def test_matches_bfs_oracle_on_random_link_sets() -> None:
    rng = random.Random(20261005)
    for trial in range(200):
        n = rng.randint(1, 40)
        buyers = [f"w{i:02d}" for i in range(n)]
        links = []
        for li in range(rng.randint(0, 10)):
            k = rng.randint(2, 4)
            members = rng.sample(buyers, min(k, n))
            if len(members) < 2:
                continue
            links.append(_link(members, tag=f"{trial}_{li}"))
        drafts = form_clusters(buyers, links)
        oracle = {frozenset(v) for v in _bfs(buyers, links).values() if len(v) >= 2}
        assert {d.wallets for d in drafts} == oracle


def test_singletons_form_no_draft() -> None:
    assert form_clusters(["a", "b", "c"], []) == ()


def test_each_link_belongs_to_exactly_one_draft() -> None:
    links = [_link(("a", "b"), tag="1"), _link(("b", "c"), tag="2"), _link(("d", "e"), tag="3")]
    drafts = form_clusters(["a", "b", "c", "d", "e"], links)
    counts: dict[int, int] = {}
    for d in drafts:
        for link in d.links:
            counts.setdefault(id(link), 0)
            counts[id(link)] += 1
    assert sorted(counts.values()) == [1, 1, 1]
    assert {d.wallets for d in drafts} == {frozenset(("a", "b", "c")), frozenset(("d", "e"))}


def test_result_independent_of_link_order() -> None:
    links = [_link(("a", "b"), tag="1"), _link(("c", "d"), tag="2"), _link(("b", "c"), tag="3")]
    expected = form_clusters(["a", "b", "c", "d"], links)
    for perm in itertools.permutations(links):
        got = form_clusters(["a", "b", "c", "d"], list(perm))
        assert [(d.wallets, d.links) for d in got] == [(d.wallets, d.links) for d in expected]


def test_every_buyer_in_at_most_one_draft() -> None:
    links = [_link(("a", "b"), tag="1"), _link(("c", "d"), tag="2")]
    drafts = form_clusters(["a", "b", "c", "d", "e"], links)
    seen = [w for d in drafts for w in d.wallets]
    assert len(set(seen)) == len(seen)


def test_drafts_sorted_by_min_address() -> None:
    links = [_link(("z1", "z2"), tag="1"), _link(("a1", "a2"), tag="2")]
    drafts = form_clusters(["z1", "z2", "a1", "a2"], links)
    assert [min(d.wallets) for d in drafts] == sorted(min(d.wallets) for d in drafts)


def test_three_buyer_link_unions_all_three() -> None:
    drafts = form_clusters(["a", "b", "c"], [_link(("a", "b", "c"), tag="x")])
    assert len(drafts) == 1 and drafts[0].wallets == frozenset(("a", "b", "c"))


def test_cluster_id_stable_for_same_wallets_regardless_of_links_and_order() -> None:
    d1 = form_clusters(["a", "b"], [_link(("a", "b"), tag="1")])
    d2 = form_clusters(["b", "a"], [_link(("b", "a"), tag="2")])
    assert cluster_id(d1[0].wallets) == cluster_id(d2[0].wallets)


def test_deep_chain_does_not_recurse() -> None:
    buyers = [f"w{i:04d}" for i in range(1000)]
    links = [_link((buyers[i], buyers[i + 1]), via=(), etype=EvidenceType.DIRECT_TRANSFER, tag=f"c{i}")
             for i in range(999)]
    drafts = form_clusters(buyers, links)
    assert len(drafts) == 1 and len(drafts[0].wallets) == 1000


def test_draft_links_sorted_by_key() -> None:
    l1 = _link(("a", "b"), tag="z")
    l2 = _link(("a", "b"), tag="a")
    (draft,) = form_clusters(["a", "b"], [l1, l2])
    assert draft.links == tuple(sorted([l1, l2], key=link_sort_key))
