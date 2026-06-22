### Title
Minimum Fee Check Uses Raw Byte Size Instead of Transaction Weight, Allowing Below-Minimum Fee Rate Transactions — (File: `tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold using only the raw serialized byte size of a transaction (`tx_size`), not the actual transaction weight (`max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`). Because the weight can be dominated by cycle consumption, a transaction sender can craft a high-cycles, small-size transaction that passes the fee gate while its true effective fee rate is far below the configured minimum. No second fee check is performed after cycles are determined. The admitted transaction is relayed to peers and eligible for mining, reducing miner incentive per unit of block resource consumed.

---

### Finding Description

CKB's transaction weight is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [1](#0-0) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (the ratio `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`). [2](#0-1) 

The pool's fee rate for ordering and eviction is always computed from this weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

However, the admission gate `check_tx_fee` computes the minimum fee using **only `tx_size`**, not weight:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [4](#0-3) 

In `_process_tx`, `check_tx_fee` is called inside `pre_check` **before** `verify_rtx` determines actual cycles. After verification, the entry is created with the real cycles and the already-approved fee — no second fee check is performed:

```rust
let (ret, snapshot) = self.pre_check(&tx).await;          // fee checked here (size only)
// ...
let verified_ret = verify_rtx(...).await;                  // cycles determined here
// ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size); // no re-check
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [5](#0-4) 

---

### Impact Explanation

**Concrete example** with default configuration (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 |
| `weight` | `max(200, 70_000_000 × 0.000_170_571_4)` = **11,940** |
| Fee check threshold | `1000 × 200 / 1000` = **200 shannons** |
| Actual effective fee rate | `200 × 1000 / 11,940` ≈ **16 shannons/KW** |
| Configured minimum | **1000 shannons/KW** |

The attacker pays 200 shannons — 60× below the minimum — and the transaction is admitted, relayed, and eligible for mining. Miners receive far less fee per unit of block resource (cycles + bytes) consumed, undermining the economic incentive that `min_fee_rate` is designed to protect.

---

### Likelihood Explanation

The attack is trivially reachable by any RPC caller (`send_transaction`) or any P2P peer relaying a transaction. No special privilege is required. The attacker only needs to:
1. Construct a transaction with a script that consumes near-maximum cycles (e.g., a loop-heavy lock script using the always-available VM).
2. Keep the serialized transaction body small (minimal inputs/outputs/witnesses).
3. Set the fee just above `min_fee_rate.fee(tx_size)` — a value the attacker can compute exactly.

The code comment itself acknowledges the discrepancy ("Theoretically we cannot use size as weight directly"), confirming the gap is known but unguarded. [6](#0-5) 

---

### Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee check using the true weight before creating the `TxEntry`:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// Re-check fee rate with actual weight now that cycles are known
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

The cheap size-only pre-check in `check_tx_fee` can remain as an early rejection for obviously under-fee transactions, but it must not be the sole gate.

---

### Proof of Concept

1. Deploy a lock script that runs a tight loop consuming ~70,000,000 cycles but whose witness/script data is minimal (e.g., a pre-compiled binary of ~100 bytes stored in a cell dep).
2. Construct a transaction spending a cell locked by that script:
   - `tx_size` ≈ 200 bytes (minimal inputs/outputs/witnesses)
   - `cycles` ≈ 70,000,000
   - `fee` = `min_fee_rate.fee(200)` = 200 shannons (just above the size-based threshold)
3. Submit via `send_transaction` RPC or relay via P2P.
4. Observe: the transaction is accepted into the pool (`check_tx_fee` passes because `200 >= 200`).
5. Compute actual effective fee rate: `FeeRate::calculate(200, get_transaction_weight(200, 70_000_000))` ≈ 16 shannons/KW — 62× below `min_fee_rate`.
6. The transaction is relayed to all peers and included in block templates, with miners receiving only 200 shannons for consuming 11,940 weight units of block space.

### Citations

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
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

**File:** tx-pool/src/process.rs (L715-753)
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
```
