I have all the information needed. Let me trace the complete flow precisely.

### Title
`check_tx_fee` Uses Raw Serialized Size Instead of `get_transaction_weight` for Admission Gate, Allowing High-Cycle Transactions to Enter Pool Below Minimum Fee Rate — (`tx-pool/src/util.rs`)

---

### Summary

The sole fee-rate admission gate, `check_tx_fee`, computes the minimum required fee using only the raw serialized byte size of the transaction. The actual pool weight metric, `get_transaction_weight(size, cycles)`, takes the maximum of serialized size and `cycles × DEFAULT_BYTES_PER_CYCLES`. For any transaction whose cycle count dominates, the admission check is satisfied with a fee that is orders of magnitude below what the weight-based minimum would require. No second weight-based check exists anywhere in the admission path. The code comment at the check site explicitly acknowledges this as a "cheap check" but no compensating check follows.

---

### Finding Description

**Admission gate — `tx-pool/src/util.rs`, `check_tx_fee`:** [1](#0-0) 

The comment reads: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* The minimum fee is computed as `min_fee_rate.fee(tx_size)` — purely size-based.

**Weight function — `util/types/src/core/tx_pool.rs`, `get_transaction_weight`:** [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a tx with 300 bytes and 70,000,000 cycles: `weight = max(300, 70_000_000 × 0.000_170_571_4) = max(300, 11_940) = 11_940`. The proper minimum fee at `min_fee_rate=1000` is **11,940 shannons**. `check_tx_fee` only requires **300 shannons** — a 39.8× undercharge.

**Full admission path — `tx-pool/src/process.rs`, `_process_tx`:** [3](#0-2) 

The sequence is:
1. `pre_check` → calls `check_tx_fee` (size-only gate, read lock)
2. `verify_rtx` → measures actual cycles
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles and the fee that passed the size-only gate
4. `submit_entry` → `_submit_entry` → `tx_pool.add_pending(entry)` — **no second weight-based fee check**

`TxEntry::fee_rate()` does use `get_transaction_weight`: [4](#0-3) 

But this method is only used for **sorting and eviction priority**, not for admission gating.

---

### Impact Explanation

Any unprivileged submitter can craft a transaction with a small serialized size but cycles near `max_tx_verify_cycles` (70,000,000). Such a transaction:

1. **Passes `check_tx_fee`** with a fee proportional only to its byte size (e.g., 300 shannons for a 300-byte tx).
2. **Enters the pool** with an actual weight-based fee rate of ~8 shannons/KB — 125× below the 1,000 shannons/KB minimum.
3. **Remains in the pool** until evicted by `limit_size` when the pool is full. If the pool is not full, it persists indefinitely.
4. **Can be mined**: miners select by weight-based fee rate, so these txs are deprioritized but not excluded. In a low-traffic period or on a miner that does not enforce a weight-based floor, they will be included in blocks.
5. **Forces expensive verification**: each such tx causes the node to execute up to 70M VM cycles, at a cost to the attacker of only ~300 shannons instead of ~11,940 shannons.

The fee economy invariant — *every admitted tx satisfies `fee ≥ min_fee_rate × get_transaction_weight(size, cycles) / 1000`* — is violated for all high-cycle, small-size transactions.

---

### Likelihood Explanation

The path is fully reachable via the `send_transaction` RPC with no privileges required. The attacker needs only valid UTXOs and a script that consumes many cycles (e.g., a loop-heavy lock script). The discrepancy is structural and present in every node running the default configuration. The code comment at the check site confirms the developers are aware of the theoretical gap but left no compensating check.

---

### Recommendation

Replace the size-only check in `check_tx_fee` with a weight-based check:

```rust
// In tx-pool/src/util.rs, check_tx_fee
// Replace:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// With a weight-based check using declared_cycles (available from the verify queue entry)
// or, conservatively, use max_tx_verify_cycles as an upper bound at pre-check time,
// then re-check with actual cycles after verify_rtx completes.
```

The cleanest fix is a **two-stage check**:
- Pre-check (read lock): use size only as a fast lower-bound filter (keep as-is).
- Post-verify (before `submit_entry`): re-check `fee >= min_fee_rate.fee(get_transaction_weight(tx_size, verified.cycles))` and reject if it fails.

This second check can be inserted in `_process_tx` between lines 751 and 753 of `tx-pool/src/process.rs`, after `verified.cycles` is known. [5](#0-4) 

---

### Proof of Concept

```
1. Construct a CKB transaction T:
   - 1 input consuming a live cell (attacker-owned UTXO)
   - 1 output returning capacity minus fee
   - Lock script: a loop that runs for ~70,000,000 cycles
   - Serialized size: ~300 bytes
   - Fee: 300 shannons (satisfies check_tx_fee: 1000 * 300 / 1000 = 300)

2. Submit via send_transaction RPC.

3. Observe:
   - check_tx_fee passes (fee=300 >= min_fee=300)
   - verify_rtx measures cycles ≈ 70,000,000
   - TxEntry created with fee=300, cycles=70_000_000, size=300
   - weight = max(300, 11940) = 11940
   - entry.fee_rate() = FeeRate::calculate(300, 11940) ≈ 25 shannons/KW
   - tx is admitted to pool despite fee_rate << min_fee_rate (1000)

4. Assert: tx appears in get_raw_tx_pool pending set.
5. Assert: tx appears in get_block_template when no competing txs exist.
6. Assert: node spent ~70M cycles verifying a tx that paid 300 shannons instead of 11,940.
```

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
