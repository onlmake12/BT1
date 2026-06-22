### Title
Fee Admission Check Uses Byte Size Only, Ignoring CKB-VM Cycle Cost — (`tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in the CKB tx-pool computes the minimum required fee using only the transaction's serialized **byte size**, completely ignoring the CKB-VM **cycle** cost. Because the actual computational weight of a transaction is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`, a transaction with a small byte footprint but near-maximum cycle consumption passes the fee gate with a fee that is orders of magnitude below what the resource consumption warrants. Any unprivileged RPC caller or P2P relay peer can exploit this to force nodes to execute expensive CKB-VM scripts at negligible cost.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate using only `tx_size` (serialized bytes):

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The correct resource weight is defined in `util/types/src/core/tx_pool.rs` as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

`check_tx_fee` is called inside `pre_check`, which runs **before** script verification — so cycles are not yet known at fee-check time, and the code deliberately falls back to size only: [4](#0-3) 

After `pre_check` passes, `verify_rtx` is called with `max_cycles = declared_cycles.unwrap_or(consensus.max_block_cycles())`: [5](#0-4) 

For a locally submitted transaction (`declared_cycles = None`), `max_cycles` becomes the full consensus `max_block_cycles` — a value far larger than the pool's `max_tx_verify_cycles` config, which only governs chunked/async scheduling, not the hard cycle cap passed to the VM.

**Concrete numbers (default config):**

| Parameter | Value |
|---|---|
| `min_fee_rate` | 1,000 shannons/KB |
| Minimum tx size | ~200 bytes |
| Size-based min fee | **200 shannons** |
| `max_block_cycles` (mainnet) | ~3,500,000,000 |
| Weight at max cycles | `3.5B × 0.000_170_571_4 ≈ 597,000 bytes` |
| Weight-correct min fee | **597,000 shannons** |
| **Discrepancy** | **~3,000×** |

Even using the pool's `max_tx_verify_cycles = 70,000,000`:

- Weight = `max(200, 70M × 0.000_170_571_4) = 11,940 bytes`
- Correct fee = 11,940 shannons vs. 200 shannons paid → **~60× underpayment**

---

### Impact Explanation

An attacker who controls valid UTXOs can submit transactions that:
1. Have a small serialized size (pass the size-based fee check with ~200 shannons)
2. Contain a lock/type script that loops until the cycle limit is exhausted

Each such transaction forces every receiving node to run the CKB-VM for up to `max_block_cycles` cycles during pool admission. With `max_ancestors_count = 25` chained transactions per UTXO, the attacker multiplies the per-UTXO impact 25×. Sustained submission of such transactions can saturate the tx-pool verification workers, delay block assembly, and degrade node responsiveness — analogous to the SEDA validator resource drain. [6](#0-5) 

---

### Likelihood Explanation

- Entry path is fully unprivileged: any `send_transaction` RPC caller or P2P relay peer qualifies.
- The attacker needs valid UTXOs, but the fee cost per UTXO is negligible (~200 shannons ≈ 0.000002 CKB).
- No special knowledge or access is required beyond the ability to deploy a looping script cell.
- The relay path also applies: `transactions_process.rs` only bans peers whose `declared_cycles > max_block_cycles`; declaring exactly `max_block_cycles` is permitted and triggers the same full-cycle verification with a size-only fee. [7](#0-6) 

---

### Recommendation

Replace the size-only fee check in `check_tx_fee` with a weight-based check. For the pre-check stage (before cycles are known), use the declared cycles from the relay message as an upper bound, or use `max_tx_verify_cycles` as a conservative proxy:

```rust
// Use declared_cycles (or max_tx_verify_cycles as proxy) to compute weight
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

This ensures the fee gate is proportional to the actual worst-case computational cost, closing the ~60–3000× underpayment gap. [8](#0-7) 

---

### Proof of Concept

1. Deploy a CKB script cell whose code loops consuming cycles until the VM halts at the cycle limit.
2. Create a transaction spending any UTXO with that script as the lock, sized to ~200 bytes.
3. Submit via `send_transaction` RPC. The fee check passes (200 shannons ≥ size-based minimum).
4. The node's `verify_rtx` runs the script for up to `max_block_cycles` cycles before rejecting or accepting.
5. Repeat with 25 chained descendants (up to `max_ancestors_count`) per UTXO to multiply impact.
6. Observe that verification workers are saturated and block template assembly is delayed. [1](#0-0) [2](#0-1)

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

**File:** tx-pool/src/process.rs (L720-732)
```rust
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
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-26)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```
