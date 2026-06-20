### Title
Missing `since`/Time-Relative Re-Validation During Tx-Pool Reorg Promotion — (`tx-pool/src/process.rs`, `tx-pool/src/pool.rs`)

---

### Summary

When a chain reorganization occurs, the tx-pool promotes transactions from `Pending`/`Gap` status to `Proposed` status inside `_update_tx_pool_for_reorg`. This promotion calls `proposed_rtx` / `gap_rtx`, which only relabel the pool entry's status — they do **not** re-run `time_relative_verify` (the `since` / cellbase-maturity check) against the new chain tip. If the reorg moves the tip to a lower block number, a transaction whose `since` condition was satisfied at the old tip may no longer be satisfied at the new tip, yet it remains in the proposed pool and is packaged into the next block template. The miner then builds and submits a block that the chain verifier rejects, wasting the block reward.

---

### Finding Description

**Admission-time check (correct path):**

When a transaction is first submitted via `_process_tx`, it goes through `pre_check` → `verify_rtx`. `verify_rtx` calls `ContextualTransactionVerifier`, which internally calls `TimeRelativeTransactionVerifier` (maturity + `SinceVerifier`). The `TxVerifyEnv` used is `TxVerifyEnv::new_submit(tip_header)`, which projects the effective commit block number forward by `1 + proposal_window.closest()` blocks. [1](#0-0) [2](#0-1) 

**Reorg promotion path (missing re-check):**

During `_update_tx_pool_for_reorg`, after the snapshot is updated to the new (potentially lower) tip, pending and gap transactions whose proposal IDs appear in the new chain's proposal window are promoted: [3](#0-2) 

`tx_pool.proposed_rtx(&id)` only calls `self.set_entry_proposed(short_id)` — a pure status-label change with no `time_relative_verify`: [4](#0-3) 

Similarly, `tx_pool.gap_rtx(&id)` only calls `self.set_entry_gap(short_id)`: [5](#0-4) 

**Block assembly does not re-check `since`:**

`calc_dao` in the block assembler only performs a cell-resolve check (`entry.rtx.check(...)`), not a `since` / maturity check. Transactions that fail `since` at the new tip are silently included in the block template: [6](#0-5) 

**Developer acknowledgment:**

The integration test `test_since_and_proposal` is explicitly disabled with the comment `// TODO: Uncomment this case after proposed/pending pool tip verify logic changing`, confirming the developers are aware that the pending/proposed pool does not correctly re-verify `since` conditions when the tip changes: [7](#0-6) 

---

### Impact Explanation

A miner running in mine-mode assembles a block template from the proposed pool. If the proposed pool contains a transaction whose `since` condition is no longer satisfied at the current tip (due to a reorg), that transaction is included in the template. When the miner submits the block via `submit_block`, the chain verifier runs the full `ContextualTransactionVerifier` (including `SinceVerifier`) and rejects the block. The miner loses the block reward for that round. Repeated occurrences cause sustained mining DoS.

---

### Likelihood Explanation

Natural reorgs of 1–2 blocks occur regularly on any PoW chain. An attacker only needs to submit a transaction with a `since` value of `current_tip + 1 + proposal_window.closest()` (the minimum value that passes the admission check). Any 1-block reorg will then cause the `since` condition to fail at the new tip while the transaction remains in the proposed pool. No special privileges, hashpower, or Sybil capability are required — only the ability to submit a transaction via `send_transaction` RPC and wait for a natural reorg. [8](#0-7) [9](#0-8) 

---

### Recommendation

In `_update_tx_pool_for_reorg`, before calling `tx_pool.proposed_rtx(&id)` or `tx_pool.gap_rtx(&id)`, re-run `time_relative_verify` against the new snapshot tip. If the check fails, call `callbacks.call_reject` and skip promotion (do not move the entry to proposed/gap). This mirrors the existing pattern in `submit_entry` where `time_relative_verify` is re-run when the snapshot tip changes between pre-check and submission: [10](#0-9) 

---

### Proof of Concept

1. Chain is at tip block `T`. `proposal_window.closest() = 2` (default).
2. Attacker submits a transaction with `since = absolute_block_number(T + 3)`. Admission check uses `TxVerifyEnv::new_submit` → effective block = `T + 1 + 2 = T + 3 >= T + 3`. **Passes.**
3. Transaction enters the pending pool.
4. Block `T+1` is mined; the transaction's proposal ID is included. Transaction moves to gap.
5. A natural 1-block reorg occurs: the chain reverts to tip `T` and a competing block `T+1'` is accepted (without the proposal).
6. `_update_tx_pool_for_reorg` is called. The new snapshot tip is `T`. The transaction's proposal ID is in the new chain's proposal window (assume it was re-proposed in `T+1'`). `tx_pool.proposed_rtx(&id)` is called — **no `since` re-check** — transaction is now `Proposed`.
7. `get_block_template` is called. `package_txs` returns the transaction. `calc_dao` only does a resolve check — **no `since` check**. Transaction is in the block template.
8. Miner submits the block. Chain verifier runs `SinceVerifier` with `TxVerifyEnv::new_commit` at block `T+2`. Effective block = `T+2 < T+3`. **Block rejected.**
9. Miner loses block reward. Repeat for each natural reorg. [11](#0-10) [12](#0-11)

### Citations

**File:** tx-pool/src/util.rs (L85-100)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
```

**File:** verification/src/transaction_verifier.rs (L53-58)
```rust
    /// Perform time-related verification
    pub fn verify(&self) -> Result<(), Error> {
        self.maturity.verify()?;
        self.since.verify()?;
        Ok(())
    }
```

**File:** tx-pool/src/process.rs (L118-134)
```rust
                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }
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

**File:** tx-pool/src/process.rs (L1039-1114)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());

    // mine mode:
    // pending ---> gap ----> proposed
    // try move gap to proposed
    if mine_mode {
        let mut proposals = Vec::new();
        let mut gaps = Vec::new();

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
            let short_id = entry.inner.proposal_short_id();
            let elem = (short_id.clone(), entry.inner.clone());
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push(elem);
            } else if snapshot.proposals().contains_gap(&short_id) {
                gaps.push(elem);
            }
        }

        for (id, entry) in proposals {
            debug!("begin to proposed: {:x}", id);
            if let Err(e) = tx_pool.proposed_rtx(&id) {
                debug!(
                    "Failed to add proposed tx {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e);
            } else {
                callbacks.call_proposed(&entry)
            }
        }

        for (id, entry) in gaps {
            debug!("begin to gap: {:x}", id);
            if let Err(e) = tx_pool.gap_rtx(&id) {
                debug!(
                    "Failed to add tx to gap {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e.clone());
            }
        }
    }

    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
}
```

**File:** tx-pool/src/pool.rs (L386-403)
```rust
    pub(crate) fn gap_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
        match self.get_pool_entry(short_id) {
            Some(entry) => {
                let tx_hash = entry.inner.transaction().hash();
                if entry.status == Status::Gap {
                    Err(Reject::Duplicated(tx_hash))
                } else {
                    debug!("gap_rtx: {:?} => {:?}", tx_hash, short_id);
                    self.set_entry_gap(short_id);
                    Ok(())
                }
            }
            None => Err(Reject::Malformed(
                String::from("invalid short_id"),
                Default::default(),
            )),
        }
    }
```

**File:** tx-pool/src/pool.rs (L405-422)
```rust
    pub(crate) fn proposed_rtx(&mut self, short_id: &ProposalShortId) -> Result<(), Reject> {
        match self.get_pool_entry(short_id) {
            Some(entry) => {
                let tx_hash = entry.inner.transaction().hash();
                if entry.status == Status::Proposed {
                    Err(Reject::Duplicated(tx_hash))
                } else {
                    debug!("proposed_rtx: {:?} => {:?}", tx_hash, short_id);
                    self.set_entry_proposed(short_id);
                    Ok(())
                }
            }
            None => Err(Reject::Malformed(
                String::from("invalid short_id"),
                Default::default(),
            )),
        }
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L628-681)
```rust
    fn calc_dao(
        snapshot: &Snapshot,
        current_epoch: &EpochExt,
        cellbase: TransactionView,
        entries: Vec<TxEntry>,
    ) -> CalcDaoResult {
        let tip_header = snapshot.tip_header();
        let consensus = snapshot.consensus();
        let mut seen_inputs = HashSet::new();
        let mut transactions_checker = TransactionsChecker::new(iter::once(&cellbase));

        let mut checked_failed_txs = vec![];
        let checked_entries: Vec<_> = block_in_place(|| {
            entries
                .into_iter()
                .filter_map(|entry| {
                    let overlay_cell_checker =
                        OverlayCellChecker::new(&transactions_checker, snapshot);
                    if let Err(err) =
                        entry
                            .rtx
                            .check(&mut seen_inputs, &overlay_cell_checker, snapshot)
                    {
                        error!(
                            "Resolving transactions while building block template, \
                             tip_number: {}, tip_hash: {}, tx_hash: {}, error: {:?}",
                            tip_header.number(),
                            tip_header.hash(),
                            entry.transaction().hash(),
                            err
                        );
                        // Returning the out_point makes debugging easier and provides better logs.
                        checked_failed_txs
                            .push((entry.proposal_short_id(), err.out_point().cloned()));
                        None
                    } else {
                        transactions_checker.insert(entry.transaction());
                        Some(entry)
                    }
                })
                .collect()
        });

        let dummy_cellbase_entry = TxEntry::dummy_resolve(cellbase, 0, Capacity::zero(), 0);
        let entries_iter = iter::once(&dummy_cellbase_entry)
            .chain(checked_entries.iter())
            .map(|entry| entry.rtx.as_ref());

        // Generate DAO fields here
        let dao = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
            .dao_field_with_current_epoch(entries_iter, tip_header, current_epoch)?;

        Ok((dao, checked_entries, checked_failed_txs))
    }
```

**File:** test/src/specs/tx_pool/valid_since.rs (L23-24)
```rust
        // TODO: Uncomment this case after proposed/pending pool tip verify logic changing
        // self.test_since_and_proposal(&nodes[1]);
```

**File:** script/src/verify_env.rs (L84-92)
```rust
    pub fn block_number(&self, proposal_window: ProposalWindow) -> BlockNumber {
        match self.phase {
            TxVerifyPhase::Submitted => self.number + 1 + proposal_window.closest(),
            TxVerifyPhase::Proposed(already_proposed) => {
                self.number.saturating_sub(already_proposed) + proposal_window.closest()
            }
            TxVerifyPhase::Committed => self.number,
        }
    }
```
