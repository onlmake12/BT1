Audit Report

## Title
Size-Only Minimum Fee Check Allows Cycles-Heavy Transactions to Underpay for Pool Admission - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only the transaction's serialized byte size, ignoring consumed cycles. Because CKB blocks are constrained by two independent limits (bytes and cycles), the correct resource unit is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An attacker can craft transactions that are tiny in bytes but consume near-maximum cycles, passing the fee gate at a fraction of the cost that reflects their true block-resource footprint, causing verification workers to be exhausted at minimal cost while the pool fills with permanently unmined transactions.

## Finding Description

`check_tx_fee` is called inside `pre_check` before `verify_rtx` runs: [1](#0-0) 

It uses only `tx_size` for the fee threshold: [2](#0-1) 

The code's own comment acknowledges this is a deliberate approximation ("cheap check"). After `verify_rtx` completes and actual cycles are known, there is no second fee check using composite weight: [3](#0-2) 

The `TxEntry` is constructed with the real cycles, and `fee_rate()` correctly uses composite weight for prioritization: [4](#0-3) 

This creates a structural split: **admission uses size-only; prioritization and mining selection use composite weight**. A transaction that passes the size-based gate may have a composite fee rate far below `min_fee_rate`, meaning it will never be mined but still occupies pool space and consumes verification cycles.

The pool size eviction (`limit_size`) is triggered only when `total_tx_size > max_tx_pool_size` (byte-based), and evicts by composite fee rate: [5](#0-4) 

This does not compensate for the gap: the attacker's transactions are admitted, verified at full cost, and only evicted when the byte limit is reached — by which point verification workers have already been consumed.

The relevant constants confirm the attack parameters: [6](#0-5) [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

At default settings (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = TWO_IN_TWO_OUT_CYCLES × 20 = 70,000,000`):

- A ~100-byte transaction passes `check_tx_fee` with `min_fee = 100 shannons`.
- Its composite weight = `max(100, 70,000,000 × 0.000170571) = 11,940`.
- Its actual composite fee rate ≈ 8 shannons/KW — 119× below `min_fee_rate`.
- Each such transaction triggers full script execution (up to 70M cycles) in `verify_rtx`, consuming a verification worker thread.
- The transaction enters the pool and is never selected for block assembly (composite fee rate too low), occupying a pool slot for up to 12 hours (`DEFAULT_EXPIRY_HOURS`).
- The 180MB byte-based pool limit allows a very large number of 100-byte transactions, each burning 70M cycles of CPU during admission.
- Repeated submission exhausts verification workers and degrades node responsiveness for legitimate transactions, constituting network congestion at minimal attacker cost.

## Likelihood Explanation

The attack requires only an unprivileged `send_transaction` RPC call. No special role, key, or hashpower is needed. The attacker needs only enough CKB to pay the size-based minimum fee (100 shannons per transaction at default settings — negligible). The attack is repeatable as long as the attacker controls UTXOs. The discrepancy is structural and deterministic, independent of network conditions. The script cell can be pre-deployed on-chain and referenced as a cell dep, keeping the transaction itself tiny.

## Recommendation

After `verify_rtx` completes and cycles are known, perform a second fee check using composite weight before admitting the transaction to the pool:

```rust
// In _process_tx, after verify_rtx:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

If a pre-verification cheap check is still desired for early rejection, it should use `max_tx_verify_cycles` as a conservative upper bound for cycles to compute a worst-case weight, ensuring no cycles-heavy transaction can slip through at a size-only rate.

## Proof of Concept

1. Deploy a lock script on-chain that executes a tight RISC-V loop consuming ~70,000,000 cycles. The script binary itself is in a cell dep and does not inflate the transaction's serialized size.
2. Craft a transaction spending a UTXO locked by that script. Serialized size ≈ 100 bytes. Set fee = `min_fee_rate.fee(100)` = 100 shannons.
3. Submit via `send_transaction` RPC.
4. Node calls `pre_check` → `check_tx_fee(tx_pool, snapshot, rtx, 100)`: `min_fee = 100`, `fee = 100`, check passes.
5. Node calls `verify_rtx` with `max_tx_verify_cycles = 70,000,000`: script executes for ~70M cycles. `verified.cycles ≈ 70,000,000`.
6. `TxEntry` is created with `cycles = 70,000,000`, `size = 100`, `fee = 100`.
7. `entry.fee_rate()` = `FeeRate::calculate(100, max(100, 11940))` ≈ 8 shannons/KW — far below `min_fee_rate = 1000`.
8. Transaction enters the pool, is never selected for block assembly, and occupies a slot for 12 hours.
9. Repeat with many UTXOs. Each iteration burns 70M cycles of verification worker CPU at a cost of 100 shannons. Pool fills with unmined transactions; legitimate transactions are delayed or evicted.

### Citations

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L724-754)
```rust
        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

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

**File:** tx-pool/src/util.rs (L42-53)
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
    Ok(fee)
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** util/app-config/src/legacy/tx_pool.rs (L14-20)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
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
