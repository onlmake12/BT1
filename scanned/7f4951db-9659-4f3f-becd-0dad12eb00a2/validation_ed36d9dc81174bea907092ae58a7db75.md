Audit Report

## Title
`check_tx_fee` Enforces Min-Fee-Rate Using Serialized Size Only, Allowing Cycle-Heavy Transactions to Bypass the Fee Rate Floor — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, while the actual CKB transaction weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are unknown at pre-check time and no post-verification fee-rate re-check is performed after `verify_rtx`, a transaction with high cycles but small serialized size is admitted to the tx-pool with an actual fee rate far below `min_fee_rate`. This allows an attacker to flood the tx-pool at roughly 1–2% of the intended admission cost.

## Finding Description
In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` explicitly uses `tx_size` as the weight denominator with a comment acknowledging this is intentional as a "cheap check":

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The actual weight formula in `util/types/src/core/tx_pool.rs` lines 298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

In `_process_tx` (`tx-pool/src/process.rs` lines 715–754), the flow is:
1. `pre_check` → `check_tx_fee(tx_pool, snapshot, &rtx, tx_size)` — cycles unknown, size-only check
2. `verify_rtx(...)` — script execution runs; `verified.cycles` is now known
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with actual cycles
4. `submit_entry(...)` — entry admitted with no subsequent fee-rate re-check

There is no call to `get_transaction_weight` or any weight-based fee rate validation after `verify_rtx` returns. The size-only pre-check is the sole admission gate.

## Impact Explanation
An attacker crafts a transaction with minimal serialized size (~200 bytes) and a script consuming maximum cycles (~70M). The size-only check requires only 200 shannons at `min_fee_rate = 1000 shannons/KW`. The actual weight is `max(200, 70_000_000 × 0.000170571) = 11,940` bytes, requiring 11,940 shannons. The attacker pays 201 shannons — approximately 1.7% of the intended cost — and the transaction is admitted and relayed. Repeating this floods the tx-pool and P2P relay network at a fraction of the intended cost.

This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack requires only an unprivileged RPC caller or P2P relay peer. The attacker must deploy a CKB-VM script that consumes many cycles (e.g., a tight loop) but has a small serialized footprint — straightforward for anyone familiar with CKB script development. No privileged access, key material, or majority hashpower is required. The attack is repeatable and cheap per transaction.

## Recommendation
After `verify_rtx` returns `verified.cycles`, re-check the fee rate using the actual weight before admitting the entry in `_process_tx`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        actual_min_fee.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

Alternatively, in `check_tx_fee`, substitute `max_tx_verify_cycles` for cycles as a conservative upper bound so the pre-check is never more permissive than the post-check would be.

## Proof of Concept
**Setup:** Node configured with `min_fee_rate = 1000 shannons/KW`.

**Transaction parameters:**
- Serialized size: 200 bytes
- Script: tight CKB-VM loop consuming 70,000,000 cycles
- Fee paid: 201 shannons

**Step 1 — `check_tx_fee` (pre-check, cycles unknown):**
```
min_fee = 1000 × 200 / 1000 = 200 shannons
fee (201) >= min_fee (200) → PASS
```

**Step 2 — `verify_rtx` (cycles = 70,000,000 now known):**
No fee-rate re-check is performed. Entry is created and submitted directly.

**Step 3 — Actual fee rate:**
```
weight = max(200, 70_000_000 × 0.000170571) = 11,940
actual_fee_rate = 201 × 1000 / 11,940 ≈ 16.8 shannons/KW
```

This is ~98.3% below the configured `min_fee_rate` of 1000 shannons/KW. The transaction is admitted and relayed. Repeating with many such transactions floods the pool at ~1.7% of the intended admission cost. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** tx-pool/src/process.rs (L715-754)
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
        try_or_return_with_snapshot!(ret, submit_snapshot);
```
