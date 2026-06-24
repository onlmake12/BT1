Audit Report

## Title
`check_tx_fee` enforces fee rate using size-only weight, allowing high-cycles transactions to bypass the effective fee rate floor — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` computes the minimum required fee using only serialized byte size as the transaction weight. The canonical weight formula `get_transaction_weight(size, cycles)` — used by every other fee-rate consumer in the pool — is never applied after cycles become known in `_process_tx`. A transaction with a small serialized size but near-maximum cycles can pass the admission gate paying only a fraction of the correct minimum fee, allowing an attacker to flood the pool with entries whose effective fee rate is orders of magnitude below `min_fee_rate` at minimal cost.

## Finding Description

**Root cause — `tx-pool/src/util.rs`, line 45:**

`check_tx_fee` computes the minimum fee using only `tx_size`:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment at lines 42–44 explicitly acknowledges this is theoretically incorrect but treats it as a "cheap check." However, no second check is ever performed after cycles are known.

**Canonical weight — `util/types/src/core/tx_pool.rs`, lines 298–303:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

Every other fee-rate consumer (`TxEntry::fee_rate()`, `AncestorsScoreSortKey`, `EvictKey`, fee estimators) calls this function with both size and cycles.

**Missing post-verification check — `tx-pool/src/process.rs`, lines 715–754:**

The flow in `_process_tx` is:
1. Line 715: `pre_check` → `check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)` — size-only gate, cycles unknown.
2. Lines 724–732: `verify_rtx(...)` — actual cycles determined here.
3. Line 751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles.
4. Line 753: `submit_entry(...)` — **no second fee-rate check using the now-known cycles**.

After `verify_rtx` returns, `verified.cycles` is available but is never used to re-evaluate the fee rate before the entry is admitted to the pool.

**Concrete numbers (mainnet defaults, `min_fee_rate` = 1000 shannons/KW, `max_tx_verify_cycles` = 70,000,000):**

| | Value |
|---|---|
| tx_size | 100 bytes |
| cycles | 70,000,000 |
| Size-only min fee (what `check_tx_fee` enforces) | 100 shannons |
| Correct weight: `max(100, 70M × 0.000170571)` | 11,940 |
| Correct min fee | 11,940 shannons |
| Effective fee rate of admitted tx | ≈ 8 shannons/KW (**125× below `min_fee_rate`**) |

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can flood the 180 MB tx-pool with transactions whose effective fee rate is up to ~125× below `min_fee_rate`. These transactions:
- Pass the admission gate and occupy pool capacity.
- Persist until expiry (default 12 hours) if the pool is not full.
- Crowd out legitimate transactions paying the correct fee rate, delaying or preventing their confirmation.
- Consume full script-verification resources (up to `max_tx_verify_cycles` per tx) before the discrepancy is visible.

The attacker's cost is proportional to `min_fee_rate × tx_size`, not to the true weight, making the attack cheap relative to the damage caused.

## Likelihood Explanation

- Entry path: standard `send_transaction` RPC, available to any node peer or local caller with no special privilege.
- Script authors can trivially construct a lock/type script that consumes near-`max_tx_verify_cycles` cycles in a small serialized transaction.
- The discrepancy is structural and reproducible on every node running default configuration.
- No majority hashpower, key material, or victim interaction required.

## Recommendation

After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee-rate check using the correct weight before calling `submit_entry`:

```rust
// After verify_rtx, cycles are known:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let effective_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < effective_min_fee {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, effective_min_fee.as_u64(), fee.as_u64())), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and all pool-internal fee-rate consumers.

## Proof of Concept

```
1. Craft a CKB transaction with:
   - tx_size ≈ 100 bytes (minimal inputs/outputs)
   - A type/lock script that loops near max_tx_verify_cycles (70,000,000)
   - fee = 100 shannons (= min_fee_rate × tx_size / 1000)

2. Submit via send_transaction RPC.

3. Observe: transaction is accepted (check_tx_fee passes: 100 >= 100).

4. Query pool entry via get_pool_tx_detail_info:
   - entry.fee_rate() = fee * 1000 / get_transaction_weight(100, 70_000_000)
                      = 100 * 1000 / 11940 ≈ 8 shannons/KW

5. Repeat with many UTXOs to fill the 180 MB pool with ~8 shannons/KW entries,
   blocking legitimate 1000 shannons/KW transactions from entering.
```

The structural gap is confirmed by the code comment at `tx-pool/src/util.rs` lines 42–44 explicitly acknowledging the theoretical incorrectness of the size-only check, combined with the confirmed absence of any post-verification fee-rate enforcement in `_process_tx` between lines 734 and 753 of `tx-pool/src/process.rs`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
