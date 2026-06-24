All code references check out. Let me confirm the complete exploit path before writing the report.

The code confirms:
1. `check_tx_fee` explicitly uses size-only as a "cheap check" before cycles are known [1](#0-0) 
2. After `verify_rtx` returns actual cycles, there is no second weight-based fee check — the entry goes directly to `submit_entry` [2](#0-1) 
3. `fee_rate()` on `TxEntry` correctly uses full weight, meaning admitted entries can have actual fee rates far below `min_fee_rate` [3](#0-2) 
4. `calculate_min_replace_fee` also uses size-only for the RBF extra fee [4](#0-3) 
5. `get_transaction_weight` is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)` [5](#0-4) 
6. Eviction (`next_evict_entry`) uses the full weight-based `EvictKey`, but eviction only happens *after* verification has already consumed the cycles [6](#0-5) 

---

Audit Report

## Title
Tx-Pool Minimum Fee Check Uses Serialized Size Instead of Full Weight, Allowing High-Cycle Transactions to Bypass Fee Rate Enforcement — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while the canonical resource-cost metric `get_transaction_weight` is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are unknown at pre-check time, the code explicitly treats this as a "cheap check," but no second weight-based fee check is performed after `verify_rtx` returns the actual cycles. An unprivileged submitter can craft a transaction with a tiny serialized size but near-maximum cycles, pay only the size-proportional minimum fee, and force the node to execute up to 70 M cycles of script verification per transaction at negligible cost.

## Finding Description
The admission flow in `_process_tx` (`tx-pool/src/process.rs`) is:

1. `pre_check` → calls `check_tx_fee` with `tx_size` only:
   ```rust
   // tx-pool/src/util.rs L42-45
   // Theoretically we cannot use size as weight directly to calculate fee_rate,
   // here min fee rate is used as a cheap check,
   // so we will use size to calculate fee_rate directly
   let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
   ```
2. `verify_rtx` executes scripts and returns `verified.cycles`.
3. No second fee check is performed. The entry is created and submitted directly:
   ```rust
   // tx-pool/src/process.rs L751-753
   let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
   let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
   ```

The `fee_rate()` method on `TxEntry` correctly uses the full weight:
```rust
// tx-pool/src/component/entry.rs L115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```
where `get_transaction_weight` is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. This means admitted entries can have actual fee rates far below `min_fee_rate` with no enforcement gate.

The same pattern appears in `calculate_min_replace_fee` (`tx-pool/src/pool.rs` L103), which computes the RBF extra fee using only `size`:
```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

The eviction mechanism (`limit_size` → `next_evict_entry`) does use the full weight-based `EvictKey`, so the attacker's entries are evicted first when the pool fills. However, eviction occurs only *after* `verify_rtx` has already consumed the cycles — the verification cost is irrecoverable.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With default parameters (`min_fee_rate = 1000 shannons/KB`, `max_tx_verify_cycles = 70,000,000`, `DEFAULT_BYTES_PER_CYCLES ≈ 0.000170571`):

- A 200-byte transaction consuming 70 M cycles has a true weight of `max(200, 11,940) = 11,940`.
- Size-based min fee: 200 shannons. Weight-based min fee: 11,940 shannons.
- The attacker pays **~60× less** than the weight-based threshold requires.

Each submitted transaction forces the node to execute 70 M cycles of script verification. Repeated submissions exhaust node CPU at negligible fee cost, degrading verification throughput for legitimate transactions and causing effective network congestion. Additionally, entries admitted with sub-threshold actual fee rates degrade block-template quality and displace legitimate transactions during pool eviction.

## Likelihood Explanation
Any unprivileged user with RPC access to `send_transaction` can trigger this. Crafting a small transaction whose lock/type script performs near-maximum computation (e.g., a tight loop in CKB-VM stored in a dep cell) is straightforward. No special privilege, key, or majority hashpower is required. The default `max_tx_verify_cycles = 70,000,000` provides a large amplification factor, and the attack is repeatable as long as the attacker has valid UTXOs to spend.

## Recommendation
After `verify_rtx` returns the actual `verified.cycles`, perform a second fee check using the full weight before calling `submit_entry`:

```rust
// tx-pool/src/process.rs, after line 734
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

Apply the same fix to `calculate_min_replace_fee` in `tx-pool/src/pool.rs`: replace `self.config.min_rbf_rate.fee(size as u64)` with `self.config.min_rbf_rate.fee(get_transaction_weight(size, entry_cycles))`, passing the new entry's actual cycles.

## Proof of Concept
1. Deploy a lock script that runs a tight CKB-VM loop consuming ~70 M cycles; store the script bytecode in a dep cell so the transaction itself remains small (~200 bytes serialized).
2. Construct a transaction spending a cell locked by that script. Set output capacity so that `fee = 200 shannons` (just above `min_fee_rate × size = 200`).
3. Submit via `send_transaction` RPC. `check_tx_fee` passes: `200 ≥ 200`.
4. The node executes 70 M cycles of script verification via `verify_rtx`.
5. The admitted `TxEntry` has `fee_rate() = FeeRate::calculate(200, 11940) ≈ 16 shannons/KW`, far below the configured `min_fee_rate = 1000 shannons/KW`.
6. Repeat with many such transactions (each requiring a fresh UTXO) to continuously force expensive verification at negligible fee cost, exhausting node CPU and degrading pool quality.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/pool.rs (L102-103)
```rust
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
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

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```
