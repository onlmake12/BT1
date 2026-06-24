Audit Report

## Title
Unbounded Per-Peer Orphan Pool Submission Enables Targeted Eviction of Legitimate Orphan Transactions - (File: `tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` enforces a global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer admission quota. When the pool is full, `limit_size()` evicts entries by HashMap iteration order (effectively random), never consulting the `peer` field stored in each `Entry`. A single unprivileged P2P peer can saturate all 100 slots with crafted transactions referencing non-existent inputs, probabilistically or deterministically evicting legitimate orphan transactions. When the legitimate parent later arrives, `process_orphan_tx` finds no matching child in `by_out_point` and silently drops it.

## Finding Description

**Root cause — no per-peer quota in `OrphanPool`:**

`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` is the only guard. In `limit_size()`:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

The `peer` field in `Entry` is stored but never consulted during eviction or admission. A single peer can occupy all 100 slots.

**Entry path — P2P relay:**

In `tx-pool/src/process.rs`, any remote transaction whose inputs are missing is unconditionally added to the orphan pool:

```rust
if is_missing_input(reject) {
    self.send_result_to_relayer(TxVerificationResult::UnknownParents { ... });
    self.add_orphan(tx, peer, declared_cycle).await;
}
```

`add_orphan` calls `add_orphan_tx` which calls `limit_size()`, returning evicted hashes. These are forwarded as `TxVerificationResult::Reject`, which causes `remove_from_known_txs` to be called on the evicted hash in the relay state — removing the evicted child from the node's tracking entirely.

**Eviction consequence — silent drop on parent arrival:**

When a parent transaction is accepted, `process_orphan_tx` calls `find_by_previous`, which looks up `by_out_point`. Since `remove_orphan_tx` removes entries from both `entries` and `by_out_point`, an evicted child is never found and never re-processed.

**Rate limit is insufficient:**

The relay rate limiter in `sync/src/relayer/mod.rs` is keyed by `(PeerIndex, message_item_id)` at 30 messages/second. Each `RelayTransactions` message can carry up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` transactions, so 100 orphan slots can be saturated in a single message after the standard hash-announcement → request → send flow.

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker with a single P2P connection can continuously prevent any orphan transaction from surviving long enough for its parent to arrive. In CKB's two-phase commit model, child transactions submitted before their parent is confirmed are a normal and expected pattern. Sustained flooding of the orphan pool across multiple nodes disrupts this relay mechanism network-wide, forcing users to repeatedly resubmit transactions and degrading transaction propagation reliability at negligible cost to the attacker.

## Likelihood Explanation

The attack requires only: (1) a single P2P connection to the target node, (2) the ability to craft transactions with arbitrary non-existent `OutPoint` inputs (no keys or funds required), and (3) knowledge of the standard relay protocol flow. The orphan pool size is only 100 entries. The attack is cheap, stateless, and can be sustained indefinitely by a single peer. No privileged access is required.

## Recommendation

Enforce a per-peer cap on orphan pool entries. Track a count of entries per `PeerIndex`. When `limit_size()` must evict, prefer evicting from the peer with the most entries (the likely flooder). Additionally, during `add_orphan_tx`, if the submitting peer already holds `>= pool_size / max_peers` entries, reject the new entry outright rather than evicting a random existing one. This mirrors the fix pattern used in Bitcoin Core's orphan pool (per-peer limits introduced in Bitcoin Core #27742).

## Proof of Concept

1. Attacker connects to a CKB node via the `RelayV3` protocol as a standard P2P peer.
2. Honest user relays `child_tx` (spending an output of `parent_tx` not yet in the pool). `child_tx` enters the orphan pool (size: 1).
3. Attacker announces 100 fake transaction hashes via `RelayTransactionHashes`. The node requests them via `GetRelayTransactions`.
4. Attacker responds with `RelayTransactions` carrying 100 crafted transactions, each with inputs referencing random non-existent `OutPoint`s.
5. Each is rejected with `is_missing_input` and added to the orphan pool. After the 100th, `limit_size()` fires. With 100 attacker entries and 1 legitimate entry, `child_tx` is evicted with ~1% probability per eviction event. Sending 200+ attacker transactions drives eviction probability to near certainty.
6. Attacker's junk transactions occupy all 100 slots.
7. `parent_tx` is confirmed and relayed. `process_orphan_tx` calls `find_by_previous(&parent_tx)` — returns empty because `child_tx` was removed from `by_out_point`.
8. `child_tx` is permanently dropped from this node. The user must resubmit manually. The attacker repeats step 3–6 to prevent resubmission from succeeding.