The code confirms all technical claims. Let me verify the key elements before rendering judgment.

Audit Report

## Title
Random Eviction with No Per-Peer Quota Allows Any Relay Peer to Grief Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` is bounded at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. When full, `limit_size()` evicts via `self.entries.keys().next()` — effectively arbitrary HashMap iteration order — with no fee-rate ordering and no per-peer submission cap. Any connected relay peer can fill all 100 slots with zero-cost orphan transactions, causing legitimate orphan transactions to be randomly evicted and never auto-promoted when their parent arrives.

## Finding Description
The constants are confirmed at `tx-pool/src/component/orphan.rs` lines 15–16:
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

The eviction loop at lines 119–125 uses `self.entries.keys().next()` — a `HashMap` whose iteration order is non-deterministic — with no consultation of `entry.peer`, `entry.cycle`, or any fee metric:
```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

`add_orphan_tx` (lines 134–159) imposes no per-peer quota before inserting and calling `limit_size()`. The `Entry` struct records `peer` (line 22) but `limit_size()` never reads it.

**Exploit path:**
1. Attacker connects as a relay peer and sends `RelayTransactionHashes` with 100 hashes.
2. The victim node adds them to `unknown_tx_hashes` and issues `GetRelayTransactions` (confirmed in `transaction_hashes_process.rs` lines 38–49 and `types/mod.rs` lines 1483–1531).
3. The attacker responds with 100 structurally valid transactions whose inputs reference non-existent outputs. These pass `non_contextual_verify` but fail at resolve with `OutPointError::Unknown`, triggering `is_missing_input` (confirmed `util.rs` line 150–152), which routes them to `add_orphan` (`process.rs` line 512).
4. The pool is now at capacity. Any subsequent legitimate orphan is inserted as entry 101, and `limit_size()` evicts one entry arbitrarily.
5. The attacker continuously re-announces and re-fills the pool. The legitimate orphan is permanently displaced.
6. When the parent is confirmed, `process_orphan_tx` (`process.rs` lines 591–671) looks up children via `find_by_previous` — but the evicted child is gone, so it is never auto-promoted.

The only existing guard in `transactions_process.rs` (lines 50–54) verifies that the node previously requested the transaction from that peer, but this is trivially satisfied by step 1–2 above. The `unknown_tx_hashes` per-peer cap (`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767`) is far larger than the 100-slot orphan pool, so it provides no meaningful protection here.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A single attacker maintaining one peer connection can simultaneously target multiple CKB nodes, keeping their orphan pools saturated. Child transactions of in-flight parents are silently dropped and never promoted, degrading transaction relay reliability across the network at negligible cost to the attacker (no CKB required, no mining, no keys).

## Likelihood Explanation
Any unprivileged relay peer can execute this attack. Requirements: one TCP connection, ability to craft structurally valid transactions (no signatures needed for orphan insertion since script execution is not reached when inputs are unresolvable), and the ability to re-announce hashes after eviction. The pool size of 100 is trivially exhausted. The `ORPHAN_TX_EXPIRE_TIME` of `100 * MAX_BLOCK_INTERVAL` means attacker entries persist for a very long time without refresh. The attack is repeatable indefinitely with no ban risk, since sending orphan transactions is not a bannable offense (only `is_malformed_tx()` triggers `ban_malformed`, confirmed at `process.rs` line 514–515).

## Recommendation
1. **Per-peer eviction priority**: When `limit_size()` must evict, count entries per peer and evict from the peer with the most entries first, analogous to Bitcoin Core's orphan pool strategy.
2. **Per-peer submission cap**: Enforce a hard limit on how many orphan entries a single `PeerIndex` may hold in the pool simultaneously (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / MAX_PEERS`).
3. **Fee-rate ordering**: Evict the lowest-fee-rate orphan rather than an arbitrary one, so zero-fee attacker transactions are preferentially removed.

## Proof of Concept
```
1. Connect to victim node via RelayV3 protocol.
2. Send RelayTransactionHashes { hashes: [H1..H100] }.
3. Receive GetRelayTransactions { tx_hashes: [H1..H100] }.
4. For each Hi, craft a transaction with one input referencing a random
   non-existent OutPoint (tx_hash=random_bytes, index=0), one output,
   and a valid witness placeholder. Send as RelayTransactions.
   → Each tx passes non_contextual_verify, fails resolve with Unknown,
     is_missing_input() == true → add_orphan_tx() called → pool fills to 100.
5. Have a second connection submit a legitimate child_tx (parent in-flight).
   → add_orphan_tx inserts it (len=101), limit_size() evicts one arbitrarily.
6. Immediately re-send one attacker orphan to refill to 100.
   → child_tx is permanently displaced.
7. Mine parent tx. process_orphan_tx() finds no child in pool → child never promoted.
```
A unit test can reproduce this directly against `OrphanPool::add_orphan_tx` by inserting 100 attacker entries from `peer=0`, then inserting a legitimate entry from `peer=1`, and asserting that after `limit_size()` the legitimate entry has been evicted while attacker entries remain.