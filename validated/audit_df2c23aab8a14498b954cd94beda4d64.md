### Title
Min-Fee-Rate Gate Uses Size-Only Weight, Allowing High-Cycle Transactions to Enter the Pool Below the Effective Minimum Fee Rate — (`tx-pool/src/util.rs`)

---

### Summary

The tx-pool's minimum-fee-rate pre-check (`check_tx_fee`) measures transaction cost using serialized byte size alone, not the actual resource weight that combines both size and cycles. After script verification completes and the true cycle count is known, no second check is performed. A transaction with a small serialized size but a very high cycle count can therefore pass the fee gate while paying a fee rate that is orders of magnitude below the configured minimum, when measured against the actual block resource it consumes.

---

### Finding Description

CKB block space is governed by two independent limits: `max_block_bytes` and `max_block_cycles`. To reduce this to a single-dimensional knapsack problem, `get_transaction_weight` converts both dimensions into a unified "weight":

```
weight = max(tx_size_bytes, cycles * DEFAULT_BYTES_PER_CYCLES)
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (≈ `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`). [1](#0-0) 

This weight is used correctly for fee-rate sorting and eviction inside the pool (`TxEntry::fee_rate()`): [2](#0-1) 

However, the **admission gate** — `check_tx_fee` — computes the minimum required fee using only `tx_size`, not the full weight:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [3](#0-2) 

This check runs inside `pre_check`, before script verification, so cycles are not yet known. After `verify_rtx` completes and the actual cycle count is available, `_process_tx` stores it in the `TxEntry` but performs **no second fee-rate check** against the actual weight: [4](#0-3) 

The default `max_tx_verify_cycles` is `TWO_IN_TWO_OUT_CYCLES * 20 = 70,000,000`: [5](#0-4) 

`TWO_IN_TWO_OUT_CYCLES` itself is a hardcoded constant calibrated against the secp256k1-blake160 script: [6](#0-5) 

A transaction with:
- Serialized size = 200 bytes
- Cycle count = 70,000,000 (at the `max_tx_verify_cycles` ceiling)
- Fee = `min_fee_rate * 200 / 1000 = 200 shannons` (at the default 1000 shannons/KW)

passes `check_tx_fee` because `200 shannons >= min_fee_rate.fee(200)`. But its actual weight is:

```
max(200, 70_000_000 * 0.000_170_571_4) ≈ max(200, 11_940) = 11,940
```

Its effective fee rate is `200 * 1000 / 11,940 ≈ 16.7 shannons/KW` — roughly **1.67% of the 1000 shannons/KW minimum**. The transaction is admitted to the pool and can be mined into a block.

The `TWO_IN_TWO_OUT_CYCLES` constant is also the sole basis for `MAX_BLOCK_CYCLES`, meaning the entire block cycle budget is calibrated against a single, optimistic script benchmark (secp256k1-blake160 sighash), analogous to the report's `BenchmarkERC20` being "very optimized" and unrepresentative of real-world scripts. [7](#0-6) 

---

### Impact Explanation

An unprivileged attacker (RPC caller via `send_transaction`, or P2P relayer via `RelayTransactions`) can submit transactions that:

1. **Consume disproportionate block cycle resources** relative to the fee paid. A block filled with such transactions earns miners far less fee per unit of block capacity consumed.
2. **Persist in the pool** until evicted by the fee-rate-based eviction mechanism (which uses the correct weight), but if the pool is not full, they are never evicted and can be mined.
3. **Displace legitimate transactions** indirectly: miners selecting by fee rate will prefer higher-fee-rate transactions, but the pool's score-sorted iterator uses the correct weight, so these low-effective-fee-rate transactions sit at the bottom and consume pool memory without being evicted unless the pool fills.

The primary impact is **economic griefing of miners and degradation of block cycle utilization**, analogous to the report's "pushing costs onto recipients" by under-accounting transfer costs.

---

### Likelihood Explanation

**High.** The attack requires only:
- Authoring a CKB script (lock or type) that consumes close to `max_tx_verify_cycles` cycles (e.g., a tight computation loop in RISC-V).
- Submitting the transaction via the public `send_transaction` RPC or the P2P relay protocol.
- Paying a fee computed only on the small serialized transaction size.

No privileged access, key material, or majority hashpower is required. The `max_tx_verify_cycles` ceiling (70 million cycles by default) is large enough to make the size-vs-weight discrepancy severe.

---

### Recommendation

After script verification completes and `verified.cycles` is known, perform a second fee-rate check using the actual weight:

```rust
// In _process_tx, after verification:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, pass the declared or verified cycle count into `check_tx_fee` and use `get_transaction_weight` there. For the remote-tx path where `declared_cycles` is available before verification, the declared cycles can be used as an upper bound for an early rejection.

---

### Proof of Concept

**Attacker constructs:**
- A CKB transaction with a lock script that executes a tight RISC-V loop consuming ~70,000,000 cycles.
- Serialized transaction size: ~200 bytes.
- Fee: 201 shannons (just above `min_fee_rate.fee(200) = 200` at 1000 shannons/KW).

**Gate check** (`check_tx_fee`, `tx-pool/src/util.rs:45`):
```
min_fee = 1000 * 200 / 1000 = 200 shannons
fee (201) >= min_fee (200) → PASS
``` [8](#0-7) 

**Actual weight** (computed by `get_transaction_weight`, `util/types/src/core/tx_pool.rs:298`):
```
max(200, 70_000_000 * 0.000_170_571_4) = max(200, 11_940) = 11,940
``` [9](#0-8) 

**Effective fee rate:**
```
201 * 1000 / 11,940 ≈ 16.8 shannons/KW  (minimum is 1000 shannons/KW)
```

**No post-verification fee-rate check exists** in `_process_tx`: [10](#0-9) 

The transaction is admitted to the pool and is eligible for block inclusion, consuming 70,000,000 cycles (2% of `MAX_BLOCK_CYCLES`) for a fee of 201 shannons — approximately 60× below the minimum fee rate the node operator intended to enforce.

### Citations

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/process.rs (L719-751)
```rust
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

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** spec/src/consensus.rs (L69-84)
```rust
/// cycles of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```
