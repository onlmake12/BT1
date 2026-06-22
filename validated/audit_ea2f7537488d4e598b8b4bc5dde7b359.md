### Title
Transaction Ordering Dependency Allows RBF-Based Front-Running of Pending Transactions — (`tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

### Summary
When Replace-By-Fee (RBF) is enabled on a CKB node, any unprivileged RPC caller can observe a pending transaction in the mempool and submit a conflicting transaction with a higher fee to evict the original. The original submitter's transaction is permanently rejected with `RBFRejected`. This is a direct analog to the Ethereum front-running race condition described in the external report: transaction ordering is observable and exploitable before finality.

### Finding Description

CKB's tx-pool exposes all pending transactions via the `get_raw_tx_pool` RPC. When `min_rbf_rate > min_fee_rate` in the node configuration, RBF is active. [1](#0-0) 

The `check_rbf` function, called inside the write lock in `submit_entry`, validates a replacement transaction against the incumbent: [2](#0-1) 

Rules #3 and #4 require only that the new transaction's fee exceeds the sum of all displaced transactions' fees plus a small `extra_rbf_fee`: [3](#0-2) 

Once `check_rbf` passes, `process_rbf` unconditionally removes the original transaction and all its descendants from the pool, records them in the conflicts cache, and fires reject callbacks: [4](#0-3) 

The evicted transaction is permanently marked `RBFRejected` and is not automatically re-queued. [5](#0-4) 

There is a structural TOCTOU window that amplifies this: `pre_check` acquires only a **read lock** and returns the `tip_hash` snapshot, then releases the lock before the (potentially long) `verify_rtx` script execution. A competing transaction can be accepted into the pool during this window. When `submit_entry` later acquires the write lock, it re-checks the snapshot change but does not re-resolve inputs against the new pool state for the RBF path: [6](#0-5) 

The tx-pool's own snapshot is explicitly documented as potentially stale: [7](#0-6) 

### Impact Explanation

An attacker who monitors the mempool can:
1. Observe Alice's pending transaction (e.g., spending a time-locked cell, claiming a reward, or participating in a first-come-first-served on-chain contract).
2. Construct a conflicting transaction spending the same input cell with a fee just above `min_replace_fee`.
3. Submit it via `send_transaction` RPC.
4. Alice's transaction is evicted and permanently rejected. She must resubmit with a higher fee, potentially missing a time-sensitive window.

For time-locked cells or epoch-bounded scripts, the window of opportunity may close before Alice can resubmit, making the loss permanent.

### Likelihood Explanation

- RBF is an opt-in node configuration (`min_rbf_rate > min_fee_rate`), but it is a documented and supported feature.
- The mempool is fully public via `get_raw_tx_pool` RPC — no authentication required.
- The attacker needs only to pay a marginally higher fee (`sum(replaced_fees) + extra_rbf_fee`), which is a low economic barrier.
- The attack requires no special privileges, no majority hashpower, and no social engineering.

### Recommendation

**Short term:** Document that RBF-enabled nodes expose pending transactions to front-running. Warn users of time-sensitive transactions to use a private mempool relay or split transactions to reduce detectability. Expose `min_replace_fee` prominently in RPC responses so users can gauge replacement risk (this is already partially done via `get_transaction` verbosity=2).

**Long term:** Consider a commit-reveal scheme or time-delay for high-value time-sensitive transactions at the application layer. At the protocol layer, evaluate whether the proposal-commit two-phase design (which already provides some ordering protection) can be leveraged to reduce the front-running window for proposed transactions.

### Proof of Concept

1. Alice submits `tx_A` spending `outpoint_X` with fee `F` via `send_transaction` RPC.
2. Attacker polls `get_raw_tx_pool` and observes `tx_A` in the pending set.
3. Attacker constructs `tx_B` spending the same `outpoint_X` with fee `F + extra_rbf_fee + 1`.
4. Attacker submits `tx_B` via `send_transaction` RPC.
5. Inside `submit_entry` → `check_rbf`: `tx_B`'s fee satisfies Rule #3/#4. [8](#0-7) 

6. `process_rbf` removes `tx_A` from the pool. [9](#0-8) 

7. `tx_A` is recorded in `conflicts_cache` with reason `"replaced by tx {tx_B_hash}"`.
8. Alice queries `get_transaction(tx_A_hash)` and receives `Status::Rejected` with reason `RBFRejected`. [10](#0-9) 

The integration test `RbfConcurrency` in `test/src/specs/tx_pool/replace.rs` demonstrates that concurrent conflicting submissions resolve deterministically in favor of the highest-fee transaction, confirming the attacker's ability to reliably displace any lower-fee pending transaction. [11](#0-10)

### Citations

**File:** tx-pool/src/pool.rs (L48-51)
```rust
    pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
    // conflicted transaction outputs cache, input -> tx_short_id
    pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
}
```

**File:** tx-pool/src/pool.rs (L70-73)
```rust
    /// Tx-pool owned snapshot, it may not consistent with chain cause tx-pool update snapshot asynchronously
    pub(crate) fn snapshot(&self) -> &Snapshot {
        &self.snapshot
    }
```

**File:** tx-pool/src/pool.rs (L80-83)
```rust
    /// Check whether tx-pool enable RBF
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```

**File:** tx-pool/src/pool.rs (L574-594)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());
```

**File:** tx-pool/src/pool.rs (L662-676)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }
```

**File:** tx-pool/src/process.rs (L96-137)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

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

                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
```

**File:** tx-pool/src/process.rs (L203-232)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
```

**File:** test/src/specs/tx_pool/replace.rs (L879-942)
```rust
pub struct RbfConcurrency;
impl Spec for RbfConcurrency {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];

        node0.mine_until_out_bootstrap_period();
        node0.new_block_with_blocking(|template| template.number.value() != 13);
        let tx_hash_0 = node0.generate_transaction();
        info!("Generate 4 txs with same input");
        let tx1 = node0.new_transaction(tx_hash_0.clone());

        let mut conflicts = vec![tx1];
        // tx1 capacity is 100, set other txs to higher fee
        let fees = [
            capacity_bytes!(83),
            capacity_bytes!(82),
            capacity_bytes!(81),
            capacity_bytes!(80),
        ];
        for fee in fees.iter() {
            let tx2_temp = node0.new_transaction(tx_hash_0.clone());
            let output = CellOutputBuilder::default().capacity(fee).build();

            let tx2 = tx2_temp
                .as_advanced_builder()
                .set_outputs(vec![output])
                .build();
            conflicts.push(tx2);
        }

        // make 5 threads to set_transaction concurrently
        let mut handles = vec![];
        for tx in &conflicts {
            let cur_tx = tx.clone();
            let rpc_address = node0.rpc_listen();
            let handle = std::thread::spawn(move || {
                let rpc_client = RpcClient::new(&rpc_address);
                let _ = rpc_client.send_transaction_result(cur_tx.data().into());
            });
            handles.push(handle);
        }
        for handle in handles {
            let _ = handle.join();
        }

        let status: Vec<_> = conflicts
            .iter()
            .map(|tx| {
                let res = node0.rpc_client().get_transaction(tx.hash());
                res.tx_status.status
            })
            .collect();

        // the last tx should be in Pending(with the highest fee), others should be in Rejected
        assert_eq!(status[4], Status::Pending);
        for s in status.iter().take(4) {
            assert_eq!(*s, Status::Rejected);
        }

        let mut expected: Vec<ckb_types::H256> =
            conflicts.iter().take(4).map(|x| x.hash().into()).collect();
        expected.sort_unstable();
        assert_eq!(get_tx_pool_conflicts(node0), expected);
    }
```
