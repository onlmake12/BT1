### Title
Insufficient RBF Authorization Validation Before Expensive Script Execution Allows CPU Exhaustion - (File: `tx-pool/src/process.rs`)

### Summary

The tx-pool's `pre_check` function validates fee against `min_fee_rate` for potential RBF (Replace-By-Fee) transactions, but defers the actual RBF authorization rules (including the much stricter `min_replace_fee` threshold and structural constraints) to `submit_entry`. This allows an unprivileged transaction sender to force full CKB-VM script verification on transactions that will ultimately be rejected by RBF rules, creating a CPU exhaustion vector.

### Finding Description

In `tx-pool/src/process.rs`, when a transaction's input resolves as `Dead` (i.e., it conflicts with an existing pool transaction), the code takes the RBF path: [1](#0-0) 

The `pre_check` function performs only two validations for the RBF candidate:
1. `check_tx_fee` — verifies `fee >= min_fee_rate` (a low bar)
2. `find_conflict_outpoint` — confirms a conflicting tx exists

It then returns `Ok`, allowing the transaction to proceed to full `ContextualTransactionVerifier` execution including `ScriptVerifier` (CKB-VM execution of lock/type scripts): [2](#0-1) 

Only after this expensive verification does `submit_entry` call `check_rbf` under write lock: [3](#0-2) 

`check_rbf` enforces the actual RBF authorization rules that `pre_check` skips entirely: [4](#0-3) 

The critical gap: `check_tx_fee` checks `fee >= min_fee_rate`, but `check_rbf` Rule #3/#4 requires `fee >= min_replace_fee = sum(all_replaced_txs.fee) + extra_rbf_fee`. These thresholds are entirely different. A transaction with fee just above `min_fee_rate` but far below `min_replace_fee` passes `pre_check` and triggers full VM execution before being rejected.

Additionally, Rule #2 (no new unconfirmed inputs) and Rule #5 (≤100 replacement candidates) are also only checked in `submit_entry`, not in `pre_check`. [5](#0-4) 

### Impact Explanation

An attacker can repeatedly submit transactions that:
- Spend a dead outpoint (conflicting with a known pool tx)
- Pay fee just above `min_fee_rate` (e.g., 1000 shannons/KB)
- But far below `min_replace_fee` (which equals the replaced tx's fee + extra)

Each such transaction passes `pre_check`, enters the async verification queue, and triggers full CKB-VM script execution (potentially millions of cycles) before being rejected in `submit_entry` with `RBFRejected`. The attacker pays only the cost of submitting RPC/P2P messages; the node bears the full CPU cost of script execution.

The `ContextualTransactionVerifier::verify` confirms capacity is checked before scripts run, mirroring the ERC20 analog exactly — one condition (fee/capacity) is validated while the authorization condition (RBF rules) is not: [6](#0-5) 

### Likelihood Explanation

- RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a configurable node option
- The attacker only needs to know a transaction currently in the pool (observable via `get_raw_tx_pool` RPC or P2P relay observation)
- No privileged access required — any RPC caller or P2P peer can submit transactions
- The attack is repeatable and cheap for the attacker (low fee, no confirmation needed)

### Recommendation

Move the core RBF authorization checks (at minimum Rule #2 "no new unconfirmed inputs" and the `min_replace_fee` threshold check) into `pre_check` under the read lock, before the transaction is enqueued for script verification. The structural checks that require write-lock atomicity (final conflict resolution) can remain in `submit_entry`, but the fee-adequacy and input-validity checks do not require write-lock exclusivity and can be evaluated cheaply upfront.

### Proof of Concept

1. Submit a high-value transaction `tx_A` spending `cell_X` to the pool (it enters pending state).
2. Observe `tx_A`'s fee (e.g., 10 CKB).
3. Craft `tx_B` spending the same `cell_X`, with fee = `min_fee_rate * size` (e.g., 363 shannons — far below `min_replace_fee` of ~10 CKB + extra).
4. Submit `tx_B` via `send_transaction` RPC.
5. `pre_check`: `resolve_tx(..., rbf=true)` succeeds; `check_tx_fee` passes (363 ≥ min_fee); `find_conflict_outpoint` finds `tx_A` → returns `Ok`.
6. `verify_rtx` runs full CKB-VM script execution on `tx_B`.
7. `submit_entry` → `check_rbf` → Rule #3/#4: `363 < 10_CKB + extra` → `Err(RBFRejected)`.
8. `tx_B` is added to conflicts pool; node wasted full VM cycles.
9. Repeat step 3–8 indefinitely with fresh `tx_B` variants (different witnesses/outputs to change txid). [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L104-116)
```rust
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

**File:** tx-pool/src/util.rs (L101-115)
```rust
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
```

**File:** tx-pool/src/pool.rs (L574-676)
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

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }

        let tx_cells_deps: Vec<OutPoint> = entry
            .transaction()
            .cell_deps_iter()
            .map(|c| c.out_point())
            .collect();
        for entry in all_conflicted.iter() {
            let hash = entry.inner.transaction().hash();
            if tx_cells_deps.iter().any(|pt| pt.tx_hash() == hash) {
                return Err(Reject::RBFRejected(
                    "new Tx contains cell deps from conflicts".to_string(),
                ));
            }
        }

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

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```
