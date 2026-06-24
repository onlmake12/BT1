All code references check out. Let me verify one more detail about the `shrink_to_fit` threshold behavior and the `MultiIndexVerifyEntryMap` capacity semantics.

Audit Report

## Title
O(N²) `shrink_to_fit` on Bulk Peer-Queue Removal Enables CPU Exhaustion via Verify-Queue Write-Lock Stall - (File: tx-pool/src/component/verify_queue.rs)

## Summary

`VerifyQueue::remove_tx` unconditionally invokes `self.shrink_to_fit()` after every individual removal. When `remove_txs_by_peer` or `remove_txs` removes K entries from a queue of N entries, this triggers up to K/100 separate O(N) reallocation passes over the `MultiIndexVerifyEntryMap` (gated by `SHRINK_THRESHOLD = 100`), producing O(K × N) total CPU work — all while holding the exclusive `verify_queue` write-lock. An unprivileged remote peer can pre-fill the queue and trigger `ban_malformed` to stall the entire tx-pool.

## Finding Description

In `tx-pool/src/component/verify_queue.rs`, `remove_tx` at line 146 calls `self.shrink_to_fit()` on every single removal:

```rust
pub fn remove_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
    self.inner.remove_by_id(id).map(|e| {
        // ... accounting ...
        self.shrink_to_fit();   // called on EVERY removal
        e.inner
    })
}
```

`shrink_to_fit` expands via the macro in `util/src/shrink_to_fit.rs` (lines 14–19):

```rust
macro_rules! shrink_to_fit {
    ($map:expr, $threshold:expr) => {{
        if $map.capacity() > ($map.len() + $threshold) {
            $map.shrink_to_fit();   // O(N) reallocation across all indices
        }
    }};
}
```

With `SHRINK_THRESHOLD = 100`, the actual reallocation fires approximately once every 100 removals. For K bulk removals, this yields ~K/100 O(N) reallocation passes — still O(K × N) asymptotically. The `MultiIndexVerifyEntryMap` carries three indices (`hashed_unique` id, `ordered_non_unique` added_time, `hashed_non_unique` is_large_cycle), so each `shrink_to_fit` must reallocate and rehash all remaining entries across every index.

`remove_txs` (lines 152–156) loops calling `remove_tx` with no deferred shrink:

```rust
pub fn remove_txs(&mut self, ids: impl Iterator<Item = ProposalShortId>) {
    for id in ids {
        self.remove_tx(&id);   // shrink_to_fit fires every ~100 iterations
    }
}
```

`remove_txs_by_peer` (lines 159–168) collects all IDs for a peer and delegates to `remove_txs`. Both callers hold the `verify_queue` write-lock for the entire duration:

- `ban_malformed` in `tx-pool/src/process.rs` line 702: `self.verify_queue.write().await.remove_txs_by_peer(&peer);`
- Reorg path in `tx-pool/src/process.rs` lines 855–857: `queue.remove_txs(attached.iter().map(|tx| tx.proposal_short_id()));`

The correct pattern is already used in `OrphanPool::remove_orphan_txs` (`tx-pool/src/component/orphan.rs` lines 89–94), which calls `shrink_to_fit` once after the loop. `VerifyQueue` does not follow this pattern.

The 256 MB queue cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`, line 18) allows tens of thousands of minimum-size transactions to accumulate before the cap is hit.

## Impact Explanation

The `verify_queue` write-lock is held for the entire O(K × N) bulk removal. All concurrent tx-pool operations requiring this lock — new transaction submission (`enqueue_verify_queue`), block assembly, and reorg processing — are blocked until the removal completes. With a large queue, this constitutes a CPU-exhaustion stall reachable by any unprivileged relay peer at negligible cost. This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The attack requires no special privileges, keys, or hashpower. Any peer connected via the relay protocol can submit transactions to the verify queue. The `ban_malformed` trigger (`DeclaredWrongCycles`) is reachable by sending a transaction with a deliberately incorrect declared cycle count — a trivially crafted packet. The attacker only needs to pre-fill the queue with their `PeerIndex`-tagged transactions and then send one malformed transaction. The attack is repeatable: after the ban expires (3 days), or using a different peer identity, it can be re-executed.

## Recommendation

Move `shrink_to_fit` out of `remove_tx` and call it once after bulk operations complete, matching the pattern already used in `OrphanPool::remove_orphan_txs`:

- Remove `self.shrink_to_fit()` from `remove_tx` (line 146 of `verify_queue.rs`).
- Add a single `self.shrink_to_fit()` call at the end of `remove_txs` and `remove_txs_by_peer`.

This reduces bulk removal from O(K × N) to O(N), eliminating the repeated reallocation under the write-lock.

## Proof of Concept

```
1. Attacker connects to a CKB node via SupportProtocols::RelayV3.
2. Attacker submits a large number of minimum-size transactions to the verify queue,
   all tagged with the attacker's PeerIndex, until the queue holds N entries.
3. Attacker sends one transaction with a deliberately wrong declared cycle count,
   causing _process_tx to return Reject::DeclaredWrongCycles and invoke ban_malformed.
4. ban_malformed acquires verify_queue.write() and calls remove_txs_by_peer.
5. remove_txs_by_peer → remove_txs → remove_tx (×N), with shrink_to_fit firing
   every ~100 iterations (~N/100 O(N) reallocations total).
6. All concurrent tx-pool operations (new tx submission, block assembly) stall
   until the loop completes under the write-lock.
7. Repeatable with a new peer identity or after the 3-day ban expires.
```