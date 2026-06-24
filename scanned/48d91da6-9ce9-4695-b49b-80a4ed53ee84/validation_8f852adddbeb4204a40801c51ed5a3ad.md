Audit Report

## Title
Tx-Pool Min-Fee-Rate Admission Check Uses Only Serialized Size, Not Actual Weight — Cycle-Heavy Transactions Bypass the Minimum Fee Rate Guard - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size. The actual block weight used everywhere else in the system — pool sorting, eviction, and block assembly — is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. A transaction sender can craft a small-serialized-size but high-cycle transaction that passes the admission gate while its true effective fee rate is far below `min_fee_rate`. No second fee-rate check is performed after script verification completes and actual cycles are known.

## Finding Description

In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` computes the minimum required fee using only `tx_size`, with the code comment explicitly acknowledging the approximation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(reject);
}
``` [1](#0-0) 

The actual transaction weight used everywhere else is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate()` uses this correct weight for pool sorting and eviction: [3](#0-2) 

The exploit flow in `_process_tx` (`tx-pool/src/process.rs`) is:

1. `pre_check` is called, which calls `check_tx_fee` with only `tx_size` — cycles are unknown at this point. [4](#0-3) 

2. `verify_rtx` runs full script verification and returns actual `verified.cycles`. [5](#0-4) 

3. `TxEntry::new` is constructed with the real cycles, but **no second fee-rate check against actual weight is performed**. [6](#0-5) 

The `limit_size` eviction path does use `entry.fee_rate()` (actual weight), but this is a pool-capacity eviction, not an admission gate — it only fires when `total_tx_size > max_tx_pool_size`, and the transaction is already in the pool at that point. [7](#0-6) 

## Impact Explanation

An attacker can submit transactions with an effective fee rate 40× below the configured `min_fee_rate` (e.g., ~25 shannons/KW vs. the 1,000 shannons/KW minimum). These transactions:

- Pass the admission gate cheaply (size-based check)
- Consume significant CPU on every node during script verification (up to `max_tx_verify_cycles` = 70,000,000 cycles per transaction)
- Accumulate in the pool when it is not full, with effective fee rates far below minimum
- Undermine the economic protection that `min_fee_rate` is designed to provide

This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The fee cost to the attacker is ~300 shannons per transaction (essentially free), while each transaction forces all receiving nodes to execute up to 70M cycles of script verification.

## Likelihood Explanation

- **Entry path**: The standard `send_transaction` RPC, accessible to any unprivileged user.
- **Ease**: Crafting a small-serialized transaction with a lock script that runs a tight computation loop consuming many cycles is straightforward.
- **No special privileges required**: No key, no operator access, no majority hashpower.
- **Self-confirmed by code**: The code comment at `tx-pool/src/util.rs` line 42–44 explicitly acknowledges the discrepancy, confirming this is a known approximation with real consequences. [8](#0-7) 

## Recommendation

After `verify_rtx` completes and `verified.cycles` is known, perform a second fee-rate check using the actual weight before constructing and submitting the `TxEntry`:

```rust
let entry = TxEntry::new(Arc::clone(&rtx), verified.cycles, fee, tx_size);
let actual_fee_rate = entry.fee_rate();
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        // min fee at actual weight
        tx_pool_config.min_fee_rate.fee(get_transaction_weight(tx_size, verified.cycles)).as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and `get_transaction_weight`, ensuring the admission gate is consistent with the weight model used for block packing.

## Proof of Concept

1. Construct a CKB transaction with:
   - A lock script that runs a tight computation loop consuming ~70,000,000 cycles
   - Minimal witnesses and outputs so serialized size is ~300 bytes
   - Fee = 300 shannons (= `min_fee_rate * tx_size / 1000` = `1000 * 300 / 1000`)

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 * 300 / 1000 = 300 shannons`. Fee (300) ≥ min_fee (300) → **admitted**.

4. After verification, `verified.cycles ≈ 70,000,000`. Actual weight = `max(300, 70_000_000 × 0.000_170_571_4)` ≈ 11,940. Actual fee rate = `300 × 1000 / 11,940 ≈ 25 shannons/KW`.

5. The transaction is in the pool with an effective fee rate of 25 shannons/KW — 40× below the configured `min_fee_rate` of 1,000 shannons/KW — with no rejection.

6. Repeat with many UTXOs to flood the pool and force all nodes to expend CPU on verification at negligible fee cost.

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L724-734)
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
```

**File:** tx-pool/src/process.rs (L751-753)
```rust
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
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
