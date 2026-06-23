### Title
`check_tx_fee` Uses Raw `tx_size` Instead of Proper Transaction Weight for Min-Fee Enforcement — (`File: tx-pool/src/util.rs`)

---

### Summary

In `tx-pool/src/util.rs`, the `check_tx_fee` function computes the minimum required fee using only the serialized byte size of the transaction (`tx_size`) as the weight parameter. The correct weight is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, this underestimates the weight, causing the minimum fee threshold to be set too low. An unprivileged RPC caller can submit cycle-heavy transactions with fees below the proper minimum fee rate and have them admitted to the tx-pool.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate at tx-pool admission time:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself acknowledges the approximation. The correct weight formula, defined in `util/types/src/core/tx_pool.rs`, is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a cycle-heavy transaction where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`, the true weight is dominated by cycles, not bytes. By using only `tx_size`, `check_tx_fee` computes a `min_fee` that is strictly lower than what the proper weight would require.

Every other fee-rate-sensitive path in the codebase uses `get_transaction_weight`. For example, `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` uses:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

And the block assembler and eviction logic all use `get_transaction_weight`. Only the admission gate (`check_tx_fee`) uses the raw size.

---

### Impact Explanation

An unprivileged tx-pool submitter (RPC caller via `send_transaction`) can craft a cycle-heavy transaction — one where `cycles * DEFAULT_BYTES_PER_CYCLES >> tx_size` — and attach a fee that satisfies `fee >= min_fee_rate.fee(tx_size)` but violates `fee >= min_fee_rate.fee(get_transaction_weight(tx_size, cycles))`. Such a transaction passes `check_tx_fee` and is admitted to the pool. Once inside, it occupies pool capacity and competes for pool slots, potentially displacing legitimate transactions via the pool-full eviction path. The transaction will not be prioritized for mining (miners use proper weight-based fee rate), but it can persist in the pool and consume resources. This is a pool-admission bypass for the minimum fee rate policy on cycle-heavy transactions.

**Likelihood: High** — the wrong value is used on every admission for any transaction where cycles dominate the weight. No special conditions are required beyond submitting a cycle-heavy transaction.

**Impact: Low-to-Medium** — the pool's eviction and mining selection use correct weight-based fee rates, so the attacker's transactions are deprioritized for block inclusion. However, pool space is consumed by under-priced transactions, and the min-fee-rate policy — a spam-prevention mechanism — is bypassed at the admission gate.

---

### Likelihood Explanation

The condition `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size` is easily achievable. A small transaction (e.g., 200 bytes) running a script consuming ~1,200,000 cycles would have a weight of `max(200, 1_200_000 * 0.000_170_571_4) ≈ max(200, 204) = 204`. At higher cycle counts the divergence grows. Any script author can craft such a transaction. The wrong value is used unconditionally — there is no fallback to the correct weight even when cycles are available from the verification cache.

---

### Recommendation

Replace the raw `tx_size` with `get_transaction_weight(tx_size, cycles)` in `check_tx_fee`. Since cycles are not yet known at the pre-verification admission stage, the function should either:

1. Accept `cycles` as a parameter (available from the cache entry when present) and use `get_transaction_weight` when cycles are known, falling back to `tx_size` only when cycles are unavailable (cache miss), or
2. Perform a second fee-rate check after `verify_rtx` returns the actual cycles, using the proper weight at that point.

The comment in the code already acknowledges the theoretical incorrectness; the fix is to use the proper weight when cycles are available.

---

### Proof of Concept

**Root cause location:** [1](#0-0) 

**Correct weight formula (not used in `check_tx_fee`):** [2](#0-1) 

**Correct usage in `TxEntry::fee_rate` (post-admission):** [3](#0-2) 

**`FeeRate::fee` — the function called with the wrong weight:** [4](#0-3) 

**Attack entry point — `send_transaction` RPC → `check_tx_fee`:** [5](#0-4) 

**Scenario:**

1. Attacker constructs a transaction with a small serialized size (e.g., 200 bytes) but a script that consumes a large number of cycles (e.g., 10,000,000 cycles).
2. True weight = `max(200, 10_000_000 * 0.000_170_571_4)` ≈ `max(200, 1705)` = **1705**.
3. With `min_fee_rate = 1000 shannons/KW`, the correct `min_fee = 1000 * 1705 / 1000 = 1705 shannons`.
4. But `check_tx_fee` computes `min_fee = 1000 * 200 / 1000 = 200 shannons`.
5. Attacker sets fee = 201 shannons. The transaction passes `check_tx_fee` (201 > 200) and is admitted to the pool, despite its true fee rate being `1000 * 201 / 1705 ≈ 117 shannons/KW` — well below the 1000 shannons/KW minimum.
6. The pool fills with such transactions, bypassing the min-fee-rate spam filter.

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```
