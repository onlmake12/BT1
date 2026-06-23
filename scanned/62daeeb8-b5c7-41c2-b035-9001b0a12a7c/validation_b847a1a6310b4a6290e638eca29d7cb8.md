### Title
`check_tx_fee` Uses Raw Serialized Size Instead of Weight to Enforce `min_fee_rate`, Allowing Fee-Rate Policy Bypass for Cycle-Heavy Transactions — (File: `tx-pool/src/util.rs`)

---

### Summary

In `tx-pool/src/util.rs`, the `check_tx_fee` function enforces the `min_fee_rate` threshold using the transaction's raw serialized byte size (`tx_size`) as the weight argument to `FeeRate::fee()`. However, `FeeRate` is defined in **shannons per kilo-weight**, where weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, weight can be many times larger than `tx_size`. This unit inconsistency means the admission check computes a minimum fee that is far too low, allowing an unprivileged submitter to bypass the `min_fee_rate` policy.

---

### Finding Description

`FeeRate` is defined as shannons per kilo-weight: [1](#0-0) 

The weight of a transaction is: [2](#0-1) 

So `weight = max(tx_size, cycles * 0.000_170_571_4)`. For a transaction with `cycles = 70_000_000` (the default `max_tx_verify_cycles`), the weight from cycles alone is `≈ 11,940` bytes, regardless of how small the serialized size is.

The admission gate in `check_tx_fee` computes the minimum fee as: [3](#0-2) 

The code itself acknowledges the inconsistency with the comment *"Theoretically we cannot use size as weight directly to calculate fee_rate"*, but proceeds to do exactly that. `FeeRate::fee(tx_size as u64)` computes `min_fee_rate * tx_size / 1000`, whereas the correct computation is `min_fee_rate * weight / 1000`.

This function is called on every submitted transaction during `pre_check`: [4](#0-3) 

---

### Impact Explanation

An unprivileged RPC caller can submit a cycle-heavy transaction that passes `check_tx_fee` while paying a fee rate far below `min_fee_rate` (measured in the correct unit, shannons/kilo-weight).

**Concrete example** with `min_fee_rate = 1,000 shannons/kilo-weight`:
- `tx_size = 200 bytes`, `cycles = 70,000,000`
- Correct weight = `max(200, 11,940) = 11,940`
- Correct minimum fee = `1,000 * 11,940 / 1,000 = 11,940 shannons`
- Actual check computes: `1,000 * 200 / 1,000 = 200 shannons`

The attacker pays only **200 shannons** instead of **11,940 shannons** — a ~60× reduction — to pass the admission gate. Such transactions enter the pool, consume pool space, and are deprioritized for mining (since eviction and sorting use the correct weight-based fee rate), but they bypass the spam-prevention policy that `min_fee_rate` is designed to enforce.

---

### Likelihood Explanation

Any unprivileged node reachable via the JSON-RPC `send_transaction` endpoint can exploit this. No special privileges, keys, or majority hashpower are required. The attacker only needs to craft a transaction with high declared cycles and a small serialized body. The `max_tx_verify_cycles` limit (70M by default) bounds the maximum weight amplification factor, but even at modest cycle counts the discrepancy is significant.

---

### Recommendation

Replace the raw `tx_size` argument in `check_tx_fee` with the actual transaction weight:

```rust
// In check_tx_fee, replace:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// With:
let weight = get_transaction_weight(tx_size, rtx.transaction.cycles().unwrap_or(0));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

This aligns the admission check with the unit in which `min_fee_rate` is defined (shannons/kilo-weight) and with how fee rate is computed everywhere else in the pool (eviction, sorting, fee estimator).

---

### Proof of Concept

1. Start a CKB node with `min_fee_rate = 1_000` (default mainnet).
2. Construct a transaction with:
   - Serialized size ≈ 200 bytes
   - Declared cycles = 70,000,000 (max allowed)
   - Fee = 200 shannons (passes `check_tx_fee` since `1000 * 200 / 1000 = 200`)
3. Submit via `send_transaction` RPC.
4. The transaction is admitted to the pool despite having an actual fee rate of `1000 * 200 / 11940 ≈ 16 shannons/kilo-weight`, far below the configured `min_fee_rate = 1,000`.
5. Repeat to fill the pool with sub-threshold-fee-rate transactions, degrading the effectiveness of the spam-prevention policy.

The root cause is at: [5](#0-4) 

compared to the correct weight computation at: [2](#0-1)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```
