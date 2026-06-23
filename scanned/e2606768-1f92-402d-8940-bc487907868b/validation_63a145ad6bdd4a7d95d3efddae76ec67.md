### Title
Proposal Reward Under-Calculation: Target Block's Own Proposals Incorrectly Excluded at Farthest Proposal Window Boundary — (`util/reward-calculator/src/lib.rs`)

---

### Summary

In `proposal_reward()`, when iterating backwards through commit blocks, the code adds the **target block's own proposals** to the `proposed` exclusion set when `competing_proposal_start == target.number()`. This causes the target block to receive **zero proposal reward** for transactions committed exactly `proposal_window.farthest()` blocks after the target, even when the target was the earliest (and only possible) proposer. The existing unit test does not exercise this code path because it uses a non-real-world parent-target distance.

---

### Finding Description

`proposal_reward()` builds a `proposed` HashSet to track proposals from blocks that could have proposed the same transaction *before* the target block. For each commit block `index` in the loop:

```rust
let competing_proposal_start =
    cmp::max(index.number().saturating_sub(proposal_window.farthest()), 1);

let previous_ids = store
    .get_block_hash(competing_proposal_start)
    .map(|hash| self.get_proposal_ids_by_hash(&hash))
    .expect("finalize target exist");

proposed.extend(previous_ids);
``` [1](#0-0) 

When `index.number() = target.number() + proposal_window.farthest()`, the expression `competing_proposal_start = target.number()`. The call to `get_proposal_ids_by_hash` at that hash returns the **target block's own proposals** (including uncle proposals), which are then inserted into `proposed`. [2](#0-1) 

Subsequently, for transactions committed in block `target.number() + farthest` that were proposed by the target:

```rust
if target_proposals.remove(&id) && !proposed.contains(&id) {
    reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
}
``` [3](#0-2) 

`!proposed.contains(&id)` evaluates to `false` (the target's proposals are now in `proposed`), so no reward is added. The target block is incorrectly treated as a "competing proposer" against itself.

This is wrong: for a transaction committed in block `target.number() + farthest`, the earliest possible proposer is `target.number() + farthest − farthest = target.number()` — the target itself. The target IS the earliest possible proposer, but the code denies it the reward.

**Real-world scenario on mainnet** (`ProposalWindow(2, 10)

### Citations

**File:** util/reward-calculator/src/lib.rs (L244-252)
```rust
            let competing_proposal_start =
                cmp::max(index.number().saturating_sub(proposal_window.farthest()), 1);

            let previous_ids = store
                .get_block_hash(competing_proposal_start)
                .map(|hash| self.get_proposal_ids_by_hash(&hash))
                .expect("finalize target exist");

            proposed.extend(previous_ids);
```

**File:** util/reward-calculator/src/lib.rs (L265-267)
```rust
                    if target_proposals.remove(&id) && !proposed.contains(&id) {
                        reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
                    }
```

**File:** util/reward-calculator/src/lib.rs (L283-294)
```rust
    fn get_proposal_ids_by_hash(&self, hash: &Byte32) -> HashSet<ProposalShortId> {
        let mut ids_set = HashSet::new();
        if let Some(ids) = self.store.get_block_proposal_txs_ids(hash) {
            ids_set.extend(ids)
        }
        if let Some(us) = self.store.get_block_uncles(hash) {
            for u in us.data().into_iter() {
                ids_set.extend(u.proposals());
            }
        }
        ids_set
    }
```
