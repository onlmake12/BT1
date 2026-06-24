Audit Report

## Title
`remove_expired` Removes Parent Transactions Without Evicting Dependent Descendants, Leaving Zombie Entries in the Pool - (File: tx-pool/src/pool.rs)

## Summary

`TxPool::remove_expired` iterates over expired entries and removes each one individually via `pool_map.remove_entry(...)` rather than `remove_entry_and_descendants`. Child transactions submitted after the parent — but before the parent's expiry — remain in the pool in a permanently uncommittable state, consuming pool capacity and locking their input outpoints in `edges.inputs`. This contradicts the function's own comment ("Expire all transaction **and their dependencies**") and diverges from every other eviction path in the codebase.

## Finding Description

The code at `tx-pool/src/pool.rs` L270–288 confirms the bug exactly as described:

```rust
// Expire all transaction (and their dependencies) in the pool.
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    ...
    for entry in removed {
        self.pool_map.remove_entry(&entry.proposal_short_id()); // ← single-entry removal only
        ...
    }
}
```

The expiry filter (`self.expiry + entry.inner.timestamp < now_ms`) checks each entry's **own** timestamp independently. A child submitted at time `T + δ` expires at `T + δ + expiry_hours`, which is strictly later than the parent's expiry at `T + expiry_hours`. During the window `[T + expiry_hours, T + δ + expiry_hours]`:

1. The parent is removed via `remove_entry` (L235–250 of `pool_map.rs`), which calls `remove_entry_edges` and `remove_entry_links` — severing the parent↔child link and releasing the parent's own inputs, but **not** touching the child entries.
2. Child transactions remain in `entries` with `Status::Pending/Gap/Proposed`.
3. Their inputs (referencing the now-gone parent's outputs) remain registered in `edges.inputs`.
4. `calc_ancestors` returns an empty set for them (links severed), so the pool treats them as root transactions — but their inputs reference outputs that are neither on-chain nor in the pool.
5. They can never be committed and will not be re-evicted until their own timestamps cross the threshold.

By contrast, `remove_entry_and_descendants` (L252–265 of `pool_map.rs`) first collects all descendants via `calc_descendants`, pre-removes all links, then removes each entry — the correct pattern used by every other eviction path (`limit_size`, `remove_tx`, `remove_by_detached_proposal`, `resolve_conflict`, `resolve_conflict_header_dep`).

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can keep a significant fraction of the pool filled with permanently unspendable zombie transactions:

- Pool size/cycle budget is consumed by entries that can never be mined.
- `edges.inputs` records the zombie children's outpoints as claimed, so any legitimate transaction attempting to spend the same outpoints is rejected as a double-spend — even though the parent was never mined and is no longer in the pool.
- By maximizing `δ` (submitting children just before the parent expires), the zombie window approaches a full `expiry_hours` (default 12 hours).
- Repeating the pattern with many parent/child chains can sustain pool saturation indefinitely.

## Likelihood Explanation

The attack requires only the ability to submit transactions — available to any unprivileged peer or RPC caller. The parent transaction needs only a fee above the minimum to be accepted. The attacker controls `δ` and can set it to nearly `expiry_hours`, maximizing the zombie window. The pattern is repeatable at low cost and requires no special knowledge or privileges.

## Recommendation

Replace `remove_entry` with `remove_entry_and_descendants` inside `remove_expired`, consistent with every other eviction path:

```rust
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

This aligns the implementation with the function comment and with the behaviour of all other eviction paths.

## Proof of Concept

1. Submit parent transaction `P` with a fee just above the minimum (accepted into the pending pool, timestamp `T`).
2. Submit child transaction `C` spending an output of `P`, submitted at time `T + δ` (where `δ` is close to `expiry_hours`).
3. Wait until `now > T + expiry_hours` but `now < T + δ + expiry_hours`. `remove_expired` fires and removes `P` but not `C`.
4. Query `tx_pool_info` — `C` is still listed as pending even though `P` is gone.
5. Submit a new transaction `C'` spending the same output of `P` as `C` — it is rejected with a double-spend error, despite `P` being absent from both the pool and the chain.
6. Repeat with many parent/child chains to sustain pool saturation across the full zombie window.