Audit Report

## Title
`check_tx_fee` Uses Serialized Byte Size Instead of Weight for Minimum Fee Enforcement, Enabling Compute-Heavy DoS - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` computes the minimum required fee using only the serialized byte size (`tx_size`) as the weight argument to `FeeRate::fee()`, which is defined in units of shannons per kilo-weight. For compute-heavy transactions, the true weight (dominated by cycles) can be up to ~60× larger than the byte size. After `verify_rtx` returns the true cycle count in `_process_tx`, no post-verification fee-rate re-check is performed, meaning transactions can be admitted at a fraction of the intended minimum fee rate while forcing full CKB-VM script execution.

## Finding Description

`FeeRate` is defined as shannons per kilo-weight in `util/types/src/core/fee_rate.rs`: [1](#0-0) 

`FeeRate::fee(weight)` computes `fee_rate × weight / 1000`: [2](#0-1) 

The correct weight formula accounts for both size and cycles via `get_transaction_weight`: [3](#0-2) 

However, `check_tx_fee` passes only `tx_size` as the weight, with a comment explicitly acknowledging the unit mismatch: [4](#0-3) 

In `_process_tx`, the flow is:
1. `pre_check` → `check_tx_fee` (uses `tx_size` only, cycles unknown) — passes
2. `verify_rtx` → full CKB-VM script execution → `verified.cycles` now known
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with true cycles
4. **No post-verification fee-rate re-check against true weight** [5](#0-4) 

The pool's internal `fee_rate()` method on `TxEntry` correctly uses `get_transaction_weight(self.size, self.cycles)` for ordering and eviction: [6](#0-5) 

But this is never used as an admission gate. The gap is that `verified.cycles` is available at line 734 of `process.rs` but is never used to re-validate the fee rate against `min_fee_rate`. [7](#0-6) 

## Impact Explanation

This matches the **High** impact category: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

For a transaction with 200 serialized bytes and ~70,000,000 cycles (default `max_tx_verify_cycles`):
- True weight: `70,000,000 × 0.000_170_571_4 ≈ 11,940`
- Fee charged by gate: `1000 × 200 / 1000 = 200 shannons`
- Fee actually required: `1000 × 11,940 / 1000 = 11,940 shannons`
- Discount factor: **~60×**

An attacker can saturate the node's script verification worker pool by submitting many compute-heavy transactions that each consume up to `max_tx_verify_cycles` cycles while paying only the size-based minimum fee. Because the damage occurs during the verification phase (before eviction can act), `max_tx_pool_size` and eviction logic do not prevent it. [8](#0-7) 

## Likelihood Explanation

- Requires only a valid RPC connection — no privileged access, no key material, no majority hashpower.
- Crafting a CKB script that loops for ~70M cycles with a small serialized size is straightforward for any CKB script author.
- The fee cost to the attacker is ~60× lower than the node operator intends, making sustained attacks economically viable.
- The attack is repeatable and parallelizable across multiple nodes simultaneously.
- The code comment at `util.rs` line 42–44 confirms the developers are aware of the approximation but treat it as acceptable, meaning no existing guard closes this gap. [4](#0-3) 

## Recommendation

Add a post-verification fee-rate re-check in `_process_tx` after `verify_rtx` returns the true cycle count:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Re-check fee rate with true weight now that cycles are known
let true_weight = get_transaction_weight(tx_size, verified.cycles);
let true_min_fee = tx_pool_config.min_fee_rate.fee(true_weight);
if fee < true_min_fee {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, true_min_fee.as_u64(), fee.as_u64())), snapshot));
}
```

This should be inserted at line 734 of `process.rs`, immediately after `verified` is obtained. Alternatively, replace the pre-check with a conservative upper-bound estimate using `max_tx_verify_cycles`, or enforce both a per-byte and a per-weight minimum. At minimum, the post-verification re-check must be added to close the admission gap. [7](#0-6) 

## Proof of Concept

1. Construct a CKB transaction whose lock or type script loops for ~70,000,000 cycles but whose serialized size is ~200 bytes.
2. Set the transaction fee to `ceil(1000 × 200 / 1000) = 200 shannons` (at default `min_fee_rate = 1000`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons` → **passes**.
5. The node proceeds to full CKB-VM verification, executing ~70M cycles per worker thread.
6. The transaction is admitted with an actual fee rate of `1000 × 200 / 11940 ≈ 16 shannons/KW` — far below `min_fee_rate = 1000`.
7. Repeat with many concurrent transactions to exhaust the verification worker pool.
8. Confirm: node's verification workers are saturated; legitimate transactions experience severe delays.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-5)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L715-751)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

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
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
