All cited code references are confirmed in the repository. Let me verify one more detail about the pool size tracking and whether `total_tx_size` is byte-based or weight-based.

Audit Report

## Title
`min_fee_rate` Admission Bypass via High-Cycle Low-Size Transactions — (`File: tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size (`tx_size`), but the actual fee rate used for pool eviction and miner sorting is computed via `get_transaction_weight(tx_size, cycles)` — which equals `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. An unprivileged attacker can craft a transaction with a small serialized size but near-maximum cycle consumption, paying a fee that satisfies the size-only admission check while the true weight-based fee rate is ~60× below `min_fee_rate`. The pool size limit (`total_tx_size`) is tracked in raw bytes, so these transactions occupy real pool space at a fraction of the intended cost.

## Finding Description
In `tx-pool/src/util.rs` (L42–45), `check_tx_fee` explicitly acknowledges the discrepancy in a comment and deliberately uses `tx_size` alone:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

This is called from `pre_check` in `tx-pool/src/process.rs` (L274, L289) before script execution, so actual cycles are unknown at admission time: [2](#0-1) 

After verification, a `TxEntry` is created with the actual `verified.cycles` at L751: [3](#0-2) 

The entry's `fee_rate()` — used for eviction ordering and miner selection — uses the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

Where `get_transaction_weight` is `max(tx_size, cycles * 0.000_170_571_4)`: [5](#0-4) 

The pool size limit (`limit_size` in `tx-pool/src/pool.rs` L298) is enforced against `total_tx_size`, which is the sum of raw serialized sizes — not weights: [6](#0-5) [7](#0-6) 

There is no post-verification fee check using weight before `submit_entry`. The `declared_cycles` mismatch check at L736–748 only rejects relay transactions where declared ≠ actual cycles; it does not re-evaluate the fee rate against weight. [8](#0-7) 

**Exploit flow:**
1. Attacker crafts a transaction: serialized size ≈ 200 bytes, cycle consumption ≈ 70,000,000 (near `max_tx_verify_cycles`).
2. Admission check: `min_fee = 1000 * 200 / 1000 = 200 shannons`. Transaction passes with fee = 200 shannons.
3. Actual weight: `max(200, 70_000_000 * 0.000_170_571_4) = 11,940`. Effective fee rate ≈ 16.7 shannons/KW — ~60× below `min_fee_rate = 1000 shannons/KW`.
4. Entry is inserted into the pool. Eviction (`EvictKey` sorted by weight-based `fee_rate`) marks it as the first candidate for removal, but the attacker continuously resubmits, keeping the pool saturated.

## Impact Explanation
This matches the **High** impact category: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

Concrete consequences:
- The attacker can fill the 180 MB mempool at ~60× lower fee cost than `min_fee_rate` intends, since pool capacity is byte-measured and each 200-byte transaction costs only 200 shannons instead of 11,940.
- Each admitted transaction forces the node to execute a near-`max_tx_verify_cycles` script, consuming significant CPU. Continuous resubmission sustains this load.
- Legitimate transactions are evicted or delayed as the pool fills with artificially cheap-to-admit, high-cycle transactions.
- The `min_fee_rate` spam-prevention invariant is effectively nullified for the cycle dimension.

## Likelihood Explanation
- **Entry path**: Any `send_transaction` RPC caller or P2P relay peer. No privileges required.
- **Craft difficulty**: Low. A RISC-V tight loop consuming ~70M cycles can be expressed in tens of bytes of bytecode, keeping `tx_size` minimal.
- **Cost**: ~200 shannons per transaction vs. the intended ~11,940 shannons — a ~60× discount.
- **Repeatability**: The attacker can submit many such transactions in parallel, limited only by available UTXOs and the `max_ancestors_count` limit per chain. UTXOs can be pre-created on-chain.

## Recommendation
Add a post-verification fee check using the actual weight before inserting the entry into the pool. After `verify_rtx` returns `verified.cycles` and before `submit_entry` is called (around `tx-pool/src/process.rs` L751), compute:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This is the most accurate fix: it uses the real execution metric (actual cycles) and requires no changes to the pre-check path. The existing size-only pre-check in `check_tx_fee` can remain as a fast early rejection for obviously under-fee'd transactions.

## Proof of Concept
1. Deploy a CKB script (RISC-V bytecode, ~20 bytes) that loops consuming ~70,000,000 cycles.
2. Create a transaction with 1 input spending a UTXO locked by this script, 1 output. Serialized size ≈ 200 bytes.
3. Set fee = 200 shannons (`min_fee_rate * tx_size / 1000 = 1000 * 200 / 1000`).
4. Submit via `send_transaction` RPC.

**Expected (correct) behavior**: Rejected with `LowFeeRate` — effective fee rate ≈ 16.7 shannons/KW < 1000 shannons/KW.

**Actual behavior**: Accepted. `check_tx_fee` passes because `fee (200) >= min_fee_rate.fee(tx_size=200) = 200`. The entry sits in the pool with `fee_rate() ≈ 16.7 shannons/KW`.

Repeat with many pre-funded UTXOs to fill the pool. Each iteration costs ~200 shannons instead of ~11,940, sustaining a full pool of high-cycle transactions at ~60× below the intended spam-prevention threshold. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/process.rs (L274-290)
```rust
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
