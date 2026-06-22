### Title
Tx-Pool Admission Fee-Rate Check Uses `tx_size` Instead of Actual Transaction Weight, Allowing Sub-Minimum Effective Fee-Rate Transactions Into the Pool - (File: tx-pool/src/util.rs)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold by computing the minimum required fee against `tx_size` (serialized byte length) only. However, every subsequent operation in the pool — mining priority sorting, eviction, and RBF fee comparison — computes the effective fee rate using `get_transaction_weight(size, cycles) = max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`, which is always ≥ `tx_size`. The admission check therefore uses an underestimate of the true weight denominator, mirroring the external report's pattern: a threshold check is performed against a value that does not account for the adjustment applied afterward.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The code itself acknowledges the discrepancy with a comment: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [2](#0-1) 

After admission, the transaction is stored as a `TxEntry` with its actual verified `cycles`. All subsequent fee-rate computations use `get_transaction_weight`:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

`get_transaction_weight` is defined as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [4](#0-3) 

Because `weight ≥ tx_size` always holds, the admission check computes a `min_fee` that is always ≤ the `min_fee` that would be required if the true weight were used. A transaction with high cycles relative to its serialized size passes the size-based admission check but has an actual effective fee rate well below `min_fee_rate`.

The same weight function is used for mining priority sorting (`AncestorsScoreSortKey`), eviction (`EvictKey`), and the fee estimator: [5](#0-4) [6](#0-5) 

The `_process_tx` flow confirms that `check_tx_fee` runs before cycles are known (pre-check stage), and the `TxEntry` is only constructed with the verified cycles afterward: [7](#0-6) 

---

### Impact Explanation

An attacker who submits a transaction with a small serialized size but very high cycle consumption can pass the `min_fee_rate` admission gate while having an actual effective fee rate far below the node operator's configured threshold. Such transactions:

1. Occupy pool space without meeting the node's fee rate policy.
2. Are sorted to the bottom of the mining priority queue and are unlikely to be mined, wasting pool capacity.
3. Can be used to fill the pool with low-priority transactions, potentially displacing legitimate transactions that do meet the true weight-based fee rate requirement.

The node operator's `min_fee_rate` configuration is effectively bypassed for cycle-heavy transactions.

---

### Likelihood Explanation

Any RPC caller (`send_transaction`) or P2P relayer can submit transactions. Crafting a transaction with high cycles is straightforward for any script author — a script that performs many iterations of computation will produce high cycles with a small serialized transaction body. The attacker only needs to pay `min_fee_rate * tx_size` shannons rather than `min_fee_rate * max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` shannons, which can be orders of magnitude less for cycle-dominant transactions.

---

### Recommendation

Replace the size-only weight estimate in `check_tx_fee` with the actual transaction weight. Since cycles are not yet known at the pre-check stage (they are determined by script execution), the check should either:

1. Use a declared-cycles value (already passed in for relayed transactions) to compute the weight at admission time, or
2. Re-validate the fee rate after script execution completes and the actual cycles are known, before the entry is committed to the pool.

The second approach is more robust: after `verify_rtx` returns `verified.cycles` in `_process_tx`, perform a second fee-rate check using `get_transaction_weight(tx_size, verified.cycles)` before calling `submit_entry`.

---

### Proof of Concept

1. Attacker crafts a transaction with serialized size = 1,000 bytes and a script that consumes 10,000,000 cycles.
2. `check_tx_fee` computes: `min_fee = 1000 shannons/KW * 1000 bytes / 1000 = 1000 shannons`. Attacker pays 1001 shannons. Admission check passes.
3. After script execution, `verified.cycles = 10_000_000`. Actual weight = `max(1000, 10_000_000 * 0.000_170_571_4) ≈ max(1000, 1705) = 1705`.
4. Actual effective fee rate = `1001 * 1000 / 1705 ≈ 587 shannons/KW` — well below the `min_fee_rate` of 1000 shannons/KW.
5. The transaction is admitted to the pool, occupies space, and is sorted near the bottom of the mining queue, violating the node's fee rate policy.

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

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
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

**File:** tx-pool/src/process.rs (L715-751)
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
```
