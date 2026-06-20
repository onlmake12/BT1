### Title
`min_fee_rate` Admission Check Uses Size-Only Metric While Actual Fee Rate Uses Cycle-Weighted Metric, Allowing High-Cycle Transactions to Bypass the Minimum Fee Rate Guard — (`tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission check in `check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, while the actual fee rate used for block assembly, eviction, and pool ordering uses `get_transaction_weight(size, cycles)` = `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For transactions with high cycle counts, the weight can be orders of magnitude larger than the size alone. This creates two conflicting fee-rate calculation paths: a transaction can pass the admission gate while its true effective fee rate is far below `min_fee_rate`. The conflict is structurally analogous to the JBXBuybackDelegate issue, where one code path forces a value to zero, rendering a safety check in the other path ineffective.

---

### Finding Description

**Path 1 — Admission check (`check_tx_fee`):**

In `tx-pool/src/util.rs`, the minimum fee is computed using `tx_size` only:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
```

The code comment explicitly acknowledges the discrepancy: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [1](#0-0) 

**Path 2 — Actual fee rate (`TxEntry::fee_rate`):**

In `tx-pool/src/component/entry.rs`, the fee rate stored in the pool entry and used for block assembly, eviction, and relay ordering uses the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

**The weight function:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

**Concrete example of the bypass:**

Consider a transaction with:
- `tx_size = 200` bytes
- `cycles = 70,000,000` (the default `max_tx_verify_cycles`)
- `fee = 200` shannons (just enough to pass the size-based check at `min_fee_rate = 1000 shannons/KW`)

Admission check: `min_fee = 1000 × 200 / 1000 = 200 shannons`. The transaction passes (`fee ≥ min_fee`).

Actual weight: `max(200, 70,000,000 × 0.000170571) = max(200, 11,940) = 11,940`.

Actual fee rate: `200 × 1000 / 11,940 ≈ 16.7 shannons/KW` — approximately **60× below** the configured `min_fee_rate` of 1000 shannons/KW. [4](#0-3) 

There is no second fee-rate check after script execution returns the actual cycle count. The `TxEntry` is created with the real cycles, but the admission gate has already been passed using size alone. [5](#0-4) 

---

### Impact Explanation

An unprivileged transaction sender can craft transactions with near-maximum cycles and minimal fee (sized to just clear the size-based admission threshold). Such transactions:

1. Pass the `min_fee_rate` admission check and enter the pool.
2. Force every receiving node to execute expensive scripts (up to `max_tx_verify_cycles = 70M` cycles) before the entry is created.
3. Occupy pool space with an effective fee rate far below `min_fee_rate`, displacing legitimate transactions.
4. Are relayed to peers, propagating the resource cost across the network.

The `min_fee_rate` configuration, which operators set to protect their nodes from spam, is rendered ineffective for the class of high-cycle, small-size transactions. This is directly analogous to the JBXBuybackDelegate issue: one path (size-based admission) forces the effective protection value toward zero, while the other path (weight-based ordering) uses the correct metric — but only after the damage (pool admission, script execution, relay) is done. [6](#0-5) 

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P relay peer can submit transactions. Crafting a transaction with high cycles requires writing a script that consumes many cycles (e.g., a tight loop), which is straightforward. The attacker pays only the size-based fee (a few hundred shannons), while nodes pay the full verification cost. The attack is repeatable and requires no special access. [7](#0-6) 

---

### Recommendation

1. **Add a post-execution fee-rate check.** After `verify_rtx` returns the actual cycle count, recompute the fee rate using `get_transaction_weight(tx_size, cycles)` and reject the transaction if the weight-based fee rate falls below `min_fee_rate`. This closes the gap between the two paths.

2. **Alternatively, use a cycle-aware pre-check.** Estimate an upper-bound weight using `max_tx_verify_cycles` as a conservative cycle estimate before execution, and require the fee to cover that worst-case weight. This prevents admission of transactions that cannot possibly meet `min_fee_rate` at maximum cycles.

3. **Document the limitation explicitly.** If the intentional design is to use size-only for the cheap pre-check, the documentation and configuration description for `min_fee_rate` should clearly state that the guarantee does not hold for high-cycle transactions, so operators can set the rate accordingly. [8](#0-7) 

---

### Proof of Concept

1. Configure a node with `min_fee_rate = 1000` (default).
2. Write a lock script that consumes ~70,000,000 cycles (tight loop).
3. Construct a transaction with:
   - One input spending a cell locked by that script.
   - One output returning all capacity minus 200 shannons (fee = 200 shannons).
   - Serialized size ≈ 200 bytes.
4. Submit via `send_transaction` RPC.
5. Observe: the transaction is accepted into the pool (`fee 200 ≥ min_fee 200`).
6. Query `get_transaction` and observe the entry is in `Pending` state.
7. Compute the actual fee rate: `200 × 1000 / max(200, 70_000_000 × 0.000170571) ≈ 16.7 shannons/KW` — far below the configured 1000 shannons/KW threshold.

The `min_fee_rate` guard is bypassed. The node executed 70M cycles of script and admitted the transaction to the pool for a fee that would be rejected if the weight-based metric were applied at admission time. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/util.rs (L85-132)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
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

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-14)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** util/app-config/src/configs/tx_pool.rs (L14-16)
```rust
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
```
