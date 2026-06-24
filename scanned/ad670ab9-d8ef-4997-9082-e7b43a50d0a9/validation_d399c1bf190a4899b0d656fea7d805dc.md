Audit Report

## Title
Missing Post-Verification Fee Rate Re-Check Allows `min_fee_rate` Policy Bypass via High-Cycle Transactions — (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` validates the minimum fee rate using only the serialized transaction size (`tx_size`), explicitly acknowledged in a code comment as a "cheap check." After `verify_rtx` in `_process_tx` determines the actual consumed cycles, no re-check of the fee rate against the true transaction weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`) is performed before the entry is admitted. An attacker can craft a transaction with high cycles but small serialized size to bypass the `min_fee_rate` admission policy by a factor proportional to the cycles-to-size ratio, enabling spam at a fraction of the intended economic cost.

## Finding Description

**Root cause — `check_tx_fee` uses size only:**

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum fee using only `tx_size`, with a comment explicitly acknowledging this is a "cheap check":

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

**No post-verification re-check in `_process_tx`:**

In `tx-pool/src/process.rs`, `_process_tx` calls `pre_check` (which calls `check_tx_fee`) before running the expensive `verify_rtx`. After `verify_rtx` returns `verified.cycles`, the code only checks `DeclaredWrongCycles` (and only when `declared_cycles` was provided), then immediately creates the entry and submits it with no fee rate re-check against actual weight: [2](#0-1) 

**Admission check vs. eviction/sorting use different metrics:**

`TxEntry::fee_rate()` — used for eviction and sorting — computes the fee rate using the full weight: [3](#0-2) 

`get_transaction_weight` takes the max of size and cycles-based weight: [4](#0-3) 

with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`: [5](#0-4) 

**`submit_entry` has no fee rate guard:**

Inspection of `submit_entry` confirms it performs no fee rate re-check — it handles RBF, context-change time-relative re-verification, and pool size eviction (`limit_size`), but `limit_size` only evicts already-admitted entries to stay within pool size limits; it does not reject the current entry based on its effective fee rate. [6](#0-5) 

**Exploit flow:**
1. Attacker crafts a transaction: small serialized size (e.g., ~300 bytes), lock/type script that loops for ~70,000,000 cycles (the `max_tx_verify_cycles` ceiling), fee = `min_fee_rate.fee(tx_size)`.
2. `pre_check` → `check_tx_fee`: `min_fee = 1000 × 300 / 1000 = 300 shannons`; fee equals minimum — **admission passes**.
3. `verify_rtx` runs the script and returns `verified.cycles ≈ 70,000,000`.
4. No re-check. `TxEntry::new(rtx, 70_000_000, 300_shannons, 300)` is created and submitted.
5. Actual weight = `max(300, 70,000,000 × 0.000170571)` ≈ **11,940**; actual fee rate ≈ **25 shannons/KW** — roughly **40× below** `min_fee_rate`.
6. Transaction is admitted, cached, and relayed to peers, each of which also runs the full 70M-cycle verification.

## Impact Explanation

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

The `min_fee_rate` mechanism is the primary economic barrier against transaction spam. By bypassing it by a factor of 40–125× (depending on actual transaction size), an attacker can flood the network with transactions that each force all receiving nodes to execute up to 70M cycles of script verification. The verification cost is borne by every node that relays the transaction; the attacker pays only a fraction of the fee that `min_fee_rate` was designed to require. This can saturate the tx-pool verify queue, delay legitimate transaction processing, and cause network-wide congestion.

## Likelihood Explanation

The attack is reachable by any unprivileged actor via the `send_transaction` RPC or by relaying a transaction to a peer. No privileged access, key material, or majority hashpower is required. A script that loops for many cycles but has a compact body is straightforward to construct — the script code is deployed separately (as a cell dep), so the transaction body itself remains small. The bypass factor scales linearly with cycles, making it easy to tune. The attack is repeatable as long as the attacker has UTXOs to spend, and the economic cost per attack transaction is ~40–125× lower than the node operator's configured minimum.

## Recommendation

After `verify_rtx` returns actual cycles in `_process_tx`, re-validate the fee rate against the true transaction weight before creating the entry:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Re-check fee rate against actual weight now that cycles are known
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((
        Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, actual_min_fee.as_u64(), fee.as_u64())),
        snapshot,
    ));
}

let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

This mirrors the weight metric already used in `TxEntry::fee_rate`, `AncestorsScoreSortKey`, and `EvictKey`, and closes the gap between the admission check and the actual weight metric. [7](#0-6) 

## Proof of Concept

1. Deploy a CKB lock script on-chain whose body loops for ~70,000,000 cycles (e.g., a tight loop in RISC-V bytecode).
2. Construct a transaction spending a UTXO locked by that script: serialized size ~300 bytes, fee = `min_fee_rate × tx_size / 1000` = 300 shannons (at default `min_fee_rate = 1000 shannons/KW`).
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000 shannons/KW`.
4. Observe: `check_tx_fee` computes `min_fee = 300 shannons`; fee equals minimum — admission passes.
5. `verify_rtx` runs the script and returns `verified.cycles ≈ 70,000,000`.
6. No re-check occurs. `TxEntry::new(rtx, 70_000_000, 300_shannons, 300)` is created.
7. Verify the entry's actual fee rate: `weight = max(300, 11940) = 11940`; `fee_rate = 300×1000/11940 ≈ 25 shannons/KW` — ~40× below `min_fee_rate`.
8. Confirm the transaction is in the pool and is relayed to peers, each of which also executes the full 70M-cycle verification.
9. Repeat with many UTXOs to demonstrate pool saturation and verification queue congestion at a fraction of the intended economic cost.

### Citations

**File:** tx-pool/src/util.rs (L42-52)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** tx-pool/src/process.rs (L96-152)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }

                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/process.rs (L734-754)
```rust
        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```
