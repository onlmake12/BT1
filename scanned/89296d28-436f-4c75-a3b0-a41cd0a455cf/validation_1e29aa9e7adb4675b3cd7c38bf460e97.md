Audit Report

## Title
Incomplete Fee-Rate Admission Check Omits Cycles Component, Allowing Below-Minimum-Fee-Rate Transactions Into the Pool — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` validates the minimum fee using only the transaction's serialized byte size, while the canonical weight used everywhere else in the system is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A submitter can craft a small-serialized-size, high-cycles transaction whose fee satisfies `fee ≥ min_fee_rate × size` yet whose true fee rate (computed with proper weight) is far below `min_fee_rate`. No second fee-rate gate is applied after cycles become known, so the transaction is admitted and persists in the pool.

## Finding Description

`check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment itself acknowledges the discrepancy. The canonical weight function used everywhere else is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`TxEntry::fee_rate()` uses this proper weight for sorting, eviction, and fee estimation. The full submission flow in `_process_tx` is:

1. `pre_check` → calls `check_tx_fee` with size only (line 289 / 294 of `process.rs`)
2. `verify_rtx` → executes scripts, returns actual `verified.cycles` (line 724)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry constructed with real cycles (line 751)
4. `submit_entry` — no second fee-rate check applied

There is no gate between steps 2 and 4 that re-evaluates the fee rate using the now-known cycles value.

## Impact Explanation

An attacker submitting a 200-byte transaction consuming 5,000,000 cycles pays only `min_fee_rate × 200 / 1000 = 200 shannons` to pass `check_tx_fee`. The true weight is `max(200, 5_000_000 × 0.000_170_571_4) ≈ 852`, giving a true fee rate of `≈ 236 shannons/KB` against a `min_fee_rate` of `1000 shannons/KB`. The transaction enters the pool at ~4× below the minimum threshold.

The pool's `limit_size` eviction uses `EvictKey` which calls `fee_rate()` (proper weight), so the attacker's transactions are evicted first when the pool is full — but the attacker can continuously resubmit at negligible cost (`min_fee_rate × size` shannons per attempt), causing:

- **Pool pollution**: below-threshold-fee-rate transactions occupy pool slots, displacing legitimate transactions
- **Fee estimation distortion**: `estimate_fee_rate` in `pool_map.rs` and `FeeRateCollector` in `rpc/src/util/fee_rate.rs` use proper weight; admitted low-fee-rate transactions distort the fee market signal
- **CPU DoS**: each submission triggers a full `verify_rtx` execution (up to `max_tx_verify_cycles` cycles of script execution) for a transaction that will be immediately evicted, at a cost to the attacker far below what proper weight-based admission would require

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points).

## Likelihood Explanation

Any node with a public RPC endpoint is reachable via `send_transaction` with no privilege required. Crafting a small-serialized-size, high-cycles transaction is straightforward for any script author. The attack is cheap and repeatable: the attacker pays only `min_fee_rate × size` shannons per submission, which is a fraction of the fee that proper weight-based admission would require. The attack can be sustained indefinitely.

## Recommendation

After `verify_rtx` returns the actual cycles in `_process_tx`, apply a second fee-rate check using the proper weight before constructing the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and `estimate_fee_rate` in `pool_map.rs`. The existing size-only check in `check_tx_fee` can remain as the cheap pre-verification gate; the weight-based check becomes the authoritative post-verification gate.

## Proof of Concept

1. Construct a CKB transaction with a lock script that runs a tight loop consuming ~5,000,000 cycles. Serialized size: ~200 bytes.
2. Set outputs capacity such that `fee = 201 shannons` (just above `min_fee_rate × 200 / 1000 = 200 shannons` at `min_fee_rate = 1000 shannons/KB`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; `201 ≥ 200` → passes.
5. `verify_rtx` executes the script, returning `cycles = 5_000_000`.
6. `TxEntry` is created; `fee_rate() = FeeRate::calculate(201, max(200, 852)) ≈ 236 shannons/KB` — well below `min_fee_rate = 1000 shannons/KB`.
7. The transaction is now in the pool with a fee rate ~4× below the minimum threshold.
8. Repeat continuously: each iteration costs ~201 shannons and forces the node to execute 5,000,000 cycles of script verification. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/process.rs (L715-753)
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

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
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
