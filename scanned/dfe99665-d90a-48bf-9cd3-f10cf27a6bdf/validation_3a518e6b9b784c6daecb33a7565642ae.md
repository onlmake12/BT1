Now I have a complete picture of the vulnerability. Let me compile the full finding.

### Title
Fee Admission Check Uses Serialized Size Instead of Weight, Allowing High-Cycle Transactions to Bypass the Effective Minimum Fee Rate — (File: `tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission gate in `check_tx_fee` enforces `min_fee_rate` against raw serialized transaction size, but the node's actual resource cost for a transaction is measured by its **weight** = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are unknown at fee-check time, no weight-based fee check is ever performed after cycles become known. An unprivileged submitter can craft a transaction with near-maximum cycle consumption but minimal serialized size, pass the fee check by paying only the size-based minimum fee (which can be ~60× below the weight-based minimum), and force the node to execute up to `max_tx_verify_cycles` (70 million) CKB-VM cycles per transaction at a fraction of the intended cost.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual weight formula is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

The admission flow in `_process_tx` is:

1. `pre_check` → calls `check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)` — cycles are **not yet known**, so only size is used.
2. `verify_rtx` — CKB-VM executes the script; actual cycles are now known.
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry is created with real cycles.
4. `submit_entry` — no second weight-based fee check is performed. [4](#0-3) 

After `verify_rtx` returns `verified.cycles`, the code creates the entry and submits it directly with no re-check of fee rate against the now-known weight. [5](#0-4) 

The `check_tx_fee` call in `pre_check` is the **only** fee rate gate: [6](#0-5) 

---

### Impact Explanation

With `max_tx_verify_cycles = 70,000,000` (default) and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`:

- Maximum weight from cycles alone: `70,000,000 × 0.000_170_571_4 ≈ 11,940` weight-bytes.
- A minimal transaction serialized size: ~200 bytes.
- Weight-based minimum fee at `min_fee_rate = 1,000 shannons/KB`: `1,000 × 11,940 / 1,000 = 11,940 shannons`.
- Size-based minimum fee (what is actually checked): `1,000 × 200 / 1,000 = 200 shannons`.

The attacker pays **~200 shannons** but forces the node to execute **70 million CKB-VM cycles** — approximately a **60× underpayment** relative to the intended weight-based cost. By submitting many such transactions, the attacker saturates the `max_tx_verify_workers` thread pool with expensive script executions at negligible fee cost, degrading or stalling transaction processing for legitimate users. [7](#0-6) 

---

### Likelihood Explanation

The attack requires only:
- A valid UTXO to spend (any live cell the attacker controls).
- A lock script that consumes close to `max_tx_verify_cycles` cycles (trivially constructable with a loop in CKB-VM).
- Submission via the public `send_transaction` RPC or P2P relay.

No privileged access, no key leakage, no majority hashpower. The attacker needs only enough CKB to cover the size-based minimum fee (~200 shannons per transaction), making this economically cheap to sustain.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee rate check against the true weight:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors how `TxEntry::fee_rate()` already computes the effective fee rate using weight: [8](#0-7) 

The existing size-based check in `check_tx_fee` can remain as the cheap pre-verification gate; the weight-based check should be added as a post-verification gate in `_process_tx` before `submit_entry`.

---

### Proof of Concept

1. Deploy a lock script that executes a tight loop consuming ~69,000,000 CKB-VM cycles (just under `max_tx_verify_cycles = 70,000,000`).
2. Create a transaction spending a cell locked by this script. The transaction serialized size is ~200 bytes. Set output capacity = input capacity − 200 shannons (satisfying the size-based fee check: `1,000 × 200 / 1,000 = 200 shannons`).
3. Submit via `send_transaction` RPC.
4. `pre_check` calls `check_tx_fee` with `tx_size = 200`: `min_fee = 200 shannons`, `fee = 200 shannons` → **passes**.
5. `verify_rtx` executes the lock script for ~69,000,000 cycles.
6. No weight-based re-check occurs. The entry is admitted with `fee_rate() = FeeRate::calculate(200 shannons, 11,769 weight) ≈ 17 shannons/KW` — far below `min_fee_rate = 1,000 shannons/KW`.
7. Repeat with many UTXOs. Each submission consumes a full verification worker for the duration of the script execution, at a cost of only 200 shannons per transaction. [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/util.rs (L42-53)
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
    Ok(fee)
```

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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
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

**File:** util/app-config/src/legacy/tx_pool.rs (L9-16)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
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
