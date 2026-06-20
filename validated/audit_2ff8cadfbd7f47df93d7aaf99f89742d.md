Now I have enough context to write the finding. Let me confirm the exact line numbers for the critical path.### Title
Tx-Pool Admission Fee Check Ignores Cycles, Allowing Below-Minimum-Fee-Rate Transactions to Enter the Pool - (File: tx-pool/src/util.rs)

---

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size, ignoring VM execution cycles. Because the actual fee rate is computed using `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`, a transaction with a small serialized size but high cycle consumption passes the admission gate while its true fee rate is far below `min_fee_rate`. No second fee-rate check is performed after the actual cycles are known. Any unprivileged RPC caller or P2P relay peer can exploit this to inject below-minimum-fee-rate transactions into the pool and propagate them across the network.

---

### Finding Description

**Root cause — `check_tx_fee` uses size-only weight:**

```rust
// tx-pool/src/util.rs, lines 42-52
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The actual fee rate of a pool entry is computed using `get_transaction_weight`:

```rust
// util/types/src/core/tx_pool.rs, lines 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`, meaning cycles dominate weight whenever `cycles > tx_size / 0.000_170_571_4`. [3](#0-2) 

**Missing post-verification fee-rate check:**

The processing pipeline in `_process_tx` calls `pre_check` (which calls `check_tx_fee` with size-only), then calls `verify_rtx` to obtain the actual cycles, then immediately constructs the `TxEntry` and submits it — with no re-check of the fee rate against the now-known actual weight:

```rust
// tx-pool/src/process.rs, lines 715-753
let (ret, snapshot) = self.pre_check(&tx).await;                    // size-only fee check here
let (tip_hash, rtx, status, fee, tx_size) = ...;
let verified_ret = verify_rtx(...).await;                            // actual cycles learned here
let verified = ...;
// ← no fee-rate re-check using verified.cycles
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);       // entry carries real cycles
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [4](#0-3) 

`pre_check` is the sole admission gate: [5](#0-4) 

The `TxEntry::fee_rate()` method — used for pool sorting and eviction — correctly uses `get_transaction_weight(size, cycles)`: [6](#0-5) 

So the admission check and the actual fee-rate metric are computed on different bases, creating a gap an attacker can exploit.

---

### Impact Explanation

An attacker crafts a transaction with:
- **Small serialized size** (e.g., ~200 bytes) — minimizing the size-based admission fee.
- **High cycle consumption** (up to `max_tx_verify_cycles`, default `TWO_IN_TWO_OUT_CYCLES × 20 ≈ 70 M cycles`) — by embedding a looping script.

Numerical example with `min_fee_rate = 1000 shannons/KW`, `tx_size = 200 bytes`, `cycles = 70,000,000`:
- Size-based min fee = `1000 × 200 / 1000 = 200 shannons` → **passes admission**.
- Actual weight = `max(200, 70,000,000 × 0.000_170_571_4) ≈ 11,940`.
- Actual fee rate = `200 / 11,940 × 1000 ≈ 16.7 shannons/KW` — **~60× below `min_fee_rate`**.

Consequences:
1. Below-minimum-fee-rate transactions are admitted to the pool and relayed to peers (who apply the same buggy check), propagating network-wide.
2. Pool space is consumed by transactions that should have been rejected; legitimate transactions may be evicted sooner.
3. The attacker pays ~60× less than the protocol-mandated minimum, making sustained pool flooding cheap.
4. Miners' fee-rate-ordered block assembly is polluted with artificially cheap high-cycle transactions.

---

### Likelihood Explanation

The entry path is fully unprivileged: any user can call `send_transaction` via JSON-RPC or relay a transaction via P2P. No special role, key, or majority hashpower is required. The attacker only needs to hold enough CKB to pay the (artificially low) size-based fee and to own a live cell to spend. The attack is repeatable by chaining outputs. The code comment explicitly acknowledges the approximation ("Theoretically we cannot use size as weight directly"), confirming the gap is known but unguarded. [7](#0-6) 

---

### Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee-rate check using the true weight before constructing and submitting the `TxEntry`. In `_process_tx` (or at the start of `submit_entry`), add:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_fee_rate = FeeRate::calculate(fee, actual_weight);
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        tx_pool_config.min_fee_rate.fee(actual_weight).as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

The existing size-only pre-check in `check_tx_fee` can remain as a cheap early filter, but it must not be the final admission gate.

---

### Proof of Concept

1. Attacker deploys a lock script that executes a tight loop consuming ~70 M cycles (within `max_tx_verify_cycles`). The script binary is small (a few hundred bytes).
2. Attacker creates a transaction spending one of their cells, with serialized size ~200 bytes, and sets the fee to exactly `min_fee_rate × 200 / 1000 = 200 shannons` (at default 1000 shannons/KW).
3. Attacker submits via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; fee equals min_fee → **admitted**.
5. `verify_rtx` executes the script, returns `verified.cycles ≈ 70,000,000`.
6. `TxEntry::new(rtx, 70_000_000, 200_shannons, 200)` is created; `fee_rate() = FeeRate::calculate(200, 11940) ≈ 16 shannons/KW` — far below `min_fee_rate`.
7. Transaction is submitted to the pool and relayed to all peers via the relay protocol.
8. Attacker repeats with a chain of outputs, flooding the mempool across the network at ~60× discount. [8](#0-7) [9](#0-8) [6](#0-5)

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

**File:** tx-pool/src/process.rs (L269-314)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
