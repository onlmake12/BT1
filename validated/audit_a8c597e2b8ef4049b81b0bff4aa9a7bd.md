### Title
Off-by-One Boundary Exclusion in Proposal Reward Loop Skips Earliest Valid Commit Block — (`util/reward-calculator/src/lib.rs`)

---

### Summary

The `proposal_reward` function in `util/reward-calculator/src/lib.rs` uses a strict `>` comparison in its loop termination condition, causing the block at exactly the earliest valid commit height (`target.number() + w_close`) to be silently excluded from the proposal reward calculation. When a transaction is committed at that boundary block, the proposer of the target block receives zero reward for it instead of the protocol-mandated 40% of the transaction fee.

---

### Finding Description

The CKB two-step transaction confirmation protocol (RFC-0020) defines the valid commit window for a transaction proposed at height `h_p` as:

```
w_close ≤ h_c − h_p ≤ w_far
```

This is an **inclusive** range on both ends. The earliest valid commit height is `h_p + w_close` (inclusive).

In `proposal_reward`, the code computes `competing_commit_start` as the earliest block that could have committed a transaction proposed by the target block:

```rust
let closest_start = 1u64
    .checked_add(proposal_window.closest())
    .ok_or(CapacityError::Overflow)?;

// Transaction can be committed at height H(c): H(c) > H(w_close)
let competing_commit_start = cmp::max(
    block_number.saturating_sub(proposal_window.length()),
    closest_start,
);
```

Since `block_number = target.number() + finalization_delay_length` and `finalization_delay_length = w_far + 1`, this resolves to:

```
competing_commit_start = target.number() + w_close
```

The loop then iterates backwards over commit blocks:

```rust
while index.number() > competing_commit_start && !target_proposals.is_empty() {
    index = store
        .get_block_header(&index.data().raw().parent_hash())
        .expect("header stored");
    // ... process index ...
}
```

Because the condition is `>` (strict), the loop stops when `index.number() == competing_commit_start`. The block at `competing_commit_start = target.number() + w_close` is **never entered into the loop body** and is therefore never processed. Any transaction committed at that block is excluded from the proposal reward calculation.

The RFC requires `>=` (inclusive), but the code implements `>` (exclusive), exactly mirroring the `<=` vs `<` boundary error in M-1. [1](#0-0) 

---

### Impact Explanation

When a transaction is proposed in the target block and committed at exactly `target.number() + w_close` (the earliest valid commit height), the proposer of the target block receives **zero** proposal reward for that transaction instead of the protocol-mandated 40% of the transaction fee. The 40% is simply not included in the finalized block reward — it is not redirected to anyone else, it is lost. The miner of the commit block still receives their 60% share via `txs_fees`, so the total reward paid out is less than the protocol specifies. [2](#0-1) 

---

### Likelihood Explanation

The condition is triggered whenever a transaction is committed at exactly the boundary block `target.number() + w_close`. This is a normal, naturally occurring event — any transaction that enters the mempool and is committed at the first eligible block triggers the bug. No special attacker action is required; the bug fires silently on every such commit. The entry path is fully unprivileged: any transaction sender submitting a transaction that gets proposed and committed at the boundary activates the underpayment. [3](#0-2) 

---

### Recommendation

Change the loop termination condition from strict `>` to `>=` so that the block at `competing_commit_start` is included in the reward scan:

```rust
// Before (incorrect — excludes the boundary block):
while index.number() > competing_commit_start && !target_proposals.is_empty() {

// After (correct — includes the boundary block per RFC w_close ≤ h_c − h_p):
while index.number() >= competing_commit_start && !target_proposals.is_empty() {
```

This aligns the implementation with the RFC-0020 condition `w_close ≤ h_c − h_p ≤ w_far`, which is inclusive on the lower bound. [4](#0-3) 

---

### Proof of Concept

**Setup**: `ProposalWindow(2, 10)` (mainnet default: `closest = 2`, `farthest = 10`). Target block at height 13. Finalization block at height 24 (`13 + 10 + 1`). `competing_commit_start = 13 + 2 = 15`.

**Scenario**:
1. Transaction `tx` is proposed in block 13 (the target block).
2. `tx` is committed in block 15 (height `13 + w_close = 15`), which is the earliest valid commit block per RFC.
3. `proposal_reward` is called with `parent = block 23`, `block_number = 24`.
4. The loop starts at `index = block 23` and iterates backwards while `index.number() > 15`.
5. The loop processes blocks 22, 21, 20, 19, 18, 17, 16 — but **stops before block 15**.
6. Block 15 (where `tx` was committed) is never scanned.
7. The proposer of block 13 receives **zero** proposal reward for `tx`.

The RFC states block 15 is a valid commit block (`15 − 13 = 2 = w_close`), so the proposer should receive 40% of `tx`'s fee. [5](#0-4) [6](#0-5)

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

**File:** util/reward-calculator/src/lib.rs (L188-270)
```rust
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
```

**File:** spec/src/consensus.rs (L120-153)
```rust
/// Two protocol parameters w_close and w_far define the closest
/// and farthest on-chain distance between a transaction's proposal
/// and commitment.
///
/// A non-cellbase transaction is committed at height h_c if all of the following conditions are met:
/// 1) it is proposed at height h_p of the same chain, where w_close <= h_c − h_p <= w_far ;
/// 2) it is in the commitment zone of the main chain block with height h_c ;
///
/// ```text
/// ProposalWindow (2, 10)
///     propose
///        \
///         \
///         13 14 [15 16 17 18 19 20 21 22 23]
///                \_______________________/
///                             \
///                           commit
/// ```
///
impl ProposalWindow {
    /// The w_close parameter
    pub const fn closest(&self) -> BlockNumber {
        self.0
    }

    /// The w_far parameter
    pub const fn farthest(&self) -> BlockNumber {
        self.1
    }

    /// The proposal window length
    pub const fn length(&self) -> BlockNumber {
        self.1 - self.0 + 1
    }
```
