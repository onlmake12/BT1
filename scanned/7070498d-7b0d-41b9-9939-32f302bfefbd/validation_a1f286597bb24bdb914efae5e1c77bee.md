The code is confirmed. Let me verify a few more details before rendering judgment.

Audit Report

## Title
`remove_expired` Removes Only the Expired Entry, Leaving Dependent Descendants as Zombie Entries - (File: tx-pool/src/pool.rs)

## Summary

`TxPool::remove_expired` calls `pool_map.remove_entry(...)` for each expired transaction, which removes only the single entry and severs its links, but leaves all descendant transactions in the pool. Descendants whose own timestamps have not yet crossed the expiry threshold remain in the pool in a permanently uncommittable state, consuming pool size/cycle budget and holding their input outpoints in `edges.inputs`. This contradicts the function's own comment ("Expire all transaction **and their dependencies**") and diverges from every other eviction path in the codebase, all of which call `remove_entry_and_descendants`.

## Finding Description

`remove_expired` at `tx-pool/src/pool.rs:270-288` collects all entries satisfying `self.expiry + entry.inner.timestamp < now_ms` and removes each via `self.pool_map.remove_entry(&entry.proposal_short_id())` (line 284).

`remove_entry` (`pool_map.rs:235-250`) does the following for the removed entry only:
- `update_ancestors_index_key` / `update_descendants_index_key` — updates score keys for the removed entry.
- `remove_entry_edges` — releases the removed entry's own inputs from `edges.inputs`.
- `remove_entry_links` — severs the parent↔child link so the child's `links.parents` no longer contains the parent.

It does **not** recurse into or remove descendants. This is confirmed by the existing unit test `test_remove_entry` (`score_key.rs:157-160`), which explicitly asserts that after `remove_entry(&tx1_id)`, `tx2` and `tx3` remain in the pool.

The zombie scenario:
1. Attacker submits parent `P` at time `T` (timestamp `T`).
2. Attacker submits child `C` at time `T + δ` (timestamp `T + δ`), spending `P`'s output.
3. At time `T + expiry`, `remove_expired` fires. `P` satisfies the filter; `C` does not (its expiry is `T + δ + expiry`).
4. `P` is removed via `remove_entry`. `C` remains in the pool with `Status::Pending/Gap/Proposed`.
5. `C`'s inputs (which reference `P`'s outputs) remain registered in `edges.inputs`.
6. `C` can never be committed: `P` was never mined, so `P`'s outputs do not exist on-chain.
7. `C` will not be evicted until `T + δ + expiry`, giving a zombie window of up to `δ` (which approaches a full `expiry_hours` if the attacker submits `C` just before `P` expires).

All other eviction paths use `remove_entry_and_descendants`:
- `limit_size` → `pool_map.rs:307`
- `remove_tx` → `pool.rs:359`
- `remove_by_detached_proposal` → `pool.rs:343`
- `resolve_conflict` → `pool_map.rs:310`
- `resolve_conflict_header_dep` → `pool_map.rs:285`

`remove_expired` is the sole outlier.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker who controls transaction submission (any unprivileged peer or RPC caller) can maintain a sustained fraction of the mempool filled with permanently unspendable zombie entries:

- Each zombie entry counts against `pool_map.total_tx_size` and `total_tx_cycles`, reducing the effective pool capacity available to legitimate users.
- Zombie entries hold their input outpoints in `edges.inputs`, causing any replacement transaction spending the same outpoints to be rejected as a double-spend for the duration of the zombie window.
- By submitting many parent/child chains with children submitted just before each parent expires, the attacker can keep zombie entries alive for up to `expiry_hours` (default: 12 hours) per cycle, and repeat the pattern continuously.
- The pool's `limit_size` eviction path does use `remove_entry_and_descendants` and will eventually evict zombies when the pool is full, but this itself displaces legitimate low-fee transactions, degrading throughput.

## Likelihood Explanation

The attack requires only the ability to submit transactions — available to any network peer or RPC caller without special privileges. The cost is the minimum-fee transactions for `P` and `C₁…Cₙ`. Because `P` is intentionally low-fee and the zombie window per cycle is up to `expiry_hours`, the cost-to-impact ratio is favorable for an attacker. The attack is repeatable and does not require any victim mistakes or external conditions.

## Recommendation

Replace `remove_entry` with `remove_entry_and_descendants` inside `remove_expired`, consistent with every other eviction path and with the function's own comment:

```rust
// tx-pool/src/pool.rs  remove_expired()
for entry in removed {
    let tx_hash = entry.transaction().hash();
    debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
-   self.pool_map.remove_entry(&entry.proposal_short_id());
+   let evicted = self.pool_map.remove_entry_and_descendants(&entry.proposal_short_id());
    let reject = Reject::Expiry(entry.timestamp);
-   callbacks.call_reject(self, &entry, reject);
+   for e in evicted {
+       callbacks.call_reject(self, &e, reject.clone());
+   }
}
```

Note: since `remove_expired` already iterates over all expired entries, and `remove_entry_and_descendants` will remove descendants that may themselves appear later in the `removed` list, the inner `remove_entry` call inside `remove_entry_and_descendants` will simply return `None` for already-removed IDs (via `entries.remove_by_id`), making the change safe without additional deduplication.

## Proof of Concept

1. Configure a CKB node with a short `expiry_hours` (e.g., 1 hour) for faster testing.
2. Submit parent transaction `P` spending an on-chain UTXO, with minimum fee. Record submission time `T`.
3. Wait until just before `T + expiry` (e.g., 59 minutes), then submit child transaction `C` spending one of `P`'s outputs, also with minimum fee. Record submission time `T + δ`.
4. Wait until `T + expiry` passes (so `P` expires) but before `T + δ + expiry` (so `C` has not yet expired).
5. Query `tx_pool_info` or `get_transaction`: observe that `P` is gone from the pool but `C` is still present with `Status::Pending`.
6. Attempt to submit a new transaction `C'` spending the same output of `P` as `C` — it is rejected with a double-spend error referencing `C`, even though `P` is no longer in the pool and was never mined.
7. Repeat with many parent/child chains to demonstrate pool capacity exhaustion.

A unit test can be written directly against `TxPool::remove_expired` by constructing a parent/child pair, advancing the mock clock past the parent's expiry but not the child's, calling `remove_expired`, and asserting that the child is no longer present in the pool.