Audit Report

## Title
Orphan Transaction Pool Admission Bypasses Fee Check, Enabling Free Griefing DoS — (`tx-pool/src/process.rs`, `tx-pool/src/component/orphan.rs`)

## Summary
The orphan pool admission path in `after_process` calls `add_orphan` unconditionally when `is_missing_input` returns true, with no fee-rate gate. Any P2P peer can flood the 100-slot orphan pool with zero-fee transactions referencing fabricated outpoints, continuously evicting legitimate orphans and preventing their promotion when parents arrive.

## Finding Description
In `pre_check` (`process.rs` L286–312), `resolve_tx` is called and its result matched. `check_tx_fee` is invoked only on the `Ok` branch (L289) and the `Err(Reject::Resolve(OutPointError::Dead(_)))` branch (L294). The catch-all `Err(err) => Err(err)` at L311 returns directly, skipping `check_tx_fee` for all other errors including `OutPointError::Unknown`. [1](#0-0) 

`after_process` (L507–512) checks `is_missing_input(reject)` and, if true, calls `self.add_orphan(tx, peer, declared_cycle).await` with no fee check of any kind. [2](#0-1) 

`is_missing_input` matches exactly `Reject::Resolve(out_point_err) if out_point_err.is_unknown()`, which is the error produced by fabricated outpoints. [3](#0-2) 

`add_orphan_tx` in `OrphanPool` only checks for duplicates before inserting, then calls `limit_size()`. [4](#0-3) 

`limit_size()` evicts by `self.entries.keys().next()` — pseudo-random HashMap iteration order — once the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. [5](#0-4) 

The only existing guards are `non_contextual_verify` (structural validity) and a duplicate check. Neither constitutes a fee or rate-limit barrier for orphan admission.

## Impact Explanation
This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker can permanently saturate the orphan pool of any targeted node with 100 zero-fee transactions. When a legitimate parent transaction is confirmed or relayed, `process_orphan_tx` finds no matching child and the child is silently dropped, requiring user resubmission. Sustained flooding keeps the orphan pool permanently saturated across targeted nodes, disrupting orphan-based transaction chaining network-wide at negligible cost. [6](#0-5) 

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer. No on-chain funds are required — the attacker constructs transactions spending fabricated outpoints (random 32-byte tx hash, index 0) that pass `non_contextual_verify` but fail `resolve_tx` with `OutPointError::Unknown`. Only 100 messages are needed to fill the pool. The attack can be sustained continuously by re-sending transactions as old ones expire per `ORPHAN_TX_EXPIRE_TIME`. The eviction is pseudo-random, so an attacker re-flooding after each expiry keeps the pool saturated indefinitely. [7](#0-6) 

## Recommendation
1. Require a minimum declared fee density using `declared_cycle` and transaction size already available at orphan admission time, rejecting orphans below `min_fee_rate` before calling `add_orphan`.
2. Enforce a per-peer orphan admission rate limit (e.g., sliding window counter per `PeerIndex`) to raise the cost of sustained flooding.
3. Prioritize orphan eviction by lowest declared fee density rather than pseudo-random HashMap iteration order, so legitimate higher-fee orphans survive flooding.

## Proof of Concept
1. Connect to a CKB node as a P2P peer via the relay protocol.
2. Generate 100+ transactions each spending a fabricated `OutPoint` (random 32-byte tx hash, index 0). Each transaction must pass `non_contextual_verify`: correct molecule encoding, non-cellbase, within size limits, valid witness structure.
3. Relay each transaction via `RelayTransactionHashes` / `GetRelayTransactions` with any `declared_cycle` value.
4. Each transaction traverses: `process_tx` → `non_contextual_verify` (passes) → `_process_tx` → `pre_check` → `resolve_tx` returns `Err(Reject::Resolve(OutPointError::Unknown(...)))` → `after_process` → `is_missing_input` returns `true` → `add_orphan` called with no fee check.
5. After 100 transactions, `limit_size` in `add_orphan_tx` randomly evicts legitimate orphan entries.
6. Submit a legitimate parent transaction. `process_orphan_tx` finds no matching child in the orphan pool. The child is lost and must be resubmitted by the user. [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L286-312)
```rust
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
```

**File:** tx-pool/src/process.rs (L458-526)
```rust
    pub(crate) async fn after_process(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
        _snapshot: &Snapshot,
        ret: &Result<Completed, Reject>,
    ) {
        let tx_hash = tx.hash();

        // log tx verification result for monitor node
        if log_enabled_target!("ckb_tx_monitor", Trace)
            && let Ok(c) = ret
        {
            trace_target!(
                "ckb_tx_monitor",
                r#"{{"tx_hash":"{:#x}","cycles":{}}}"#,
                tx_hash,
                c.cycles
            );
        }

        if matches!(
            ret,
            Err(Reject::RBFRejected(..) | Reject::Resolve(OutPointError::Dead(_)))
        ) {
            let mut tx_pool = self.tx_pool.write().await;
            if tx_pool.pool_map.find_conflict_outpoint(&tx).is_some() {
                tx_pool.record_conflict(tx.clone());
            }
        }

        match remote {
            Some((declared_cycle, peer)) => match ret {
                Ok(_) => {
                    debug!(
                        "after_process remote send_result_to_relayer {} {}",
                        tx_hash, peer
                    );
                    self.send_result_to_relayer(TxVerificationResult::Ok {
                        original_peer: Some(peer),
                        tx_hash,
                    });
                    self.process_orphan_tx(&tx).await;
                }
                Err(reject) => {
                    debug!(
                        "after_process {} {} remote reject: {} ",
                        tx_hash, peer, reject
                    );
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
                }
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L96-132)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
    }
```

**File:** tx-pool/src/component/orphan.rs (L134-158)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```
