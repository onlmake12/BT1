### Title
`check_tx_fee` Uses Serialized Size Instead of Full Weight for Min-Fee-Rate Enforcement, Allowing Cycle-Heavy Transactions to Bypass the Fee Rate Floor — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size as the weight denominator. The actual transaction weight in CKB is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the actual weight can be orders of magnitude larger than `tx_size`. Because cycles are not yet known at pre-check time and no subsequent fee-rate re-check is performed after script verification, a transaction with high cycles but small serialized size can be admitted to the tx-pool with an actual fee rate far below `min_fee_rate`.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate as follows:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The actual weight formula used everywhere else in the codebase is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a transaction consuming 70 million cycles (the maximum), the cycle-derived weight is `70_000_000 × 0.000170571 ≈ 11,940` bytes, while a minimal transaction may serialize to only ~200 bytes.

The process flow in `_process_tx` is:

1. `pre_check` → `check_tx_fee(tx_pool, snapshot, &rtx, tx_size)` — cycles unknown, uses `tx_size` as weight
2. `verify_rtx(...)` — script execution runs; `verified.cycles` is now known
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with actual cycles, but `fee` from step 1 is used unchanged
4. `submit_entry(...)` — entry admitted to pool with no subsequent fee-rate re-check [3](#0-2) 

There is no post-verification step that re-evaluates `fee / get_transaction_weight(tx_size, verified.cycles)` against `min_fee_rate`. The "cheap check" is the only check.

---

### Impact Explanation

An attacker can craft a transaction with:
- Minimal serialized size (e.g., ~200 bytes)
- Maximum cycles (e.g., 70M cycles)
- Fee just above `min_fee_rate × tx_size / 1000` (e.g., 200 shannons at `min_fee_rate = 1000 shannons/KW`)

Such a transaction passes `check_tx_fee` (200 shannons > 200 shannons threshold), but its actual fee rate is:

```
200 shannons / 11,940 weight × 1000 ≈ 16.7 shannons/KW
```

This is ~98% below the configured `min_fee_rate` of 1000 shannons/KW. The transaction is admitted to the pool and relayed to peers (who apply the same size-only check). This allows an attacker to:

- Flood the tx-pool with transactions paying only ~1.7% of the expected fee
- Displace legitimate higher-fee-rate transactions from the pool
- Propagate sub-threshold transactions across the P2P network

The `min_fee_rate` policy is the primary admission gate for the tx-pool; bypassing it undermines DoS protection for all nodes.

---

### Likelihood Explanation

The attack requires only an unprivileged RPC caller or P2P relay peer. The attacker must deploy a script that consumes many cycles (e.g., a tight loop in CKB-VM) but has a small serialized footprint. This is straightforward for anyone familiar with CKB script development. No privileged access, key material, or majority hashpower is required. The attack is repeatable and cheap per transaction.

---

### Recommendation

After `verify_rtx` returns `verified.cycles`, re-check the fee rate using the actual weight before admitting the entry:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, in `check_tx_fee`, use a conservative upper bound by substituting `max_tx_verify_cycles` for cycles when the actual cycles are not yet known, so the pre-check is never more permissive than the post-check would be.

---

### Proof of Concept

**Setup:** Node configured with `min_fee_rate = 1000 shannons/KW`.

**Transaction parameters:**
- Serialized size: 200 bytes → `min_fee = 1000 × 200 / 1000 = 200 shannons`
- Script: tight CKB-VM loop consuming 70,000,000 cycles
- Fee paid: 201 shannons (just above the size-only threshold)

**Step 1 — `check_tx_fee` (pre-check, cycles unknown):**
```
min_fee = min_fee_rate.fee(tx_size) = 1000 × 200 / 1000 = 200 shannons
fee (201) >= min_fee (200) → PASS
``` [4](#0-3) 

**Step 2 — `verify_rtx` (script runs, cycles = 70,000,000 now known):**
No fee-rate re-check is performed. [5](#0-4) 

**Step 3 — `TxEntry::new` and `submit_entry`:**
Entry admitted with `fee = 201 shannons`, `cycles = 70_000_000`.

**Actual fee rate:**
```
weight = max(200, 70_000_000 × 0.000170571) = 11,940
actual_fee_rate = 201 × 1000 / 11,940 ≈ 16.8 shannons/KW
``` [2](#0-1) 

This is 98.3% below the configured `min_fee_rate` of 1000 shannons/KW. The transaction is admitted and relayed. Repeating this with many transactions floods the pool at a fraction of the intended cost.

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
