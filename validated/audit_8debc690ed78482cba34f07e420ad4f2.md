Audit Report

## Title
Orphan Tx-Pool Griefing via No Per-Peer Limit Allows Attacker to Evict Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary

`OrphanPool` enforces a global cap of 100 entries with no per-peer submission accounting. An unprivileged relay peer can flood all 100 slots with zero-fee transactions referencing fabricated non-existent inputs. When the pool is full, legitimate orphans are randomly evicted and immediately marked as `Reject` in the relay filter, causing them to be silently dropped and never re-promoted when their parent is confirmed.

## Finding Description

**Root cause — `tx-pool/src/component/orphan.rs` L41–45:**

`OrphanPool` stores entries in a flat `HashMap` with no per-peer tracking:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

The global cap is `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` (L16). `add_orphan_tx()` (L134–159) performs no per-peer check before inserting — the only guard is a duplicate-key check.

**Random eviction — `limit_size()` L119–125:**

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

HashMap iteration order is effectively random — no fee-rate priority, no per-peer fairness.

**Fee check bypass for orphan admission:**

The normal fee check (`check_tx_fee` in `util.rs` L28–54) requires resolved inputs. For orphan transactions, `resolve_tx` fails with `Reject::Resolve(OutPointError::Unknown(...))` before `check_tx_fee` is ever reached. `after_process` in `process.rs` L507–512 catches this via `is_missing_input()` and calls `add_orphan` directly — no fee check is applied. Fabricated zero-fee transactions with random inputs trivially pass `non_contextual_verify` and are admitted.

**Eviction consequence — `process.rs` L557–573:**

Every evicted orphan hash is sent as `TxVerificationResult::Reject` to the relay layer, marking it as unknown/rejected in the bloom filter. The node will not re-request it from peers.

**Orphan promotion failure — `process.rs` L591–597:**

`process_orphan_tx()` performs BFS via `find_orphan_by_previous()`. If the legitimate child was evicted, it is absent from `by_out_point` and is never promoted to pending when its parent is confirmed.

**Attack path:**
1. Attacker connects as an unprivileged relay peer.
2. Attacker sends 100 transactions `T_1..T_100`, each spending a distinct fabricated out-point. Each passes `is_missing_input` and is admitted via `add_orphan_tx`. Pool reaches capacity.
3. Any subsequent legitimate orphan `C` triggers `limit_size()`. If `C` is evicted, `TxVerificationResult::Reject{C.hash}` is sent — `C` is marked unknown in relay filter.
4. When `C`'s parent `P` is confirmed, `process_orphan_tx(P)` finds nothing in `by_out_point` for `P`'s outputs. `C` is never promoted.
5. Attacker's fake orphans persist for `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` (~50 minutes) and can be refreshed before expiry to sustain the attack indefinitely.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** An attacker with a single P2P connection and 100 zero-fee transactions (no on-chain funds required) can continuously disrupt orphan transaction processing on any targeted node. Legitimate users' child transactions are silently dropped and must be manually re-submitted. Deployed against multiple nodes simultaneously, this degrades orphan resolution network-wide during periods of high orphan activity.

## Likelihood Explanation

Any unprivileged peer reachable via the relay protocol can execute this attack. No keys, on-chain funds, or special access are required — orphan admission does not verify input existence and bypasses the fee check. The attack is trivially automatable (100 minimal transactions with random fabricated inputs) and can be sustained indefinitely by refreshing before `ORPHAN_TX_EXPIRE_TIME` expires.

## Recommendation

1. **Add per-peer orphan quota**: Track `peer → count` in `OrphanPool`. Reject `add_orphan_tx` if the submitting peer already holds `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_connected_peers` entries.
2. **Evict from the most-contributing peer**: Replace the random eviction in `limit_size()` with a policy that preferentially evicts from the peer with the most entries, making flooding self-defeating.
3. **Apply minimum fee-rate check before orphan admission**: Reject orphans below `min_fee_rate` before inserting into the pool, raising the cost of fabricating fake orphans.

## Proof of Concept

```
1. Attacker connects to a CKB node as a relay peer.
2. Attacker constructs 100 transactions T_1..T_100, each spending a distinct
   fabricated out-point (tx_hash=random_bytes, index=0). Valid structure,
   non-existent parent. Zero fee (no on-chain funds needed).
3. Attacker sends RelayTransactionHashes for T_1..T_100.
   Node requests them; attacker sends via RelayTransactions.
   Each fails resolution with is_missing_input=true → added to OrphanPool.
   After 100 submissions: OrphanPool.len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS.
4. Honest user U submits child transaction C (parent P not yet in pool).
   C is also missing input → add_orphan_tx called → limit_size() triggers.
   Random eviction: if C is evicted → send_result_to_relayer(Reject{C.hash}).
   C is now marked unknown in relay filter.
5. Parent P is confirmed in a block. process_orphan_tx(P) is called.
   find_orphan_by_previous(P) returns empty (C was evicted and removed from
   by_out_point). C is never promoted to pending.
6. U's transaction C is silently lost. U must manually re-submit.
7. Attacker refreshes T_1..T_100 before ORPHAN_TX_EXPIRE_TIME (~50 min)
   to sustain the attack indefinitely.
```