### Title
Tx-Pool Min-Fee Check Uses Serialized Size Only, Ignoring Actual Cycles Weight, Allowing High-Cycles Transactions to Bypass Effective Fee-Rate Enforcement — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, not its actual weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because cycles are unknown before script execution, the pre-check uses size as a fixed proxy. After `verify_rtx` resolves the true cycle count, no second fee-rate check is performed. An unprivileged tx-pool submitter can craft a transaction with a small serialized size but near-maximum cycles, pass the size-only fee gate, and be admitted to the pool at an effective fee rate up to ~12× below `min_fee_rate`.

---

### Finding Description

**Analog to the external report:** The OUSG contract assumed 1 USDC = 1 USD (a fixed conversion rate) when computing mint amounts, with no oracle check on the actual USDC price. In CKB, `check_tx_fee` assumes `tx_size ≈ weight` (a fixed conversion rate between bytes and resource cost), with no re-check after the actual cycle count is known.

**Root cause — `check_tx_fee`:**

```rust
// tx-pool/src/util.rs  lines 42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The actual weight formula is:

```rust
// util/types/src/core/tx_pool.rs  lines 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (≈ `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`).

**Processing pipeline — no second fee check after cycles are known:**

```rust
// tx-pool/src/process.rs  lines 715-753
let (ret, snapshot) = self.pre_check(&tx).await;          // check_tx_fee uses tx_size only
let (tip_hash, rtx, status, fee, tx_size) = ...;

let verified_ret = verify_rtx(...).await;                  // cycles now known
let verified = ...;

let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size); // no re-check of fee vs weight
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

`submit_entry` and `_submit_entry` perform no fee-rate re-validation against the actual weight.

**Quantified gap:** With `max_tx_verify_cycles = 70_000_000` and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`, a transaction consuming 70 M cycles has an effective weight of ≈ 11,940 bytes. If its serialized size is 1,000 bytes, the min-fee check requires only `1,000 × min_fee_rate / 1,000` shannons, but the true weight-based fee should be `11,940 × min_fee_rate / 1,000` — a ~12× shortfall. The effective admitted fee rate is ≈ 84 shannons/KW against a configured floor of 1,000 shannons/KW.

---

### Impact Explanation

An unprivileged attacker can flood the tx-pool with transactions that:
1. Pass the size-only min-fee gate at ~1/12 of the intended cost.
2. Consume full script-execution CPU time during `verify_rtx`.
3. Occupy pool slots (pool size is byte-capped at 180 MB, not cycle-capped), so many high-cycles, small-size transactions fit simultaneously.
4. Displace legitimate transactions via pool eviction (which does use actual weight), causing confirmation delays for honest users.

The pool eviction mechanism (`limit_size`) does use actual weight, so these entries are eventually evicted when the pool fills — but the attacker can continuously resubmit, sustaining the pressure at a fraction of the intended fee cost.

---

### Likelihood Explanation

Any node reachable via the `send_transaction` RPC or the P2P relay path is a valid entry point. Crafting a small-serialized, high-cycles transaction requires only a script that loops near `max_tx_verify_cycles`; no privileged access, key material, or majority hash power is needed. The attack is cheap to automate and monitor.

---

### Recommendation

After `verify_rtx` resolves the actual cycle count, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, enforce a cycles-proportional fee floor at the pre-check stage by using a declared-cycles upper bound (already available via the `declared_cycles` parameter in `_process_tx`).

---

### Proof of Concept

1. Craft a transaction with serialized size ≈ 1,000 bytes and a lock/type script that consumes ≈ 70,000,000 cycles (near `max_tx_verify_cycles`).
2. Set the fee to exactly `min_fee_rate.fee(1_000)` shannons (e.g., 1,000 shannons at the default 1,000 shannons/KW rate).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1_000 × 1_000 / 1_000 = 1_000 shannons` → passes.
5. `verify_rtx` executes the script, returning `cycles ≈ 70_000_000`.
6. Actual weight = `max(1_000, 70_000_000 × 0.000_170_571_4)` ≈ 11,940.
7. True required fee = `11,940 × 1_000 / 1_000 = 11,940 shannons` — but only 1,000 were paid.
8. `TxEntry::new(rtx, 70_000_000, 1_000_shannons, 1_000)` is created and submitted with no further fee check.
9. Repeat at scale to fill the pool with cycle-heavy, fee-light entries.

**Key files and lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
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

**File:** tx-pool/src/process.rs (L705-754)
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
        try_or_return_with_snapshot!(ret, submit_snapshot);
```
