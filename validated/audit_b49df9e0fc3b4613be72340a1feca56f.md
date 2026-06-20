### Title
Miners Receive Full Fee Rebate via Proposal/Commit Split, Enabling Zero-Cost Transaction Ordering Manipulation - (File: `util/reward-calculator/src/lib.rs`)

### Summary
CKB's two-phase proposal/commit fee distribution gives miners who both propose and commit their own transactions a 100% fee rebate (40% proposal reward + 60% commit reward). This is the direct analog of the 0x Protocol vulnerability where market makers receive a portion of the protocol fee they pay, reducing their net cost for front-running. In CKB, any miner can submit transactions with arbitrarily high fees at zero net cost, enabling them to displace regular users' transactions from the mempool and front-run them.

### Finding Description
CKB's fee distribution is split between two roles:

**Commit reward (60%)** — implemented in `txs_fees()`: [1](#0-0) 

**Proposal reward (40%)** — implemented in `proposal_reward()`: [2](#0-1) 

These are documented in the `BlockReward` type: [3](#0-2) 

When the **same miner** both proposes a transaction in block N and commits it in block N+2 through N+10 (the mainnet proposal window), they receive:
- 40% of `tx_fee` as `proposal_reward`
- `tx_fee − 40% of tx_fee` (i.e., 60%) as `txs_fees`
- **Total: 100% of `tx_fee` returned to the miner**

The RBF replacement fee check in `calculate_min_replace_fee` enforces:
```
min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
``` [4](#0-3) 

This check is applied uniformly to all submitters. It does **not** account for the fact that a miner submitting the replacement transaction will recover 100% of the fee they pay. For a regular user, `min_replace_fee` is a real cost. For a miner who controls both the proposal and commit blocks, the net cost is **zero**.

The block assembler selects transactions by fee rate: [5](#0-4) 

A miner can inject their own high-fee transactions, which will be prioritized by the fee-rate selector, displacing regular users' transactions — at zero net cost to the miner.

### Impact Explanation
A miner with any nonzero hashrate can:
1. Submit their own transactions with arbitrarily high fees via `send_transaction` RPC.
2. Propose those transactions in their own block (earning the 40% proposal reward later).
3. Commit those transactions in a subsequent block within the proposal window (earning the 60% commit reward).
4. Recover 100% of the fees paid, making the effective transaction cost zero.

This allows the miner to:
- **Displace regular users' transactions** from the mempool by outbidding them with high fees at zero cost.
- **Front-run regular users' transactions** by inserting their own transactions ahead of pending ones, exploiting the visible mempool.
- **Abuse RBF** to replace other users' transactions at zero net cost, since `calculate_min_replace_fee` does not discount the miner's fee rebate.

Regular users always pay full fees. Miners pay zero net fees for their own transactions. This asymmetry is the root cause.

### Likelihood Explanation
- **Entry path is permissionless**: any party can run a CKB miner node and submit transactions via the standard `send_transaction` RPC.
- **No majority hashrate required**: even a miner with 1% hashrate can exploit this for their own transactions — they simply wait for their own block to propose and commit.
- **Clear economic incentive**: front-running profitable transactions (e.g., DEX arbitrage, DAO withdrawals) is directly profitable, and the cost to the miner is zero.
- **Proposal window is public**: the `tx_proposal_window` (closest=2, farthest=10 on mainnet) is a consensus parameter, making the timing of commit eligibility fully predictable. [6](#0-5) 

### Recommendation
**Short term:** Document that miners have a structural zero-cost advantage over regular users when submitting and ordering their own transactions. Users of applications built on CKB (DEXes, DAO interactions) should be aware that miners can front-run at zero cost.

**Long term:** Consider fee structures that do not return 100% of fees to the same actor who both proposes and commits. For example:
- Burn a portion of the fee when the proposer and committer are the same miner (detectable via the `CellbaseWitness` lock).
- Introduce a fee mechanism that does not depend solely on the miner's own block inclusion, reducing the rebate incentive for self-dealing.

### Proof of Concept
**Setup:** Mainnet consensus, `proposer_reward_ratio = 4/10`, proposal window `[2, 10]`.

1. **Alice** submits transaction `T_alice` with fee `F = 10,000` shannons to the mempool.
2. **Eve** (a miner) submits transaction `T_eve` spending the same or a related cell, with fee `F' = 15,000` shannons (satisfying `calculate_min_replace_fee`).
3. Eve mines block `N`, including `T_eve` in the **proposals** section. `T_alice` is displaced from the mempool via RBF.
4. Eve mines block `N+2`, including `T_eve` in the **transactions** (commit) section.
5. Eve's reward for block `N` (finalized at block `N+11`) includes:
   - `proposal_reward` = `15,000 × 4/10` = `6,000` shannons
   - `txs_fees` = `15,000 − 6,000` = `9,000` shannons
   - **Total returned: 15,000 shannons = 100% of `F'`**
6. Eve's **net cost for front-running Alice: 0 shannons**.

The `calculate_min_replace_fee` check at `tx-pool/src/pool.rs:665` enforced `F' ≥ F + extra_rbf_fee`, treating Eve identically to a regular user — but Eve recovers the entire fee, making the check economically meaningless as a deterrent for miners. [7](#0-6) [8](#0-7)

### Citations

**File:** util/reward-calculator/src/lib.rs (L103-132)
```rust
        let txs_fees = self.txs_fees(target)?;
        let proposal_reward = self.proposal_reward(parent, target)?;
        let (primary, secondary) = self.base_block_reward(target)?;

        let total = txs_fees
            .safe_add(proposal_reward)?
            .safe_add(primary)?
            .safe_add(secondary)?;

        debug!(
            "[RewardCalculator] target {} {}\n
             txs_fees {:?}, proposal_reward {:?}, primary {:?}, secondary: {:?}, total_reward {:?}",
            target.number(),
            target.hash(),
            txs_fees,
            proposal_reward,
            primary,
            secondary,
            total,
        );

        let block_reward = BlockReward {
            total,
            primary,
            secondary,
            tx_fee: txs_fees,
            proposal_reward,
        };

        Ok((target_lock, block_reward))
```

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

**File:** util/reward-calculator/src/lib.rs (L173-234)
```rust
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
```

**File:** util/types/src/core/reward.rs (L32-45)
```rust
    /// the block.
    ///
    /// # Notice
    ///
    /// Miners only get 60% of the transaction fee for each transaction committed in the block.
    pub tx_fee: Capacity,
    /// The transaction fees that are rewarded to miners because the transaction is proposed in the
    /// block or its uncles.
    ///
    /// # Notice
    ///
    /// Miners only get 40% of the transaction fee for each transaction proposed in the block
    /// and committed later in its active commit window.
    pub proposal_reward: Capacity,
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

**File:** tx-pool/src/component/tx_selector.rs (L52-66)
```rust
/// Selects transactions for inclusion in a block-template using **package-aware** fee-rate sorting.
///
/// ### Package definition
/// A package is a connected group of ≤ MAX_ANCESTORS_COUNT（1_000）transactions
/// The mempool is linearly ordered into non-overlapping packages using a greedy clustering
/// algorithm that maximizes total fee for a given size and cycles.
///
/// ### Why packages instead of individual transactions?
/// - A high-fee child transaction is worthless without its low-fee parent(s) (CPFP).
/// - A low-fee parent with many high-fee children should be prioritized as a unit (package).
/// - Sorting individual txs breaks incentive compatibility and leads to suboptimal templates.
///
/// ### Sorting rule
/// Packages are sorted by **package fee rate** = total fee / total weight of the entire package.
///
```
