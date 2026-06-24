All cited code references have been verified against the actual repository. Here is the validation result:

Audit Report

## Title
Orphan Transaction Pool Slot Exhaustion via No Per-Peer Limit — (`tx-pool/src/component/orphan.rs`)

## Summary
`OrphanPool` enforces a single global cap of 100 entries with no per-peer quota. A single unprivileged P2P peer can fill all 100 slots with structurally valid transactions referencing non-existent `OutPoint`s. When the pool is full, `limit_size()` evicts one entry at random and the attacker immediately re-fills the slot, maintaining near-total occupancy and preventing legitimate orphan transactions from persisting long enough to be promoted.

## Finding Description
`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` is the sole global cap at `tx-pool/src/component/orphan.rs:16`. The `OrphanPool` struct holds only `entries: HashMap<ProposalShortId, Entry>` and `by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>`; there is no per-peer counter anywhere in the struct or its methods. When the pool is full, `limit_size()` at lines 119–125 evicts the first key returned by `HashMap::keys().next()` — effectively random — with no preference for entries from the most-contributing peer. `add_orphan_tx` at lines 134–159 accepts entries from any peer with no per-peer accounting or quota check.

The reachable exploit path is: remote peer sends `RelayTransactions` → `non_contextual_verify` passes (structural checks only, no input-existence check, confirmed in `tx-pool/src/util.rs:56–83`) → resolution fails with `Reject::Resolve(out_point_err)` where `out_point_err.is_unknown()` is true → `is_missing_input` returns `true` (confirmed at `tx-pool/src/util.rs:150–152`) → `add_orphan` is called (`tx-pool/src/process.rs:507–512`). Crucially, `ban_malformed` is only triggered for `reject.is_malformed_tx()`, not for `is_missing_input` rejections, so the attacker is never banned and can submit indefinitely.

## Impact Explanation
Matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attack requires zero CKB balance — only a live P2P connection and structurally valid transactions. When the orphan pool is saturated, legitimate child transactions that arrive before their parents cannot be stored. When the parent later arrives, `process_orphan_tx` finds no children to promote, and the child transaction is silently dropped from the relay graph unless independently re-relayed. Applied across multiple nodes simultaneously, this degrades transaction relay reliability across the targeted portion of the network at negligible attacker cost.

## Likelihood Explanation
Low-to-medium. The attacker needs only a standard P2P connection (available to any network participant) and must continuously re-submit replacement orphan transactions as their slots are evicted. No funds, no signatures, and no special privileges are required. The attack is cheap to sustain indefinitely and can be parallelized across multiple target nodes.

## Recommendation
Add per-peer accounting to `OrphanPool`. Introduce a `HashMap<PeerIndex, usize>` field tracking entry counts per peer. In `limit_size()`, prefer evicting from the peer with the most entries, analogous to the largest-group eviction strategy in `PeerRegistry::try_evict_inbound_peer` at `network/src/peer_registry.rs:191–210`. Alternatively, cap per-peer orphan entries at `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_inbound_peers` and reject new orphan submissions from a peer that has reached its quota inside `add_orphan_tx`.

## Proof of Concept
1. Attacker establishes a standard P2P connection to the target CKB node using the Relay protocol.
2. Attacker crafts 100 structurally valid CKB transactions, each with one input referencing a random non-existent `OutPoint` (random `tx_hash`, index 0). No CKB balance or valid signatures are needed.
3. Each transaction is sent via `RelayTransactions`. Each passes `non_contextual_verify`, enters the verify queue, fails resolution with `OutPointError::Unknown`, satisfies `is_missing_input`, and is inserted into the orphan pool via `add_orphan_tx`. No ban is triggered because `is_missing_input` rejections do not call `ban_malformed`.
4. After 100 submissions, `OrphanPool::len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS`.
5. Any legitimate orphan from an honest peer triggers `limit_size()`, which randomly evicts one entry (100/101 probability of evicting an attacker entry). The attacker immediately sends a replacement, restoring full occupancy.
6. Legitimate orphan transactions are evicted within one round-trip and are never promoted when their parents arrive. This is reproducible as a unit test: fill the orphan pool from a single `PeerIndex`, insert a legitimate orphan from a different peer, and assert it is immediately evicted — the existing test infrastructure in `tx-pool/src/component/tests/orphan.rs` provides the scaffolding needed.