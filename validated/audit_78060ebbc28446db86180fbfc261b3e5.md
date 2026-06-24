Audit Report

## Title
Tx-Pool Min-Fee Check Uses Serialized Size Only, Ignoring Actual Cycles Weight, Allowing High-Cycles Transactions to Bypass Effective Fee-Rate Enforcement — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, not its actual weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). After `verify_rtx` resolves the true cycle count, no second fee-rate check is performed before the entry is admitted to the pool. An unprivileged submitter can craft a transaction with a small serialized size but near-maximum cycles, pass the size-only fee gate, and be admitted to the pool at an effective fee rate up to ~12× below `min_fee_rate`, enabling sustained CPU exhaustion and pool flooding at a fraction of the intended cost.

## Finding Description

**Root cause — `check_tx_fee` (`tx-pool/src/util.rs`, lines 42–45):**

The function computes the minimum required fee using only `tx_size`, not the actual weight. The code comment itself acknowledges this is a known approximation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The actual weight formula (`util/types/src/core/tx_pool.rs`, lines 298–303) is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

**No second fee check after cycles are known (`tx-pool/src/process.rs`, lines 715–753):**

`_process_tx` calls `pre_check` (which invokes `check_tx_fee` with size only), then calls `verify_rtx` to obtain the actual cycle count, then immediately creates the `TxEntry` and calls `submit_entry` — with no weight-based fee re-validation:

```rust
let (ret, snapshot) = self.pre_check(&tx).await;          // size-only fee check
let (tip_hash, rtx, status, fee, tx_size) = ...;
let verified_ret = verify_rtx(...).await;                  // cycles now known
let verified = ...;
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size); // no re-check
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [4](#0-3) 

`submit_entry` and `_submit_entry` perform no fee-rate re-validation against actual weight.

**Pool size limit is byte-based, not cycle-based (`tx-pool/src/pool.rs`, line 298):**

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [5](#0-4) 

With `max_tx_pool_size = 180_000_000` bytes and 1,000-byte transactions, ~180,000 high-cycles entries can coexist in the pool simultaneously. [6](#0-5) 

**Quantified gap:** With `max_tx_verify_cycles = 70_000_000` and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`, a 1,000-byte transaction consuming 70M cycles has an effective weight of ≈ 11,940 bytes. The size-only fee gate requires only 1,000 shannons (at 1,000 shannons/KW), while the true weight-based fee should be 11,940 shannons — a ~12× shortfall.

## Impact Explanation

This maps to **High: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An attacker can:
1. Pass the size-only min-fee gate at ~1/12 of the intended cost.
2. Force the node to execute full script verification (`verify_rtx`) for each submitted transaction, consuming CPU at near-`max_tx_verify_cycles` per transaction.
3. Flood the pool with up to ~180,000 such entries simultaneously (pool is byte-capped, not cycle-capped).
4. Displace legitimate transactions via pool eviction (which does use actual weight via `EvictKey`), causing confirmation delays for honest users.
5. Continuously resubmit evicted transactions, sustaining the attack at a fraction of the intended fee cost. [7](#0-6) 

## Likelihood Explanation

Any node reachable via the `send_transaction` RPC or the P2P relay path is a valid entry point. Crafting a small-serialized, high-cycles transaction requires only a script that loops near `max_tx_verify_cycles`; no privileged access, key material, or majority hash power is needed. The attack is cheap to automate and sustain indefinitely.

## Recommendation

After `verify_rtx` resolves the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()
    )), snapshot));
}
```

This check should be inserted between lines 734 and 751 of `tx-pool/src/process.rs`. Alternatively, enforce a cycles-proportional fee floor at the pre-check stage using the `declared_cycles` parameter already available in `_process_tx` (line 708). [8](#0-7) 

## Proof of Concept

1. Craft a transaction with serialized size ≈ 1,000 bytes and a lock/type script that consumes ≈ 70,000,000 cycles (near `max_tx_verify_cycles`).
2. Set the fee to exactly `min_fee_rate.fee(1_000)` = 1,000 shannons (at the default 1,000 shannons/KW rate).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1_000 × 1_000 / 1_000 = 1_000 shannons` → passes.
5. `verify_rtx` executes the script, returning `cycles ≈ 70_000_000`.
6. Actual weight = `max(1_000, 70_000_000 × 0.000_170_571_4)` ≈ 11,940.
7. True required fee = `11,940 × 1_000 / 1_000 = 11,940 shannons` — but only 1,000 were paid.
8. `TxEntry::new(rtx, 70_000_000, 1_000_shannons, 1_000)` is created and submitted with no further fee check.
9. Repeat at scale (~180,000 transactions) to fill the pool with cycle-heavy, fee-light entries, sustaining CPU pressure and displacing legitimate transactions. [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
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

**File:** tx-pool/src/process.rs (L705-753)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** resource/ckb.toml (L211-215)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
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
