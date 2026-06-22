### Title
Miner Proposal Reward Lost When Proposed Transaction Is Replaced via RBF — (`tx-pool/src/pool.rs`, `util/reward-calculator/src/lib.rs`)

---

### Summary

CKB's two-phase commit mechanism creates a deferred-reward structure for proposing miners. A miner who includes a transaction's short ID in the proposal zone of a block pays the opportunity cost of that block space upfront, but receives the 40% proposal reward only if the transaction is later committed within the proposal window. Because CKB explicitly allows RBF replacement of transactions that are already in the `Proposed` state, an attacker (e.g., a competing miner or a transaction sender) can replace a proposed transaction after the proposing miner has already consumed block space, causing the proposing miner to receive zero proposal reward for that slot.

---

### Finding Description

**CKB's two-phase commit and proposal reward design:**

In CKB, every non-cellbase transaction must be *proposed* in one block and *committed* in a later block within the proposal window (`[closest, farthest]`, i.e., `[2, 10]` on mainnet). The miner who first proposes a transaction earns 40% of that transaction's fee as a `proposal_reward`, paid out when the transaction is committed. The committing miner earns the remaining 60%.

The proposal reward is calculated in `RewardCalculator::proposal_reward()`: [1](#0-0) 

This function iterates over committed transactions within the proposal window and accumulates `tx_fee * proposer_ratio` only for transactions that were actually committed: [2](#0-1) 

If a proposed transaction is never committed, it contributes **zero** to `proposal_reward`. The proposing miner receives nothing for the block space they consumed.

**RBF is permitted on `Proposed`-status transactions:**

CKB's `check_rbf()` in `tx-pool/src/pool.rs` enforces fee-rate rules (Rules #1–#5) but contains **no check on the status of the conflicting transaction**. It does not distinguish between `Pending`, `Gap`, and `Proposed` entries: [3](#0-2) 

The codebase explicitly removed a former "Rule #6" that would have blocked replacement of proposed transactions. The test spec `RbfRejectReplaceProposed` documents this decision: [4](#0-3) 

The test confirms that a transaction in `Status::Proposed` can be successfully replaced via RBF: [5](#0-4) 

**Replacement removes the proposed transaction from the pool:**

When RBF succeeds, `process_rbf()` calls `remove_entry_and_descendants()` on the replaced transaction and records it as `RBFRejected`: [6](#0-5) 

Once removed from the pool, the replaced transaction can no longer be committed. The proposing miner's block space is consumed, but `proposal_reward()` will find no committed entry matching that proposal short ID, so the reward is zero.

---

### Impact Explanation

A proposing miner allocates scarce block space (up to `max_block_proposals_limit` short IDs per block) and expects to earn 40% of each proposed transaction's fee as a deferred reward. When a transaction is replaced via RBF after being proposed, the proposing miner:

1. Loses the 40% proposal reward they anticipated.
2. Cannot recoup the opportunity cost of the block space used for the proposal.

The economic loss scales with the fee of the replaced transaction. A high-fee transaction that is proposed and then replaced causes the proposing miner to lose a proportionally large proposal reward. Repeated across many transactions, this can materially reduce a miner's income without any recourse. [7](#0-6) 

---

### Likelihood Explanation

RBF is enabled on any node where `min_rbf_rate > min_fee_rate`: [8](#0-7) 

The default configuration sets `min_fee_rate = 1000` and `min_rbf_rate = 1500`, so RBF is enabled by default: [9](#0-8) 

Any unprivileged transaction sender can:
1. Submit a high-fee transaction T1 via the `send_transaction` RPC.
2. Wait one block for T1 to be proposed by a miner.
3. Submit T2 (same inputs, higher fee) via `send_transaction` to trigger RBF.

The attacker pays only the marginal RBF fee (`sum(replaced_fees) + extra_rbf_fee`) to cause the proposing miner to lose 40% of T1's fee. A competing miner who wants to suppress rivals' proposal income can do this systematically at low cost.

---

### Recommendation

Consider one or more of the following mitigations:

1. **Restore a status-based RBF restriction**: Reinstate a rule (the former "Rule #6") that prevents RBF replacement of transactions already in `Proposed` or `Gap` status. This directly closes the attack path.

2. **Compensate proposing miners on RBF eviction**: When a proposed transaction is evicted by RBF, credit the proposing miner's expected reward from the replacement transaction's fee. This mirrors the "collect fee upfront" recommendation from the original report.

3. **Require the replacement transaction to re-propose**: Force the replacement transaction to go through the full proposal window again, so the proposing miner of T2 (not T1) earns the reward, and the attacker cannot selectively deny a specific miner's reward.

---

### Proof of Concept

```
1. Attacker calls send_transaction(T1) with fee = F (high value).
   → T1 enters tx-pool with Status::Pending.

2. Miner A mines block N, including T1's proposal_short_id in the proposal zone.
   → T1 transitions to Status::Gap, then Status::Proposed.
   → Miner A expects proposal_reward = 0.4 * F at finalization.

3. Attacker calls send_transaction(T2) where T2 spends the same inputs as T1
   and fee(T2) >= fee(T1) + extra_rbf_fee.
   → check_rbf() in tx-pool/src/pool.rs passes (no status check on T1).
   → process_rbf() calls remove_entry_and_descendants(T1's short_id).
   → T1 is removed from the pool and recorded as RBFRejected.

4. T1 is never committed. RewardCalculator::proposal_reward() finds no
   committed entry matching T1's short_id in the proposal window.
   → Miner A's proposal_reward for block N = 0 for T1's slot.
   → Miner A loses 0.4 * F with no recourse.
```

Key code path:
- `check_rbf()` — no proposed-status guard: [10](#0-9) 
- `process_rbf()` — removes proposed tx unconditionally: [11](#0-10) 
- `proposal_reward()` — only rewards committed txs: [12](#0-11) 
- Test confirming proposed-tx RBF is intentionally allowed: [4](#0-3)

### Citations

**File:** util/reward-calculator/src/lib.rs (L135-156)
```rust
    // Miner get (tx_fee - 40% of tx fee) for tx commitment.
    // Be careful of the rounding, tx_fee - 40% of tx fee is different from 60% of tx fee.
    fn txs_fees(&self, target: &HeaderView) -> CapacityResult<Capacity> {
        let consensus = self.consensus;
        let target_ext = self
            .store
            .get_block_ext(&target.hash())
            .expect("block body stored");

        target_ext
            .txs_fees
            .iter()
            .try_fold(Capacity::zero(), |acc, tx_fee| {
                tx_fee
                    .safe_mul_ratio(consensus.proposer_reward_ratio())
                    .and_then(|proposer| {
                        tx_fee
                            .safe_sub(proposer)
                            .and_then(|miner| acc.safe_add(miner))
                    })
            })
    }
```

**File:** util/reward-calculator/src/lib.rs (L158-272)
```rust
    /// Earliest proposer get 40% of tx fee as reward when tx committed
    ///  block H(19) target H(13) ProposalWindow(2, 5)
    ///                 target                    current
    ///                  /                        /
    ///     10  11  12  13  14  15  16  17  18  19
    ///      \   \   \   \______/___/___/___/
    ///       \   \   \________/___/___/
    ///        \   \__________/___/
    ///         \____________/
    ///
    /// Note on `fn proposal_reward` implementation:
    ///
    /// On mainnet, for block 1~11, the reward target is genesis block.
    /// Genesis block must have the lock serialized in the cellbase witness,
    /// which is set to `genesis.bootstrap_lock`.
    fn proposal_reward(
        &self,
        parent: &HeaderView,
        target: &HeaderView,
    ) -> CapacityResult<Capacity> {
        let mut target_proposals = self.get_proposal_ids_by_hash(&target.hash());

        let proposal_window = self.consensus.tx_proposal_window();
        let proposer_ratio = self.consensus.proposer_reward_ratio();
        let block_number = parent
            .number()
            .checked_add(1)
            .ok_or(CapacityError::Overflow)?;
        let store = self.store;

        let mut reward = Capacity::zero();
        let closest_start = 1u64
            .checked_add(proposal_window.closest())
            .ok_or(CapacityError::Overflow)?;

        // Transaction can be committed at height H(c): H(c) > H(w_close)
        let competing_commit_start = cmp::max(
            block_number.saturating_sub(proposal_window.length()),
            closest_start,
        );

        let mut proposed: HashSet<ProposalShortId> = HashSet::new();
        let mut index = parent.to_owned();

        // NOTE: We have to ensure that `committed_idx_proc` and `txs_fees_proc` return in the
        // same order, the order of transactions in block.
        let committed_idx_proc = |hash: &Byte32| -> Vec<ProposalShortId> {
            store
                .get_block_txs_hashes(hash)
                .into_iter()
                .skip(1)
                .map(|tx_hash| ProposalShortId::from_tx_hash(&tx_hash))
                .collect()
        };

        let txs_fees_proc = |hash: &Byte32| -> Vec<Capacity> {
            store
                .get_block_ext(hash)
                .expect("block ext stored")
                .txs_fees
        };

        let committed_idx = committed_idx_proc(&index.hash());

        let has_committed = target_proposals
            .intersection(&committed_idx.iter().cloned().collect::<HashSet<_>>())
            .next()
            .is_some();
        if has_committed {
            for (id, tx_fee) in committed_idx
                .into_iter()
                .zip(txs_fees_proc(&index.hash()).iter())
            {
                // target block is the earliest block with effective proposals for the parent block
                if target_proposals.remove(&id) {
                    reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
                }
            }
        }

        while index.number() > competing_commit_start && !target_proposals.is_empty() {
            index = store
                .get_block_header(&index.data().raw().parent_hash())
                .expect("header stored");

            // Transaction can be proposed at height H(p): H(p) > H(0)
            let competing_proposal_start =
                cmp::max(index.number().saturating_sub(proposal_window.farthest()), 1);

            let previous_ids = store
                .get_block_hash(competing_proposal_start)
                .map(|hash| self.get_proposal_ids_by_hash(&hash))
                .expect("finalize target exist");

            proposed.extend(previous_ids);

            let committed_idx = committed_idx_proc(&index.hash());

            let has_committed = target_proposals
                .intersection(&committed_idx.iter().cloned().collect::<HashSet<_>>())
                .next()
                .is_some();
            if has_committed {
                for (id, tx_fee) in committed_idx
                    .into_iter()
                    .zip(txs_fees_proc(&index.hash()).iter())
                {
                    if target_proposals.remove(&id) && !proposed.contains(&id) {
                        reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
                    }
                }
            }
        }
        Ok(reward)
    }
```

**File:** tx-pool/src/pool.rs (L80-83)
```rust
    /// Check whether tx-pool enable RBF
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
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

**File:** test/src/specs/tx_pool/replace.rs (L641-644)
```rust
pub struct RbfRejectReplaceProposed;

// RBF Rule #6
// We removed rule #6, even tx in `Gap` and `Proposed` status can be replaced.
```

**File:** test/src/specs/tx_pool/replace.rs (L684-711)
```rust
        let rpc_client0 = node0.rpc_client();
        let ret = wait_until(20, || {
            let res = rpc_client0.get_transaction(txs[2].hash());
            res.tx_status.status == Status::Proposed
        });
        assert!(ret, "tx1 should be proposed");

        let clone_tx = txs[2].clone();
        // Set tx2 fee to a higher value
        let output2 = CellOutputBuilder::default()
            .capacity(capacity_bytes!(70))
            .build();

        let tx1_hash = txs[2].hash();
        let tx2 = clone_tx
            .as_advanced_builder()
            .set_outputs(vec![output2])
            .build();

        // begin to RBF
        let res = node0
            .rpc_client()
            .send_transaction_result(tx2.data().into());
        assert!(res.is_ok());

        let old_tx_status = node0.rpc_client().get_transaction(tx1_hash).tx_status;
        assert_eq!(old_tx_status.status, Status::Rejected);
        assert!(old_tx_status.reason.unwrap().contains("RBFRejected"));
```

**File:** tx-pool/src/process.rs (L190-234)
```rust
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

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
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
```

**File:** util/app-config/src/legacy/tx_pool.rs (L10-12)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
