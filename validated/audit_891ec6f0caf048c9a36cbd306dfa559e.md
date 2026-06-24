Audit Report

## Title
Fee Rate Minimum Check Uses Serialized Size Instead of Transaction Weight, Allowing Cycle-Heavy Transactions to Bypass the Minimum Fee Rate — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while the canonical transaction weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. After `verify_rtx` returns the actual cycle count, the admission path proceeds directly to `submit_entry` with no second fee-rate check using the true weight. This allows an attacker to submit cycle-heavy transactions whose fee satisfies the size-based gate but is far below the minimum fee rate when measured against true weight, enabling cheap mempool flooding.

## Finding Description
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The developer comment explicitly acknowledges the mismatch. The canonical weight function exists and is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

`check_tx_fee` is called in `pre_check` (lines 289 and 294) before `verify_rtx` runs, so the actual cycle count is unavailable at that point. [3](#0-2) 

In `_process_tx`, after `verify_rtx` returns `verified` (which contains the actual `cycles`), the code immediately constructs a `TxEntry` and calls `submit_entry` with no second fee-rate check:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// ... declared_cycles mismatch check only ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [4](#0-3) 

By contrast, the RPC fee-rate statistics path correctly uses `get_transaction_weight(size, cycles)`: [5](#0-4) 

This creates a split: the admission gate uses size; the statistics path uses weight. The size-based gate is the sole enforcement point.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default `min_fee_rate = 1000` shannons/KW, `tx_size = 1,000` bytes, and `cycles = 70,000,000` (protocol max per tx):
- True weight: `max(1000, 70,000,000 × 0.000_170_571_4)` = **11,940**
- Fee required by `check_tx_fee`: `1000 × 1000 / 1000` = **1,000 shannons**
- Fee required at true weight: `1000 × 11,940 / 1000` = **11,940 shannons**
- Effective fee rate: ≈ **84 shannons/KW** — 11.9× below the configured minimum

An attacker can fill the mempool with cycle-heavy, underpriced transactions at ~1/12th the intended cost, displacing legitimately priced transactions and degrading node and network performance.

## Likelihood Explanation
- Triggerable by any unprivileged user via the `send_transaction` RPC — no key material, privileged access, or majority hashpower required.
- The attacker controls both the script (cycle count) and the fee (capacity delta), making the exploit fully parameterizable.
- Any non-trivial lock or type script naturally consumes many cycles with a small serialized body.
- `max_tx_verify_cycles = 70,000,000` bounds the maximum weight amplification factor at ~12×.
- The attack is repeatable and can be automated to continuously flood the mempool.

## Recommendation
After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before calling `submit_entry` in `_process_tx`:

```rust
use ckb_types::core::tx_pool::get_transaction_weight;

let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This check should be inserted in `_process_tx` after `verify_rtx` completes and before `submit_entry` is called. The existing size-based check in `check_tx_fee` can be retained as the early cheap gate, but must not be the sole enforcement point.

## Proof of Concept
1. Craft a CKB transaction whose lock script runs a tight loop consuming ~70,000,000 cycles. Keep the serialized transaction body small (e.g., 1,000 bytes).
2. Set the fee to exactly `min_fee_rate × tx_size / 1000 = 1,000 × 1,000 / 1,000 = 1,000` shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = FeeRate(1000).fee(1000) = 1,000 shannons`; the fee equals the threshold, so the transaction passes the gate.
5. `verify_rtx` executes the script and returns `cycles = 70,000,000`; no subsequent fee-rate check is performed.
6. The transaction is admitted. Its true weight is `max(1000, 70,000,000 × 0.000_170_571_4) = 11,940`; effective fee rate ≈ 84 shannons/KW — 11.9× below the 1,000 shannons/KW minimum.
7. Repeat to fill the mempool with cycle-heavy, underpriced transactions.

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

**File:** tx-pool/src/process.rs (L286-295)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
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

**File:** tx-pool/src/process.rs (L734-753)
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
```

**File:** rpc/src/util/fee_rate.rs (L103-106)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```
