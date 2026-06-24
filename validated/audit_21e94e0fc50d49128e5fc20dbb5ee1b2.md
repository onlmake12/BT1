All core claims are verified against the actual code:

**Execution order in `_process_tx`** (process.rs L715→724→753): `pre_check` → `verify_rtx` → `submit_entry` — confirmed. [1](#0-0) 

**RBF branch in `pre_check`** (process.rs L292-309): only `check_tx_fee` + `find_conflict_outpoint` before `Ok` — confirmed. [2](#0-1) 

**`check_tx_fee`** (util.rs L45-52): only checks `fee >= min_fee_rate.fee(tx_size)` — confirmed. [3](#0-2) 

**`verify_rtx`** (util.rs L101-115): runs full `ContextualTransactionVerifier::verify_with_pause` — confirmed. [4](#0-3) 

**`check_rbf`** (pool.rs L574-679): Rule #2, Rule #5, and `min_replace_fee` check all deferred here, after VM execution — confirmed. [5](#0-4) 

**`calculate_min_replace_fee`** (pool.rs L101-127): `min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee`, far exceeding `min_fee_rate * size` — confirmed. [6](#0-5) 

---

Audit Report

## Title
Insufficient RBF Fee Validation Before Expensive Script Execution Enables CPU Exhaustion - (File: `tx-pool/src/process.rs`)

## Summary
In `tx-pool/src/process.rs`, the `pre_check` function's RBF branch only validates `fee >= min_fee_rate` before returning `Ok`, allowing the transaction to proceed to full CKB-VM script execution via `verify_rtx`. The stricter RBF rules — including `min_replace_fee` (sum of replaced tx fees + extra), Rule #2 (no new unconfirmed inputs), and Rule #5 (≤100 replacement candidates) — are only enforced in `submit_entry` via `check_rbf`, which runs after script execution completes. An unprivileged attacker can repeatedly submit low-fee conflicting transactions, forcing full VM execution before rejection at zero cost.

## Finding Description
**Execution order in `_process_tx`** (`tx-pool/src/process.rs` L705–753):
1. `pre_check` (read lock, L715): For the RBF path (`OutPointError::Dead`), only `check_tx_fee` (fee ≥ min_fee_rate) and `find_conflict_outpoint` are called, then `Ok` is returned.
2. `verify_rtx` (L724–732): Full `ContextualTransactionVerifier::verify_with_pause` runs up to `max_block_cycles` cycles.
3. `submit_entry` (write lock, L753): Calls `check_rbf`, which enforces Rule #2 (L602–609), Rule #5 (L619–623), and `min_replace_fee` (L665–676).

**Root cause — `pre_check` RBF branch** (`tx-pool/src/process.rs` L292–309): `check_tx_fee` (`tx-pool/src/util.rs` L45–52) only checks `fee >= min_fee_rate.fee(tx_size)` — the absolute minimum bar. No RBF-specific fee adequacy check is performed before VM execution.

**Deferred check — `check_rbf`** (`tx-pool/src/pool.rs` L574–679): `calculate_min_replace_fee` (`tx-pool/src/pool.rs` L101–127) computes `min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee`, which is orders of magnitude higher than `min_fee_rate * size` when the replaced transaction carries any meaningful fee. This check only runs after full VM execution completes.

**Existing guards are insufficient**: `check_txid_collision` prevents exact duplicate txids but the attacker trivially varies witnesses to produce fresh txids. The verify queue size limit (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) is a consequence of the attack, not a mitigation — filling it causes `Reject::Full` for legitimate transactions.

## Impact Explanation
**High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker submitting RBF-candidate transactions with fee just above `min_fee_rate` (e.g., 363 shannons) forces full CKB-VM execution (up to `max_block_cycles` cycles) per transaction before the node rejects them via `check_rbf`. The attacker pays zero fee (transactions are rejected before entering the pool). Sustained submission saturates the verify worker threads with CPU-intensive script execution, and once the verify queue fills, all legitimate transactions receive `Reject::Full`, blocking normal pool operation across the network.

## Likelihood Explanation
- RBF is enabled by default when `min_rbf_rate > min_fee_rate` (production config).
- Any pool transaction is observable via `get_raw_tx_pool` RPC or P2P relay — no privileged access required.
- The attacker varies witnesses to change the txid, bypassing `check_txid_collision`, making the attack indefinitely repeatable.
- Cost per iteration: a single RPC/P2P message with minimal fee (363 shannons for a small tx vs. potentially millions of shannons for `min_replace_fee`).

## Recommendation
Move the core RBF fee adequacy check into `pre_check` before the transaction is enqueued for script verification. Within the `Err(Reject::Resolve(OutPointError::Dead(out)))` branch of `pre_check` (`tx-pool/src/process.rs` L292–309), after `find_conflict_outpoint` confirms a conflict exists, compute a preliminary `min_replace_fee` using the conflicting transaction's fee (readable under the existing read lock) and reject early if `fee < preliminary_min_replace_fee`. Rule #2 (no new unconfirmed inputs against the snapshot) can also be checked cheaply under the read lock. Structural checks requiring write-lock atomicity (final conflict set resolution) can remain in `submit_entry`.

## Proof of Concept
1. Submit `tx_A` spending `cell_X` with fee = 10 CKB. Observe it enters pending state via `get_raw_tx_pool`.
2. Craft `tx_B` spending the same `cell_X`, with fee = `min_fee_rate * size` (e.g., 363 shannons — far below `min_replace_fee` of ~10 CKB + extra).
3. Submit `tx_B` via `send_transaction` RPC.
4. **`pre_check`**: `resolve_tx(..., rbf=true)` succeeds; `check_tx_fee` passes (363 ≥ min_fee); `find_conflict_outpoint` finds `tx_A` → returns `Ok`.
5. **`verify_rtx`**: Full CKB-VM script execution runs on `tx_B` (up to `max_block_cycles` consumed).
6. **`submit_entry` → `check_rbf`**: Rule #3/#4: `363 < 10_CKB + extra` → `Err(RBFRejected)`.
7. Node wasted full VM cycles; `tx_B` is rejected.
8. Repeat steps 2–7 with fresh `tx_B` variants (different witnesses to change txid). Each iteration forces full VM execution at zero cost to the attacker.
9. With sufficient submission rate, the verify queue fills, causing `Reject::Full` for all legitimate transactions.

### Citations

**File:** tx-pool/src/process.rs (L292-309)
```rust
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

**File:** tx-pool/src/util.rs (L45-52)
```rust
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
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

**File:** tx-pool/src/pool.rs (L101-127)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
        if let Ok(res) = res {
            Some(res)
        } else {
            let fees = conflicts.iter().map(|c| c.inner.fee).collect::<Vec<_>>();
            error!(
                "conflicts: {:?} replaced_sum_fee {:?} overflow by add {}",
                conflicts.iter().map(|e| e.id.clone()).collect::<Vec<_>>(),
                fees,
                extra_rbf_fee
            );
            None
        }
    }
```

**File:** tx-pool/src/pool.rs (L574-679)
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

        Ok(conflict_ids)
    }
```
