### Title
Verify Queue Admits Conflicting Transactions Without Input Locking, Enabling `total_tx_size` Inflation and DoS — (`tx-pool/src/component/verify_queue.rs`, `tx-pool/src/process.rs`)

---

### Summary

The `VerifyQueue` in CKB's tx-pool admits multiple transactions that spend the same input cell simultaneously, because admission only deduplicates by transaction hash (`proposal_short_id`) and never checks for input conflicts. The fee check is deferred until a worker pops the transaction for processing. An unprivileged attacker with a single confirmed cell can flood the verify queue with N conflicting transactions (same input, different outputs → different hashes), inflating `total_tx_size` up to the 256 MB hard cap and causing all subsequent legitimate transactions to be rejected with `Reject::Full`.

---

### Finding Description

**Root cause — `VerifyQueue.add_tx` tracks only by tx hash, not by inputs:** [1](#0-0) 

The queue deduplicates solely on `proposal_short_id` (derived from the tx hash). Two transactions spending the same `OutPoint` but producing different outputs have different hashes and are both admitted.

**Admission path — no input-conflict check before enqueue:** [2](#0-1) 

`resumeble_process_tx` calls `non_contextual_verify`, then checks `orphan_contains` and `verify_queue_contains` (both keyed on tx hash), then calls `enqueue_verify_queue`. There is no step that checks whether any input of the incoming transaction is already claimed by a transaction already sitting in the queue.

**`non_contextual_verify` does not check input conflicts:** [3](#0-2) 

`NonContextualTransactionVerifier` checks version, size, empty inputs/outputs, duplicate deps, outputs data, and script hash type — but not cross-transaction input conflicts. [4](#0-3) 

**Fee check is deferred — not a gate before enqueue:** [5](#0-4) 

`check_tx_fee` requires a resolved transaction and the pool lock; it is called only inside `pre_check`, which runs in the worker after the transaction has already been dequeued from the verify queue. [6](#0-5) 

**`total_tx_size` cap that can be exhausted:** [7](#0-6) [8](#0-7) 

The hard cap is 256 MB. Each transaction can be up to 512 KB (`TRANSACTION_SIZE_LIMIT`). So ≥512 conflicting transactions saturate the queue.

**The main pool's `Edges` map does enforce input uniqueness — but only for transactions already committed to the pool, not for those still in the verify queue:** [9](#0-8) 

This protection is bypassed entirely because conflicting transactions never reach `_submit_entry` together; they pile up in the verify queue first.

---

### Impact Explanation

An attacker holding one confirmed live cell creates N transactions each spending that same cell but with distinct outputs (ensuring distinct tx hashes). Each transaction passes `non_contextual_verify` and the hash-based deduplication checks, and is inserted into the verify queue, incrementing `total_tx_size` by its serialized size. Once `total_tx_size` reaches 256 MB, every subsequent `add_tx` call returns `Reject::Full`, blocking all legitimate transactions from entering the verify queue. Workers will eventually drain the conflicting transactions one by one (all but the first will fail at `pre_check` with `OutPointError::Dead`), but the attacker can continuously re-submit to maintain saturation. The effect is a sustained DoS of the tx-pool's admission pipeline.

---

### Likelihood Explanation

The attack requires only:
1. One confirmed live cell (trivially obtained on mainnet).
2. The ability to call `send_transaction` via RPC or relay transactions via P2P — both are unprivileged, externally reachable entry points.
3. Generating ~512 structurally valid transactions (different outputs, same input) — trivial with any wallet library.

No special privileges, no hashpower, no social engineering. The attack is cheap and repeatable.

---

### Recommendation

Before calling `enqueue_verify_queue`, maintain a per-`OutPoint` index inside the verify queue (mirroring `Edges.inputs` in the main pool). Reject any incoming transaction whose inputs overlap with inputs already claimed by a queued transaction, returning `Reject::RBFRejected` or a new `Reject::ConflictInVerifyQueue` variant. This mirrors the fix recommended in the external report: lock the resource at the point of the first reservation.

---

### Proof of Concept

```
1. Mine a block to get a confirmed cell C at outpoint OP.
2. Construct transactions T1..T512:
     - Each Ti has input = OP
     - Each Ti has a distinct output (e.g., different capacity or lock args)
     - Each Ti passes NonContextualTransactionVerifier (valid version, non-empty, no dup deps)
3. Submit T1..T512 via RPC send_transaction (or P2P relay).
4. Each Ti passes resumeble_process_tx:
     - non_contextual_verify: OK (structurally valid)
     - orphan_contains: false (different hashes)
     - verify_queue_contains: false (different hashes)
     - enqueue_verify_queue: OK, total_tx_size += size(Ti)
5. After ~512 submissions, total_tx_size >= 256 MB.
6. Any subsequent send_transaction from a legitimate user returns:
     Reject::Full("verify_queue total_tx_size exceeded, failed to add tx: 0x...")
7. Workers drain Ti one by one; T1 may succeed, T2..T512 fail with Dead outpoint.
   Attacker re-submits a fresh batch to maintain saturation.
```

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-19)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
const SHRINK_THRESHOLD: usize = 100;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-236)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
```

**File:** tx-pool/src/process.rs (L269-316)
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
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

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

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```

**File:** verification/src/transaction_verifier.rs (L71-102)
```rust
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

    /// Perform context-independent verification
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
    }
```

**File:** tx-pool/src/component/edges.rs (L33-54)
```rust
    pub(crate) fn insert_input(
        &mut self,
        out_point: OutPoint,
        txid: ProposalShortId,
    ) -> Result<(), Reject> {
        // inputs is occupied means double speanding happened here
        match self.inputs.entry(out_point.clone()) {
            Entry::Occupied(occupied) => {
                let msg = format!(
                    "txpool unexpected double-spending out_point: {:?} old_tx: {:?} new_tx: {:?}",
                    out_point,
                    occupied.get(),
                    txid
                );
                Err(Reject::RBFRejected(msg))
            }
            Entry::Vacant(vacant) => {
                vacant.insert(txid);
                Ok(())
            }
        }
    }
```
