Audit Report

## Title
Expired Transaction Descendants Remain in Proposed Pool, Causing Persistent Invalid Block Templates — (File: `tx-pool/src/pool.rs`)

## Summary
`remove_expired` in `tx-pool/src/pool.rs` calls bare `remove_entry` for each expired transaction, which cleans up the expired parent's own edges and links but leaves all descendant transactions in the pool. Descendants in `Status::Proposed` are subsequently selected by `TxSelector` for every block template because their ancestor check passes (the links to the expired parent were severed), causing miners to repeatedly produce invalid blocks until the descendants individually expire.

## Finding Description

`PoolMap` maintains three coupled structures: `entries` (the canonical transaction set), `edges` (input/dep/header-dep mappings), and `links` (parent/child relationships).

`remove_expired` collects all entries whose `expiry + timestamp < now_ms` and calls `pool_map.remove_entry` for each:

```
// tx-pool/src/pool.rs L271-288
self.pool_map.remove_entry(&entry.proposal_short_id());
```

`remove_entry` correctly cleans up the expired transaction's own state: it calls `remove_entry_links(id)`, which iterates the expired tx's children and calls `links.remove_parent(&child, id)` for each, severing the parent link from `tx_B`'s record. It also calls `remove_entry_edges`, which removes the expired tx's own `edges.inputs` entries. However, `tx_B` itself is never removed from `entries` or `edges`.

The consequence is a split state:
- `links`: `tx_B` no longer lists `tx_A` as a parent → `calc_ancestors(&tx_B)` returns `∅`
- `edges.inputs`: `O1 → tx_B` remains intact
- `entries`: `tx_B` remains with `Status::Proposed`

This is confirmed by the existing unit test `test_remove_entry` in `tx-pool/src/component/tests/score_key.rs` (L157–167), which explicitly asserts that after `remove_entry(&tx1_id)`, `tx2` and `tx3` remain in the pool.

In `TxSelector::txs_to_commit`, the block assembler iterates `sorted_proposed_iter()` and for each candidate checks:

```rust
// tx-pool/src/component/tx_selector.rs L175-189
let ancestors_ids = self.pool_map.calc_ancestors(&short_id);
if ancestors_ids
    .iter()
    .any(|id| !self.pool_map.has_proposed(id))
{ continue; }
```

Because `calc_ancestors(&tx_B)` returns `∅` (the link was severed), the guard is vacuously false and `tx_B` passes into the block template. The mined block then fails chain validation because `O1` does not exist on-chain (`tx_A` was never committed). The pool is not updated on block rejection, so `tx_B` is selected again in every subsequent template.

Every other removal path uses `remove_entry_and_descendants`:
- `limit_size` → `remove_entry_and_descendants` (L307)
- `remove_by_detached_proposal` → `remove_entry_and_descendants` (L343)
- `resolve_conflict` → `remove_entry_and_descendants` (L310, L321)
- `resolve_conflict_header_dep` → `remove_entry_and_descendants` (L285)

`remove_expired` is the sole path that uses bare `remove_entry`, making it the only removal path that orphans descendants.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Any miner whose pool contains a stale proposed descendant will include it in every block template. Each mined block is invalid and discarded by the network. The miner's effective hash rate contribution to the network drops to zero for the duration of the attack window (up to `expiry_hours`, default 12 h, after the parent expires). An attacker who broadcasts `tx_A`/`tx_B` pairs to all reachable mining nodes simultaneously can suppress a significant fraction of the network's block production at the cost of two transaction fees, for up to 12 hours per attack cycle.

The secondary stale `edges.inputs[O1] → tx_B` entry causes `find_conflict_tx` to return `tx_B` for any new transaction spending `O1`, routing it through the RBF path. Since `O1` does not exist anywhere, the RBF resolution also fails, so this secondary effect does not independently block legitimate transactions but does add spurious error-log noise and unnecessary RBF evaluation overhead.

## Likelihood Explanation

Reachable by any unprivileged user with no special access:
1. Submit `tx_A` (spends any live cell, creates output `O1`) via standard RPC or P2P relay.
2. Submit `tx_B` (spends `O1`) one or more minutes later, ensuring a different expiry timestamp.
3. Both transactions are proposed (appear in a block's proposal zone) within the two-epoch proposal window.
4. After `expiry_hours` (default 12 h), `remove_expired` fires, removes `tx_A`, and leaves `tx_B` in `Status::Proposed`.
5. Every block template from that point includes `tx_B`; every mined block is invalid.

No majority hashpower, no privileged RPC access, and no social engineering are required. The attack is repeatable indefinitely.

## Recommendation

Replace the bare `remove_entry` call in `remove_expired` with `remove_entry_and_descendants`, consistent with all other removal paths:

```rust
// tx-pool/src/pool.rs — remove_expired
for entry in removed {
    let tx_hash = entry.transaction().hash();
    debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
    let evicted = self.pool_map.remove_entry_and_descendants(&entry.proposal_short_id());
    for e in evicted {
        let reject = Reject::Expiry(e.timestamp);
        callbacks.call_reject(self, &e, reject);
    }
}
```

This mirrors `remove_by_detached_proposal` and `limit_size` and ensures both `entries` and `edges`/`links` remain consistent when a transaction expires.

## Proof of Concept

**Minimal unit test plan** (analogous to `test_remove_entry` in `score_key.rs`):

1. Create `tx_A` (spends external cell `C1`, produces output `O1`) and `tx_B` (spends `O1`).
2. Add both to a `PoolMap` with `Status::Proposed`.
3. Call `pool_map.remove_entry(&tx_A.proposal_short_id())` (simulating `remove_expired`).
4. Assert `pool_map.contains_key(&tx_B.proposal_short_id())` → `true` (demonstrates the orphan).
5. Assert `pool_map.calc_ancestors(&tx_B.proposal_short_id()).is_empty()` → `true` (demonstrates the severed link that fools `TxSelector`).
6. Assert `pool_map.edges.inputs` still contains the entry for `O1` → `true` (demonstrates the stale edge).
7. Construct a `TxSelector` over the pool and call `txs_to_commit`; assert `tx_B` appears in the result (demonstrates block template inclusion).

**Manual integration steps:**

1. Run a local CKB node with `expiry_hours = 1` (for faster reproduction).
2. Submit `tx_A` and `tx_B` (child of `tx_A`) via `send_transaction` RPC.
3. Ensure both are proposed (mine two blocks to advance the proposal window).
4. Wait 1 hour for `tx_A` to expire.
5. Call `get_block_template`; observe `tx_B` in the template's `transactions` field.
6. Mine the block; observe the block is rejected by the chain with an `OutPoint` resolution error on `O1`.
7. Call `get_block_template` again; observe `tx_B` is still present.