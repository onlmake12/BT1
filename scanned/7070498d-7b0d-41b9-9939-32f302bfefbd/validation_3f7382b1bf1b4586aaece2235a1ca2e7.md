Audit Report

## Title
Fee Rate Admission Check Uses Serialized Size Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` computes the minimum required fee using raw serialized byte size (`tx_size`) as the weight argument to `FeeRate::fee()`, but `FeeRate` is defined in units of shannons per kilo-weight, where weight = `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. This is the sole admission fee-rate gate: no weight-based fee check is performed after `verify_rtx` returns the actual cycle count. An unprivileged attacker can submit a cycle-heavy transaction with a fee as low as ~2% of the true weight-based minimum and have it admitted to the tx-pool.

## Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`, line 45:**

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

`FeeRate::fee(weight)` in `util/types/src/core/fee_rate.rs` computes `shannons = rate × weight / 1000`, where the type is documented as "shannons per kilo-weight." Passing `tx_size` (raw bytes) instead of `get_transaction_weight(tx_size, cycles)` underestimates the minimum fee for any transaction where `cycles × DEFAULT_BYTES_PER_CYCLES > tx_size`.

The code comment at lines 42–44 acknowledges this explicitly: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* However, the comment implies a subsequent weight-based check exists — it does not.

**Confirmed absence of post-execution weight check — `tx-pool/src/process.rs`, `_process_tx`:**

```
pre_check(&tx)          // calls check_tx_fee (size-based) — ONLY fee gate
  → verify_rtx(...)     // returns verified.cycles
  → TxEntry::new(rtx, verified.cycles, fee, tx_size)
  → submit_entry(...)   // no weight-based fee check here
```

After `verify_rtx` returns the actual cycle count, `_process_tx` creates the `TxEntry` and calls `submit_entry` directly with no additional fee-rate validation. `TxEntry::fee_rate()` does use `get_transaction_weight` correctly, but only for internal pool sorting and eviction — not for admission rejection.

**Unit mismatch arithmetic for worst-case transaction:**
- `tx_size` = 242 bytes (minimal transaction)
- `cycles` = 70,000,000 (`max_tx_verify_cycles`)
- `weight` = max(242, 70,000,000 × 0.000_170_571_4) ≈ 11,940
- `min_fee` enforced = 1,000 × 242 / 1,000 = **242 shannons**
- `min_fee` correct = 1,000 × 11,940 / 1,000 = **11,940 shannons**
- Effective admitted fee rate ≈ **20 shannons/KW** vs. configured 1,000 shannons/KW (~49× bypass)

## Impact Explanation

This directly enables **CKB network congestion with few costs** (High, 10001–15000 points). An attacker can fill the 180 MB tx-pool with cycle-heavy transactions at 242 bytes each (~743,000 transactions), paying only 242 shannons per transaction (~180 million shannons total ≈ 1.8 CKB). Each transaction also forces the node to execute up to 70,000,000 cycles of script verification before admission, compounding the CPU DoS. Legitimate transactions paying the correct weight-based fee rate are displaced or delayed. The `min_fee_rate` spam-prevention mechanism is rendered ineffective for any transaction using non-trivial lock or type scripts.

## Likelihood Explanation

**Medium-High.** The attack path is fully unprivileged — any caller of the `send_transaction` RPC or any P2P relay peer can submit transactions. Crafting a cycle-heavy script (e.g., a tight computation loop in a lock script) is within the capability of any CKB script author. The `max_tx_verify_cycles` cap bounds the maximum bypass ratio at ~49× but does not prevent the attack. The attack is repeatable and cheap.

## Recommendation

After `verify_rtx` returns `verified.cycles` in `_process_tx` (`tx-pool/src/process.rs`), add a weight-based fee check:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Post-execution weight-based fee check (cycles now known)
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

The existing size-based check in `check_tx_fee` can remain as a fast pre-filter before script execution. The post-execution check enforces the true weight-based minimum.

## Proof of Concept

1. Write a CKB lock script that executes a tight computation loop consuming ~70,000,000 cycles.
2. Deploy the script on a testnet node and create a cell locked by it.
3. Build a transaction spending that cell. Serialized size ≈ 242 bytes.
4. Set the transaction fee to exactly 242 shannons (= `1000 × 242 / 1000`, the size-based minimum).
5. Submit via `send_transaction` RPC to a node configured with `min_fee_rate = 1000`.
6. **Observed**: Transaction is admitted to the pool. Effective fee rate ≈ 20 shannons/KW, ~49× below the configured minimum.
7. **Expected**: Transaction is rejected with `LowFeeRate` because the weight-based minimum fee is 11,940 shannons.
8. Repeat in a loop to fill the 180 MB pool at a total cost of ~1.8 CKB, displacing legitimate transactions.