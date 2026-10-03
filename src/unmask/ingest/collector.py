# impl: FR-001-01, FR-001-02
"""Стан збору (data-model.md, «Стан збору і кеш»).

Зараз тут лише змінний `CollectionState` — спільний контракт кроків збору: перелічення
покупців (`buyers.py`, T-011) заповнює курсор, історію mint, купівлі й відібраних покупців;
решту полів заповнюють BFS фінансування (T-012/T-013) та оркестрація `collect()` (T-014).
`CollectionState` живе лише всередині збору й у партиційному сховищі кешу; у результат
(`IngestResult`) не потрапляє.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from unmask.ingest.model import Buyer, MissingHistory, MissingReason, Transfer, UnexpandedNode
from unmask.ingest.parse import ParsedTx
from unmask.ingest.purchases import Purchase

# Запис історії mint: (signature, slot, block_time, err) — рівно те, що зберігає перегортання (R-7).
MintSignature = tuple[str, int, int | None, Any]


@dataclass
class CollectionState:
    mint: str
    config_version: int
    # перелічення покупців (R-7): останній підпис перегортання історії mint або None
    signature_cursor: str | None = None
    mint_signatures: list[MintSignature] = field(default_factory=list)
    mint_history_exhausted: bool = False
    purchases_by_wallet: dict[str, Purchase] = field(default_factory=dict)
    buyers: tuple[Buyer, ...] = ()
    # BFS фінансування (R-1): глибина -> {гаманець: підпис-межа}
    frontier_by_depth: dict[int, dict[str, str]] = field(default_factory=dict)
    expanded: set[str] = field(default_factory=set)
    transfers: dict[tuple[str, str], Transfer] = field(default_factory=dict)
    unexpanded: list[UnexpandedNode] = field(default_factory=list)
    missing: dict[tuple[str, MissingReason], MissingHistory] = field(default_factory=dict)
    tx_cache: dict[str, ParsedTx] = field(default_factory=dict)
    rpc_calls: int = 0
    transactions_scanned: int = 0
