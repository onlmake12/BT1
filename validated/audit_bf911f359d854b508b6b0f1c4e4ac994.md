Audit Report

## Title
`check_tx_fee` Admission Gate Uses `tx_size`-Only Fee Check While Pool Prioritization Uses True Computational Weight — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces `min_fee_rate` using only the transaction's serialized byte size (`tx_size`) before script execution. After verification completes and actual cycles are known, the pool stores and sorts entries by their true weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`), but no second fee-rate check is performed. An attacker can craft a small-serialized-size, high-cycle transaction that passes the size-only admission gate while paying a fee far below what the true computational weight would require, forcing the node to expend significant CPU at a fraction of the intended cost.

## Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` explicitly acknowledges the gap with a comment and uses `tx_size` only:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

This check is invoked in `pre_check` before script execution, when cycles are unknown: [2](#0-1) 

After `verify_rtx` returns actual cycles, a `TxEntry` is created with both `size` and `cycles`, and inserted into the pool with no second fee-rate check: [3](#0-2) 

The `TxEntry::fee_rate()` method — used for eviction ordering — computes the true weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

Where `get_transaction_weight` is: [5](#0-4) 

The `EvictKey` used for pool eviction ordering is also computed from the true weight: [6](#0-5) 

The `limit_size` eviction loop uses `next_evict_entry` which iterates by `evict_key` (true weight-based fee rate), confirming the pool's internal metric diverges from the admission check: [7](#0-6) 

The default `max_tx_verify_cycles` is 70,000,000 and `min_fee_rate` is 1,000 shannons/KW: [8](#0-7) 

**Exploit path:**
1. Attacker submits a transaction with serialized size ~200 bytes and a lock script that loops for ~70M cycles.
2. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`. Fee of 201 shannons passes.
3. `verify_rtx` executes the script, consuming 70M cycles (significant CPU).
4. `TxEntry` is created with `cycles = 70_000_000`. True weight = `max(200, 70_000_000 × 0.000170571) = 11,940`.
5. Effective fee rate = `201 / 11940 × 1000 ≈ 16.8 shannons/KW` — ~60× below `min_fee_rate`.
6. The transaction is admitted to the pool. The node expended 70M cycles of CPU for a 201-shannon fee.

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker controlling a set of UTXOs can repeatedly submit high-cycle, small-size transactions, each forcing the node to execute up to `max_tx_verify_cycles` (70M) cycles of CKB-VM computation while paying only a fee proportional to the tiny serialized byte size. The computational amplification factor is approximately 60× at default settings. Sustained submission across multiple nodes degrades node CPU availability, slows block assembly, and can cause network-wide processing delays. The attack is repeatable as long as the attacker has UTXOs, and each individual transaction is cheap to construct.

## Likelihood Explanation

The `send_transaction` RPC endpoint is accessible to any connected client (default: `127.0.0.1:8114`, but commonly exposed to trusted networks). Constructing a CKB transaction with a tight-loop lock script consuming near-`max_tx_verify_cycles` cycles is straightforward using standard CKB-VM tooling. No privileged access, leaked keys, or victim mistakes are required. The attacker only needs valid UTXOs and a small fee budget. The attack is low-cost and repeatable.

## Recommendation

After `verify_rtx` returns actual cycles, perform a second fee-rate check using the true weight before inserting the entry into the pool, in `_process_tx` after line 734:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors the pattern used in `TxEntry::fee_rate()` and closes the gap between the admission check and the actual fee-rate metric used for pool management.

## Proof of Concept

1. Construct a CKB transaction with a lock script that executes a tight loop consuming ~70,000,000 cycles. Keep the serialized transaction size small (~200 bytes) by referencing the script via `code_hash` to an existing cell dep.
2. Set the transaction fee to 201 shannons (just above `min_fee_rate.fee(200) = 200` at 1000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. Observe: `check_tx_fee` passes (201 > 200). `verify_rtx` executes 70M cycles. `TxEntry` is created with effective fee rate ≈ 16.8 shannons/KW.
5. Repeat with different UTXOs. Each submission forces 70M cycles of node CPU at a ~60× discount relative to the configured minimum fee rate.
6. Confirm via node metrics (`ckb_tx_pool_sync_process` / `ckb_tx_pool_async_process`) that verification time per transaction is high while fees collected are minimal.

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

**File:** tx-pool/src/process.rs (L724-754)
```rust
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```

**File:** resource/ckb.toml (L212-215)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```
