Audit Report

## Title
Unbounded Per-Peer Orphan Pool Submission Enables Targeted Eviction of Legitimate Orphan Transactions - (File: `tx-pool/src/component/orphan.rs`)

## Summary
`OrphanPool` enforces only a global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer admission quota. The `limit_size()` eviction loop selects victims by raw `HashMap` iteration order, never consulting the `peer` field stored in each `Entry`. A single unprivileged P2P peer can saturate all 100 slots with crafted transactions referencing non-existent inputs, evicting legitimate orphan transactions before their parents arrive and causing silent drops of valid child transactions.

## Finding Description

**Root cause — no per-peer quota in `OrphanPool`:**

`tx-pool/src/component/orphan.rs` line 16 defines the only guard:
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

`add_orphan_tx` (lines 134–159) checks only for duplicate `ProposalShortId` (line 140), not per-peer counts. `limit_size()` (lines 96–132) evicts by HashMap iteration order:
```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```
The `peer` field in `Entry` (line 23) is stored but never consulted during eviction or admission.

**Entry path — P2P relay:**

In `tx-pool/src/process.rs` lines 507–512, any remote transaction whose inputs are missing is unconditionally added to the orphan pool:
```rust
if is_missing_input(reject) {
    self.send_result_to_relayer(TxVerificationResult::UnknownParents { ... });
    self.add_orphan(tx, peer, declared_cycle).await;
}
```
`is_missing_input` (defined in `tx-pool/src/util.rs` line 150–152) matches `Reject::Resolve(out_point_err) if out_point_err.is_unknown()` — exactly what a transaction with non-existent inputs produces. `non_contextual_verify` (lines 56–83 of `util.rs`) does not check input existence; that is a contextual check, so crafted transactions with random `OutPoint` inputs pass the non-contextual gate and reach the orphan path.

**Eviction consequence — silent drop on parent arrival:**

`add_orphan` (lines 557–573 of `process.rs`) forwards evicted hashes as `TxVerificationResult::Reject`, removing them from relay tracking. `remove_orphan_tx` (lines 74–87 of `orphan.rs`) removes the entry from both `entries` and `by_out_point`. When a parent is later accepted, `process_orphan_tx` calls `find_orphan_by_previous` → `find_by_previous` (lines 161–167 of `orphan.rs`), which looks up `by_out_point`. The evicted child is absent and silently dropped.

**Rate limit is insufficient:**

`sync/src/relayer/mod.rs` lines 81 and 91 show the rate limiter is keyed by `(PeerIndex, u32)` at 30 messages/second. `util/constant/src/sync.rs` line 68 shows `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`. A single `RelayTransactions` message can carry up to 32,767 transactions — 327× the orphan pool capacity — so 100 orphan slots are saturated in one message after the standard hash-announcement → request → send flow.

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker with a single P2P connection can continuously prevent any orphan transaction from surviving long enough for its parent to arrive. In CKB's two-phase commit model, child transactions submitted before their parent is confirmed are a normal and expected pattern. Sustained flooding of the orphan pool across multiple nodes disrupts this relay mechanism network-wide, forcing users to repeatedly resubmit transactions and degrading transaction propagation reliability at negligible cost to the attacker.

## Likelihood Explanation

The attack requires only: (1) a single P2P connection to the target node, (2) the ability to craft transactions with arbitrary non-existent `OutPoint` inputs — no keys or funds required, (3) knowledge of the standard relay protocol flow. The orphan pool holds only 100 entries. The attack is cheap, stateless, and can be sustained indefinitely. No privileged access is required.

## Recommendation

Enforce a per-peer cap on orphan pool entries. Add a `HashMap<PeerIndex, usize>` counter to `OrphanPool`. During `add_orphan_tx`, if the submitting peer already holds `>= DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_connected_peers` entries, reject the new entry outright rather than evicting a random existing one. In `limit_size()`, prefer evicting from the peer with the most entries (the likely flooder) rather than by HashMap iteration order. Update `remove_orphan_tx` to decrement the per-peer counter. This mirrors the fix pattern used in Bitcoin Core's orphan pool (per-peer limits introduced in Bitcoin Core #27742).

## Proof of Concept

1. Attacker connects to a CKB node via the `RelayV3` protocol as a standard P2P peer.
2. Honest user relays `child_tx` (spending an output of `parent_tx` not yet in the pool). `child_tx` enters the orphan pool (size: 1).
3. Attacker announces 100+ fake transaction hashes via `RelayTransactionHashes`. The node requests them via `GetRelayTransactions`.
4. Attacker responds with a single `RelayTransactions` message carrying 100+ crafted transactions, each with inputs referencing random non-existent `OutPoint`s. This is within the `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` limit and passes the 30 msg/sec rate limiter as a single message.
5. Each crafted transaction passes `non_contextual_verify`, fails `resolve_tx` with `is_unknown()`, and is added to the orphan pool via `add_orphan`. After the 100th entry, `limit_size()` fires and evicts `child_tx` with high probability (certainty after 200+ attacker transactions).
6. Attacker's junk transactions occupy all 100 slots.
7. `parent_tx` is confirmed and relayed. `process_orphan_tx` calls `find_by_previous(&parent_tx)` — returns empty because `child_tx` was removed from `by_out_point` by `remove_orphan_tx`.
8. `child_tx` is permanently dropped from this node. The attacker repeats steps 3–6 to prevent any resubmission from succeeding.