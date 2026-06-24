Audit Report

## Title
`remove_expired` Omits Descendant Cascade, Leaving Child Transactions Permanently Stuck Until Their Own Expiry — (`tx-pool/src/pool.rs`)

## Summary
`TxPool::remove_expired` calls `pool_map.remove_entry` (single-entry removal) instead of `pool_map.remove_entry_and_descendants`, while every other bulk-eviction path (`limit_size`, `resolve_conflict`, `resolve_conflict_header_dep`) uses the descendant-aware variant. When a parent transaction expires before its children, the children remain in the pool with inputs that reference outputs no longer present on-chain or in the pool, making them permanently unmineable until their own individual expiry fires or `limit_size` non-deterministically evicts them.

## Finding Description

**Root cause — `remove_expired` (pool.rs L271–287):**

```rust
self.pool_map.remove_entry(&entry.proposal_short_id());
```

`remove_entry` (pool_map.rs L235–250) removes only the targeted entry: it releases the parent's own inputs from `edges.inputs`, disconnects the parent from the `links` map (including removing itself from each child's parent set via `remove_entry_links`), and updates ancestor/descendant score keys. It does **not** touch any child entry.

**Contrast with `limit_size` (pool.rs L307):**

```rust
let removed = self.pool_map.remove_entry_and_descendants(&id);
```

`remove_entry_and_descendants` (pool_map.rs L252–265) first collects the target plus the full transitive descendant set via `calc_descendants`, strips all links, then removes every entry in that set.

**What is left behind after `remove_expired` removes parent P:**

1. P's entry is gone; P's own inputs are released from `edges.inputs`.
2. Every child C that spends an output of P still has its input edge recorded in `edges.inputs` as `P_output_outpoint → C_short_id`.
3. C's `links` entry no longer lists P as a parent (because `remove_entry_links` was called for P), but C's input edge is unresolvable: P's output is neither on-chain nor in the pool.
4. C retains its `Pending`/`Gap`/`Proposed` status, is counted in pool statistics, and is considered by the block assembler — which will fail to resolve C's input.

**Trigger path — `_update_tx_pool_for_reorg` (process.rs L1109–1110):**

```rust
// Remove expired transaction from pending
tx_pool.remove_expired(callbacks);
```

This fires on every new block, so the condition is checked continuously.

**Precondition for the asymmetry:** Parent P is submitted at time `t0`; child C is submitted at time `t0 + δ` (δ > 0). Both share the same pool `expiry` duration. P expires at `t0 + expiry`; C expires at `t0 + δ + expiry`. During the window `[t0 + expiry, t0 + δ + expiry]`, P is removed but C is stuck.

## Impact Explanation

**High — bad design causing CKB network congestion with few costs.**

An attacker constructs a fan-out chain: one parent P with N outputs, and N children C1…CN each spending one distinct output of P. Submitting this costs one UTXO for P plus fees for N+1 transactions. After P's expiry fires, all N children are stuck in the pool occupying N pool slots, inflating `pending_count`/`gap_count`/`proposed_count`, and blocking legitimate transactions from being accepted once `max_tx_pool_size` is reached. The attacker can repeat this cycle continuously (each cycle requires only one new UTXO for a fresh P), keeping the pool partially or fully saturated with unresolvable entries. `limit_size` will eventually evict them by fee-rate, but this is non-deterministic and itself rejects legitimate low-fee transactions in the process.

## Likelihood Explanation

Triggerable by any unprivileged user via `send_transaction` RPC. No special privileges, no victim interaction, and no external dependency required. The only precondition is that P and C have different submission timestamps, which is the normal case whenever a user submits a parent-child chain sequentially. `remove_expired` fires on every block, so the trigger is continuous.

## Recommendation

Replace `remove_entry` with `remove_entry_and_descendants` inside `remove_expired`, matching the behavior of every other bulk-removal path:

```rust
// current (broken)
self.pool_map.remove_entry(&entry.proposal_short_id());

// fix
let removed = self.pool_map.remove_entry_and_descendants(&entry.proposal_short_id());
for e in removed {
    let reject = Reject::Expiry(e.timestamp);
    callbacks.call_reject(self, &e, reject);
}
```

Note: the outer loop already collects all expired entries before removal, so iterating `removed_ids` (which may include already-collected children) requires deduplication or a guard against double-removal. The safest approach is to collect expired root entries (those whose parents are not also expired), then call `remove_entry_and_descendants` once per root.

## Proof of Concept

1. Submit parent tx `P` (with ≥2 outputs) via `send_transaction`. Record `t0 = now`.
2. Immediately submit children `C1`, `C2`, …, `CN`, each spending one output of `P`. Their timestamps are `t0 + ε`.
3. Wait until `t0 + expiry` elapses (P's expiry fires) but before `t0 + ε + expiry` (children not yet expired).
4. Mine a block. `_update_tx_pool_for_reorg` → `remove_expired` fires.
5. Assert: `P` is absent from the pool; `C1…CN` are still present with status `Pending` or `Proposed`.
6. Attempt block assembly: the assembler cannot resolve `C1…CN`'s inputs (P's outputs are gone). No block including any `Ci` can be produced.
7. Query pool stats: `pending_count` is inflated by N.
8. Repeat steps 1–4 with fresh UTXOs. After K cycles, the pool holds up to K×N stuck entries, causing legitimate transactions to be rejected by `limit_size`.