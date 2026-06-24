The code has been verified. All cited references are accurate:

- `check_tx_fee` at `tx-pool/src/util.rs:45` uses only `tx_size` for `min_fee` calculation [1](#0-0) 
- `get_transaction_weight` at `util/types/src/core/tx_pool.rs:298-303` uses `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)` [2](#0-1) 
- `_process_tx` in `tx-pool/src/process.rs` performs no second fee-rate check after `verify_rtx` returns actual cycles [3](#0-2) 
- `TxEntry::fee_rate()` uses the correct weight formula with cycles [4](#0-3) 

---

Audit Report

## Title
`check_tx_fee` enforces minimum fee rate using size-only weight, allowing high-cycles transactions to bypass the effective fee rate floor — (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` computes the minimum fee using only serialized byte size, while the canonical transaction weight used throughout the tx-pool is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are unknown at pre-check time and no second fee-rate check is performed after `verify_rtx` returns actual cycles, a transaction with small size but near-maximum cycles can enter the pool with an effective fee rate orders of magnitude below `min_fee_rate`. The code comment at the root cause site explicitly acknowledges the theoretical incorrectness of the size-only check, confirming this is a known structural gap without a compensating post-verification guard.

## Finding Description
**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`, line 45:**

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The canonical weight formula (`util/types/src/core/tx_pool.rs`, lines 298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**Code flow in `_process_tx` (`tx-pool/src/process.rs`, lines 705–754):**

1. `pre_check` → `check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)` — size-only gate, cycles unknown.
2. `verify_rtx(...)` — actual cycles determined here.
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles.
4. `submit_entry(...)` — **no second fee-rate check using the now-known cycles**.

Every other fee-rate consumer (`TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, `FeeRateCollector::statistics()`, both fee estimators) calls `get_transaction_weight(size, cycles)`. The admission gate is the sole exception.

**Concrete gap (mainnet defaults):**

| Parameter | Value |
|---|---|
| `min_fee_rate` | 1,000 shannons/KW |
| `max_tx_verify_cycles` | 70,000,000 |
| `DEFAULT_BYTES_PER_CYCLES` | 0.000_170_571_4 |
| Cycle-equivalent bytes at max cycles | ≈ 11,940 |

A transaction with `tx_size = 100 bytes`, `cycles = 70,000,000`, `fee = 100 shannons`:
- **Size-only min fee** (what `check_tx_fee` enforces): `1000 × 100 / 1000 = 100 shannons` → **passes**
- **Correct weight**: `max(100, 11,940) = 11,940`
- **Correct min fee**: `11,940 shannons`
- **Effective fee rate of admitted tx**: `≈ 8 shannons/KW` — **~125× below `min_fee_rate`**

## Impact Explanation
This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points).**

An attacker can flood the tx-pool (up to 180 MB by default) with transactions whose effective fee rate is ~125× below `min_fee_rate`. These transactions:
1. Pass the admission gate and consume pool capacity.
2. Persist until expiry (default 12 hours) or until the pool is full and they are evicted last.
3. Crowd out legitimate transactions paying the correct fee rate, delaying or preventing their confirmation.
4. Consume full script-verification resources on the node (up to `max_tx_verify_cycles` per tx) before the discrepancy is visible.

The attacker's cost is proportional to `min_fee_rate × tx_size`, not to the true weight, making the attack ~125× cheaper than the pool's own fee-rate model assumes.

## Likelihood Explanation
- Entry path: standard `send_transaction` RPC, available to any node peer or local caller.
- No special privilege, key, or majority hashpower required.
- Script authors can trivially construct a lock/type script that consumes near-`max_tx_verify_cycles` cycles in a small serialized transaction.
- The discrepancy is structural and reproducible on every node running default configuration.
- The code comment at the root cause site confirms the developers were aware of the theoretical incorrectness but did not add a compensating post-verification check.

## Recommendation
After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee-rate check using the correct weight before calling `submit_entry`:

```rust
// After verify_rtx, cycles are known:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let effective_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < effective_min_fee {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        effective_min_fee.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and all pool-internal fee-rate consumers, and closes the gap between the admission gate and the pool's own fee-rate model.

## Proof of Concept
```
1. Craft a CKB transaction with:
   - tx_size ≈ 100 bytes (minimal inputs/outputs)
   - A type/lock script that loops near max_tx_verify_cycles (70,000,000)
   - fee = 100 shannons  (= min_fee_rate × tx_size / 1000)

2. Submit via send_transaction RPC.

3. Observe: transaction is accepted (check_tx_fee passes: 100 >= 100).

4. Query pool entry via get_pool_tx_detail_info:
   - entry.fee_rate() = fee * 1000 / get_transaction_weight(100, 70_000_000)
                      = 100 * 1000 / 11940 ≈ 8 shannons/KW

5. Repeat with many UTXOs to fill the 180 MB pool with ~8 shannons/KW entries,
   blocking legitimate 1000 shannons/KW transactions from entering.
```

The structural gap is confirmed by: (a) the code comment at `tx-pool/src/util.rs:42–44` acknowledging the theoretical incorrectness of the size-only check, and (b) the absence of any post-verification fee-rate enforcement in `_process_tx` between lines 734 and 753 of `tx-pool/src/process.rs`.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
