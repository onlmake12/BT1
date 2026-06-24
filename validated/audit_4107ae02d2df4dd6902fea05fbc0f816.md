Audit Report

## Title
Orphan Transaction Pool Admission Bypasses Fee Check, Enabling Free Griefing DoS — (`tx-pool/src/component/orphan.rs`, `tx-pool/src/process.rs`)

## Summary
The CKB orphan transaction pool unconditionally admits transactions whose inputs cannot be resolved, without performing any fee-rate or cycle-density check. Any unprivileged P2P peer can flood the 100-slot orphan pool with zero-fee, structurally-valid transactions referencing fabricated outpoints in a single relay batch, causing random eviction of legitimate orphan transactions at essentially zero cost.

## Finding Description
The admission pipeline is confirmed by direct code inspection:

1. In `pre_check` (`tx-pool/src/process.rs` L286–312), `resolve_tx` is called. When inputs are unknown, the catch-all `Err(err) => Err(err)` branch at L311 fires, returning early. `check_tx_fee` is **never reached** for the unknown-input case. [1](#0-0) 

2. In `after_process` (`tx-pool/src/process.rs` L507–512), when `is_missing_input(reject)` is true, `add_orphan` is called unconditionally with no fee gate. [2](#0-1) 

3. `is_missing_input` (`tx-pool/src/util.rs` L150–152) matches any `Reject::Resolve` where the outpoint is unknown — fabricated outpoints satisfy this trivially. [3](#0-2) 

4. `add_orphan_tx` (`tx-pool/src/component/orphan.rs` L134–158) inserts the entry with no fee or cycle check, then calls `limit_size`, which evicts entries via `HashMap` iterator order (effectively random) once the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS` (100). [4](#0-3) [5](#0-4) 

5. The rate limiter (`sync/src/relayer/mod.rs` L116–123) is keyed by `(peer, message.item_id())` — it counts one token per `RelayTransactions` *message*, not per transaction within it. A single message can carry up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` transactions, so 100 orphan-flooding transactions cost exactly one rate-limit token. [6](#0-5) [7](#0-6) 

## Impact Explanation
This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* A targeted node's orphan pool is permanently saturated, preventing orphan-based transaction chaining from functioning. When a legitimate parent transaction arrives and `process_orphan_tx` is triggered, it finds no matching child — the child is silently lost and must be resubmitted. Sustained flooding keeps the pool permanently full, degrading transaction relay reliability on the targeted node at near-zero attacker cost. [8](#0-7) 

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer. The attacker only needs to construct structurally-valid transactions (passing `non_contextual_verify`) spending fabricated outpoints — no on-chain funds are required and no fees are paid. The orphan pool cap is only 100 entries, fillable in a single relay batch. The attack can be sustained continuously by rotating fabricated outpoints to avoid the duplicate-key check in `add_orphan_tx`. [9](#0-8) 

## Recommendation
Apply a minimum fee-rate or declared-cycle density check before admitting a transaction to the orphan pool:
- In `after_process`, before calling `add_orphan`, verify that `declared_cycle` meets a minimum threshold relative to transaction size (already available as `tx_size`).
- Alternatively, add a per-peer orphan admission counter and cap the number of orphans accepted from a single peer (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / 4`).
- Reject orphans with `declared_cycle == 0` or below the configured `min_fee_rate` equivalent.

## Proof of Concept
1. Connect to a CKB node as a P2P peer via the relay protocol (`SupportProtocols::RelayV3`).
2. Generate 101 transactions, each spending a unique fabricated `OutPoint` (random 32-byte tx hash, index 0). Each transaction must pass `non_contextual_verify`: correct molecule encoding, non-cellbase, within size limits, valid output capacity.
3. Send all 101 transactions in a single `RelayTransactions` message with `declared_cycle = 0`.
4. Each transaction enters `_process_tx` → `pre_check` → `resolve_tx` fails with `OutPointError::Unknown` → `after_process` → `is_missing_input` → `add_orphan`. No fee check occurs.
5. After 100 insertions, `limit_size` randomly evicts one entry per additional insertion. Legitimate orphans already in the pool are evicted.
6. Submit a legitimate parent transaction. `process_orphan_tx` finds no matching child. The child is lost.

### Citations

**File:** tx-pool/src/process.rs (L286-312)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```

**File:** tx-pool/src/component/orphan.rs (L134-158)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
