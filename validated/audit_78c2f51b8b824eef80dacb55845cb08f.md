All code references in the report have been verified against the actual codebase. Here is the validation result:

---

Audit Report

## Title
Tx-Pool Minimum Fee Rate Admission Check Uses Size-Only Weight, Allowing High-Cycle Transactions to Bypass the Fee Gate - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces `min_fee_rate` using only serialized transaction size as the weight denominator, explicitly acknowledged in a code comment as a "cheap check." After `verify_rtx` returns the actual cycle count in `_process_tx`, no second weight-based fee-rate check is performed before the `TxEntry` is admitted. An attacker can craft small-but-high-cycle transactions that pass the size gate with minimal fees but carry a true weight-based fee rate far below `min_fee_rate`.

## Finding Description

In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` uses `tx_size` as the sole weight denominator with an explicit comment acknowledging this is intentional:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

This check is called in `pre_check` at `tx-pool/src/process.rs` lines 289 and 294, before script verification: [2](#0-1) 

After `pre_check` passes, `_process_tx` calls `verify_rtx` to obtain the actual cycle count (lines 724–732), then at line 751 directly constructs `TxEntry::new(rtx, verified.cycles, fee, tx_size)` and calls `submit_entry` — with no intervening weight-based fee-rate check: [3](#0-2) 

Once admitted, the entry's true fee rate is computed using the full weight via `get_transaction_weight(self.size, self.cycles)`: [4](#0-3) 

Where `get_transaction_weight` is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)` and `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`: [5](#0-4) 

**Concrete example** with default config (`max_tx_verify_cycles = 70_000_000`, `min_fee_rate = 1_000`):
- Tx size: 1,000 bytes → passes size check with fee = 1,000 shannons
- Script cycles: 70,000,000
- True weight: `max(1000, 70_000_000 × 0.000_170_571_4)` = `max(1000, 11,940)` = **11,940**
- Actual fee rate: `1000 × 1000 / 11940` ≈ **83 shannons/KB** — ~12× below the 1,000 shannons/KB minimum

The `limit_size` eviction function triggers only when `total_tx_size > max_tx_pool_size` (serialized bytes, not weight), so small-but-high-cycle transactions do not trigger eviction promptly: [6](#0-5) 

Eviction ordering uses the weight-based `EvictKey`, meaning attacker transactions are evicted first — but only after they have already occupied pool space and consumed verification resources: [7](#0-6) 

## Impact Explanation

**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can flood the tx-pool with transactions that pass the size-based fee gate with minimal fees, consume up to `max_tx_verify_cycles` (70M) cycles per transaction during script verification, and carry a true weight-based fee rate ~12× below `min_fee_rate`. Filling the pool with ~180,000 such 1,000-byte transactions costs ~1.8 CKB in fees. The resulting verification backlog delays legitimate transactions and degrades miner block assembly quality, constituting concrete network congestion at negligible attacker cost.

## Likelihood Explanation

Reachable by any unprivileged actor via the `send_transaction` JSON-RPC endpoint (no declared cycles required; `max_block_cycles` is used as the limit) or the P2P relay protocol. The attacker only needs to deploy a lock script that runs a tight RISC-V loop consuming ~70M cycles, which is straightforward on CKB given the programmable CKB-VM. The discrepancy between size-based and weight-based fee rates is maximized when cycles dominate, which is common for non-trivial smart contract transactions. The attack is repeatable and cheap.

## Recommendation

After `verify_rtx` returns the actual cycle count in `_process_tx`, add a second fee-rate check using the true weight before constructing the `TxEntry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

This mirrors the approach already used for pool ordering (`AncestorsScoreSortKey`) and eviction (`EvictKey`), and closes the gap between the admission gate and the actual resource cost. The existing size-only check in `check_tx_fee` can remain as a fast pre-filter before script execution.

## Proof of Concept

1. Deploy a CKB lock script that runs a tight RISC-V loop consuming ~70,000,000 cycles.
2. Craft a transaction with one input cell locked by the above script, one output cell, serialized size ≈ 1,000 bytes, and fee = 1,000 shannons (exactly `min_fee_rate × size / 1000`).
3. Submit via `send_transaction` RPC.
4. Observe: `check_tx_fee` passes (fee ≥ `min_fee_rate.fee(1000)` = 1,000 shannons).
5. After script verification, `verified.cycles` ≈ 70,000,000. True weight ≈ 11,940. Actual fee rate ≈ 83 shannons/KB.
6. The `TxEntry` is admitted with `fee_rate()` ≈ 83 shannons/KB, far below the 1,000 shannons/KB minimum.
7. Repeat ~180,000 times (total cost ~1.8 CKB) to fill the pool by serialized size, exhausting the verification pipeline and occupying pool space with economically unviable entries.

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

**File:** tx-pool/src/process.rs (L287-295)
```rust
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
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

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```
