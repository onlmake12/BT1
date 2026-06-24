Audit Report

## Title
`OrphanPool.by_out_point` Index Unbounded by Transaction Count Cap, Enabling Memory Amplification — (`tx-pool/src/component/orphan.rs`)

## Summary
`OrphanPool` enforces a 100-entry cap via `DEFAULT_MAX_ORPHAN_TRANSACTIONS`, but the secondary index `by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>` accumulates one entry per input across all orphan transactions. Because `limit_size()` enforces the cap exclusively on `self.entries.len()`, `by_out_point` can grow to `DEFAULT_MAX_ORPHAN_TRANSACTIONS × inputs_per_tx` entries. An unprivileged remote peer can exploit this via the standard relay flow to cause sustained heap growth sufficient to OOM-kill the target node.

## Finding Description
In `add_orphan_tx` (`orphan.rs` L150–155), every input's `OutPoint` is inserted into `by_out_point` before `limit_size()` is called:

```rust
for out_point in tx.input_pts_iter() {
    self.by_out_point
        .entry(out_point)
        .or_default()
        .insert(tx.proposal_short_id());
}
self.limit_size()
```

`limit_size()` (`orphan.rs` L96–132) enforces the cap exclusively on `self.entries.len()` via `self.len()` (`orphan.rs` L52–54):

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) { ... }
}
```

`remove_orphan_tx` (`orphan.rs` L74–87) correctly cleans up `by_out_point` on eviction, so the index stays consistent — but its maximum logical size at steady state is `DEFAULT_MAX_ORPHAN_TRANSACTIONS × inputs_per_tx`, not `DEFAULT_MAX_ORPHAN_TRANSACTIONS`. The `shrink_to_fit` call only reclaims allocated capacity after removals; it does not cap logical size.

Before reaching `add_orphan_tx`, a transaction passes `non_contextual_verify` (`tx-pool/src/util.rs` L56–83), which enforces `TRANSACTION_SIZE_LIMIT = 512,000 bytes` (`util/types/src/core/tx_pool.rs` L309). The relay handler (`sync/src/relayer/transactions_process.rs` L49–55) also requires the node to have previously requested the transaction from that peer — but this is satisfied by the standard relay flow: the attacker announces crafted hashes via `RelayTransactionHashes`, the node requests them, and the attacker responds with crafted high-input transactions. No privilege is required.

## Impact Explanation
`TRANSACTION_SIZE_LIMIT = 512,000 bytes` bounds each transaction's size. With a minimum CKB input of ~44 bytes, a single transaction can carry ~11,600 inputs. At 100 orphans × 11,600 inputs, `by_out_point` accumulates ~1,160,000 entries. Each entry carries an `OutPoint` key (36 bytes) plus `HashSet` and `HashMap` overhead, yielding an estimated 115–230 MB of heap growth from `by_out_point` alone — on top of the ~51 MB from storing the 100 full transaction bodies in `entries`. This sustained heap pressure can cause the node process to be OOM-killed or become severely degraded. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires no keys, no PoW, and no special role. Any peer connected to the target node can:
1. Announce crafted transaction hashes via `RelayTransactionHashes`.
2. Receive a `GetRelayTransactions` request from the node.
3. Respond with crafted transactions carrying the maximum number of inputs referencing non-existent `OutPoint`s.
4. Repeat as expired entries are evicted to maintain sustained `by_out_point` bloat.

The relay filter (`requesting_peer() == Some(self.peer)`, `transactions_process.rs` L53) is satisfied by step 2 and does not prevent the attack.

## Recommendation
Bound `by_out_point` growth explicitly using one or more of the following:
- **Per-orphan input cap**: Reject transactions in `add_orphan_tx` whose `input_pts_iter().count()` exceeds a configurable threshold (e.g., `MAX_ORPHAN_TX_INPUTS = 1000`) before inserting into either structure.
- **Total index cap**: Track `by_out_point.len()` and refuse insertion when it would exceed `DEFAULT_MAX_ORPHAN_TRANSACTIONS × MAX_ORPHAN_TX_INPUTS`.
- **Memory-based eviction**: Include `by_out_point.len()` in the `limit_size()` eviction predicate so that memory, not just entry count, is the enforced invariant.

## Proof of Concept
```
1. Connect to target node as a peer supporting RelayV3.
2. Craft 100 transactions T_1..T_100, each with N inputs (N = floor(512_000 / 44) ≈ 11,636)
   referencing non-existent OutPoints (random tx_hash, index=0).
3. For each T_i:
   a. Send RelayTransactionHashes announcing hash(T_i).
   b. Receive GetRelayTransactions from node.
   c. Send RelayTransactions containing T_i with declared_cycles <= max_block_cycles.
4. Node calls non_contextual_verify (passes: size within 512 KB limit, not cellbase),
   then _process_tx → Reject::Resolve(Unknown) → add_orphan_tx(T_i, peer, cycles).
5. After 100 insertions:
     orphan.entries.len() == 100           ← count cap satisfied
     orphan.by_out_point.len() ≈ 1,163,600 ← unbounded by cap
6. Monitor node RSS; observe growth of ~115–230 MB beyond baseline.
7. Repeat as ORPHAN_TX_EXPIRE_TIME elapses to maintain sustained pressure.
```