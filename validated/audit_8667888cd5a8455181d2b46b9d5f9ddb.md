### Title
No Incentive for Transaction Senders to Submit Early; Proposed Transactions Replaceable at Last Minute via RBF — (File: `tx-pool/src/pool.rs`)

### Summary
CKB's RBF mechanism contains no status guard: transactions already in `Proposed` status (inside the two-phase commit window) can be replaced by any tx-pool submitter who pays a marginally higher fee. Because all pending transactions are publicly visible via RPC, submitting a transaction early is strictly worse than waiting — it reveals the transaction to potential replacers. An attacker with sufficient funds can observe any pending transaction, wait until it is proposed, and then replace it one block before the commit window closes, censoring the original sender and stripping the proposing miner of their 40% fee reward.

---

### Finding Description

CKB uses a two-phase commit system: a transaction must first be **proposed** in a block at height `H`, then **committed** in a block at height `H + w_close` to `H + w_far` (mainnet: `w_close = 2`, `w_far = 10`). The miner who first proposes a transaction earns 40% of its fee as a proposer reward, paid only when the transaction is eventually committed.

CKB also supports Replace-By-Fee (RBF). The critical design decision is that **Rule #6 — which would have prevented replacing transactions already in `Gap` or `Proposed` status — was explicitly removed**. This is documented in the integration test file:

```rust
// RBF Rule #6
// We removed rule #6, even tx in `Gap` and `Proposed` status can be replaced.
```

The root cause is in `check_rbf` in `tx-pool/src/pool.rs`. It calls `pool_map.find_conflict_tx`, which returns conflicting transactions by matching input outpoints with no filter on their status:

```rust
pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
    tx.input_pts_iter()
        .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
        .collect()
}
```

`check_rbf` then enforces only Rules #1–#5 (RBF enabled, no new unconfirmed inputs, fee threshold, descendant count limit, cell-dep check). There is **no check on whether the conflicting transaction is `Pending`, `Gap`, or `Proposed`**. Any transaction in any of these states can be evicted.

The attack flow mirrors the Hermez last-minute bidding scenario:

1. Alice submits transaction `T` with fee `F` via `send_transaction` RPC.
2. `T` is proposed in block `H` (status transitions to `Proposed`, observable via `get_transaction` RPC).
3. Bob monitors the public mempool, observes `T`, and computes the minimum replacement fee: `F + min_rbf_rate * tx_size`.
4. Bob submits `T'` (same inputs, slightly higher fee) just before the commit window closes.
5. `T` is evicted from the pool with status `Rejected` (reason: `RBFRejected: replaced by tx …`).
6. `T'` is committed instead; Alice's transaction is never confirmed.
7. The miner who proposed `T` receives **zero** proposer reward, because `proposal_reward` in `util/reward-calculator/src/lib.rs` only pays the proposer when the proposed transaction is committed.

Because all mempool contents are public, submitting `T` early gives Bob more time to observe and prepare the replacement. There is no incentive for Alice to submit early — the opposite of the intended economic design.

---

### Impact Explanation

**Transaction censorship**: Any tx-pool submitter can replace any pending or proposed transaction by paying `sum(replaced_fees) + min_rbf_rate * size` — a marginal increment. This enables targeted censorship of specific transactions or senders.

**Proposer reward loss**: The `proposal_reward` function in `util/reward-calculator/src/lib.rs` iterates committed transactions and pays the proposer only if the proposed transaction was committed. If `T` is replaced before commitment, the proposing miner's 40% reward is permanently lost, undermining the economic incentive to propose transactions.

**No incentive to submit early**: Since all pending transactions are publicly visible and proposed transactions can be replaced, submitting early strictly disadvantages the sender. This is the direct CKB analog of the Hermez "no incentive to bid early" finding.

---

### Likelihood Explanation

**Medium-High.** The attacker needs only:
- Access to the public `get_transaction` / `get_raw_tx_pool` RPC (no privileged role).
- Funds sufficient to pay `sum(replaced_fees) + min_rbf_rate * tx_size` — a tiny increment over the victim's fee.
- Timing: submit the replacement within the proposal window (up to ~80–480 seconds on mainnet at 8–48 s/block).

No hashpower, no key material, no social engineering is required. The attack is executable by any unprivileged tx-pool submitter.

---

### Recommendation

**Short term**: Re-introduce a status guard in `check_rbf` (`tx-pool/src/pool.rs`) that rejects replacement attempts when the conflicting transaction is already in `Gap` or `Proposed` status. This restores the original Rule #6 semantics, protects proposer rewards, and removes the last-minute replacement advantage.

**Long term**: Explore time-weighted fee incentives (e.g., a small fee discount for transactions that have been in the pool longer) to reward early submission. Stay current with research on mempool policy and fee-market design for two-phase commit systems.

---

### Proof of Concept

Confirmed by the existing integration test `RbfReplaceProposedSuccess` in `test/src/specs/tx_pool/replace.rs`:

1. Submit a chain of transactions; mine until they reach `Proposed` status.
2. Submit a replacement transaction with a higher fee via `send_transaction`.
3. The original transaction transitions to `Rejected` (`RBFRejected`); the replacement is committed.
4. The proposing miner's reward for the original transaction is zero (never committed).

The `find_conflict_tx` function confirms no status filter exists: [1](#0-0) 

The `check_rbf` function confirms no status check among its rules: [2](#0-1) 

Rule #6 removal is explicitly documented: [3](#0-2) [4](#0-3) 

The proposer reward is only paid on commitment (never on replacement): [5](#0-4) 

The two-phase commit window parameters (mainnet `w_close=2`, `w_far=10`): [6](#0-5)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L294-298)
```rust
    pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
        tx.input_pts_iter()
            .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
            .collect()
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

**File:** test/src/specs/tx_pool/replace.rs (L641-645)
```rust
pub struct RbfRejectReplaceProposed;

// RBF Rule #6
// We removed rule #6, even tx in `Gap` and `Proposed` status can be replaced.
impl Spec for RbfRejectReplaceProposed {
```

**File:** test/src/specs/tx_pool/replace.rs (L752-756)
```rust
pub struct RbfReplaceProposedSuccess;

// RBF Rule #6
// We removed rule #6, this spec testing that we can replace tx in `Gap` and `Proposed` successfully.
impl Spec for RbfReplaceProposedSuccess {
```

**File:** util/reward-calculator/src/lib.rs (L158-235)
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
```

**File:** spec/src/consensus.rs (L47-48)
```rust
/// Default transaction proposal window.
pub const TX_PROPOSAL_WINDOW: ProposalWindow = ProposalWindow(2, 10);
```
