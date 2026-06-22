### Title
`TwoPhaseCommitVerifier` Validates Committed Transactions Only by Truncated 10-Byte `ProposalShortId`, Enabling Two-Phase Commit Bypass via Birthday Collision — (File: `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

CKB's two-phase commit mechanism identifies transactions using `ProposalShortId`, which is only the **first 10 bytes (80 bits)** of the 32-byte Blake2b transaction hash. The `TwoPhaseCommitVerifier` checks that every committed transaction's `ProposalShortId` appears in the proposal set, but never verifies the **full transaction hash**. An attacker who finds two distinct transactions sharing the same `ProposalShortId` (a birthday attack requiring ~2^40 Blake2b operations, feasible with GPU hardware) can propose one transaction and commit a completely different one, bypassing the two-phase commit consensus rule. The reward calculator is also affected, as it uses `ProposalShortId` to attribute proposal rewards.

---

### Finding Description

**Root cause — truncated ID generation:**

`ProposalShortId` is constructed by taking only the first 10 bytes of the transaction hash:

```rust
// util/gen-types/src/extension/shortcut.rs
pub fn from_tx_hash(h: &packed::Byte32) -> Self {
    let mut inner = [0u8; 10];
    inner.copy_from_slice(&h.as_slice()[..10]);
    inner.into()
}
``` [1](#0-0) 

This 80-bit identifier is then used as the **sole key** in the `TwoPhaseCommitVerifier`:

```rust
// verification/contextual/src/contextual_block_verifier.rs
let committed_ids: HashSet<_> = self
    .block
    .transactions()
    .iter()
    .skip(1)
    .map(TransactionView::proposal_short_id)
    .collect();

if committed_ids.difference(&proposal_txs_ids).next().is_some() {
    return Err((CommitError::Invalid).into());
}
``` [2](#0-1) 

The verifier only checks that the 10-byte `ProposalShortId` of each committed transaction appears in the proposal set. It never checks whether the **full transaction hash** was proposed. Two distinct transactions with the same `ProposalShortId` are indistinguishable to this verifier.

**The `DuplicateVerifier` does not close this gap** — it checks for duplicate full hashes, not duplicate `ProposalShortId`s:

```rust
// verification/src/block_verifier.rs
let mut seen = HashSet::with_capacity(block.transactions().len());
if !block.transactions().iter().all(|tx| seen.insert(tx.hash())) {
    return Err((BlockErrorKind::CommitTransactionDuplicate).into());
}
``` [3](#0-2) 

So two transactions with the same `ProposalShortId` but different full hashes pass both verifiers simultaneously.

**The `ProposalView` and `ProposalTable` store only `ProposalShortId`s** (not full hashes), so the collision is invisible to the entire proposal tracking system: [4](#0-3) 

**The reward calculator is also affected.** It uses `ProposalShortId` to match proposals to committed transactions and attribute the 40% proposer reward:

```rust
// util/reward-calculator/src/lib.rs
let committed_idx_proc = |hash: &Byte32| -> Vec<ProposalShortId> {
    store
        .get_block_txs_hashes(hash)
        .into_iter()
        .skip(1)
        .map(|tx_hash| ProposalShortId::from_tx_hash(&tx_hash))
        .collect()
};
// ...
if target_proposals.remove(&id) {
    reward = reward.safe_add(tx_fee.safe_mul_ratio(proposer_ratio)?)?;
}
``` [5](#0-4) 

If transaction B (never proposed) is committed but shares a `ProposalShortId` with proposed transaction A, the proposer of A receives the proposal reward for B's fee — a direct reward misattribution.

**The tx-pool also uses `ProposalShortId` as a `hashed_unique` key**, meaning a collision silently drops one transaction:

```rust
// tx-pool/src/component/pool_map.rs
if self.entries.get_by_id(&tx_short_id).is_some() {
    return Ok((false, evicts));
}
``` [6](#0-5) 

---

### Impact Explanation

1. **Two-phase commit consensus bypass**: A miner can commit transaction B (which was never broadcast or proposed) by having previously proposed any transaction A with the same `ProposalShortId`. The `TwoPhaseCommitVerifier` accepts the block. This violates the core consensus invariant that committed transactions must have been proposed within the proposal window.

2. **Proposal reward theft**: The attacker proposes transaction A (low fee), finds transaction B with the same `ProposalShortId` (high fee), and arranges for B to be committed. The reward calculator attributes 40% of B's fee to the proposer of A (the attacker), not to any legitimate proposer of B.

3. **Double-commit with single proposal**: Because `committed_ids` is a `HashSet<ProposalShortId>`, two committed transactions sharing the same `ProposalShortId` are deduplicated to a single entry. Both pass the two-phase commit check with only one proposal, halving the proposal overhead for those two transactions.

4. **Silent tx-pool eviction**: If the attacker submits transaction A to the pool before a victim submits B (same `ProposalShortId`), B is silently dropped with `Ok((false, evicts))`, causing transaction censorship without any error signal.

---

### Likelihood Explanation

`ProposalShortId` is 80 bits. A birthday attack to find two transactions with the same first 10 bytes of Blake2b hash requires approximately **2^40 hash evaluations** (~1.1 trillion operations). A modern GPU (e.g., RTX 4090) can compute ~10^9 Blake2b hashes per second, making this achievable in roughly **18 minutes**. A mining pool with multiple GPUs could do this in seconds. The attacker fully controls transaction content (outputs, output data, witnesses) to vary the hash, making the search straightforward. This is within reach of any miner or MEV-capable actor.

The codebase itself acknowledges that short ID collisions are expected:

```
// sync/src/relayer/block_transactions_process.rs
// Keeping in mind that short_ids are expected to occasionally collide.
``` [7](#0-6) 

The compact block relay handles collisions defensively, but the `TwoPhaseCommitVerifier` and reward calculator do not.

---

### Recommendation

1. **Store full transaction hashes in the proposal set**, not just `ProposalShortId`s. The `TwoPhaseCommitVerifier` should verify that the committed transaction's full hash was proposed, not just its truncated ID.

2. **Alternatively**, extend `ProposalShortId` to the full 32-byte hash for proposal tracking purposes, accepting the increased block size overhead.

3. **The reward calculator** should match committed transactions to proposals by full hash, not by `ProposalShortId`.

4. **The tx-pool** should check for `ProposalShortId` collisions and reject (with an explicit error) rather than silently dropping the second transaction.

---

### Proof of Concept

```
// Step 1: Birthday attack — find tx_A and tx_B such that:
//   tx_A.hash()[..10] == tx_B.hash()[..10]  (same ProposalShortId)
//   tx_A.hash() != tx_B.hash()              (different full hashes)
// Cost: ~2^40 Blake2b operations (~18 minutes on a single GPU)

// Step 2: Attacker (miner) includes tx_A.proposal_short_id() in block N's proposals
//   block_N.proposals = [ProposalShortId::from_tx_hash(&tx_A.hash())]

// Step 3: Within the proposal window, attacker commits tx_B (NOT tx_A) in block N+k
//   block_N_plus_k.transactions = [cellbase, tx_B]

// Step 4: TwoPhaseCommitVerifier checks:
//   committed_ids = {tx_B.proposal_short_id()}  // == tx_A.proposal_short_id()
//   proposal_txs_ids contains tx_A.proposal_short_id()
//   committed_ids.difference(&proposal_txs_ids) == empty  => Ok(())
//   ✓ Block accepted — tx_B committed without ever being proposed

// Step 5: RewardCalculator attributes 40% of tx_B's fee to the proposer of tx_A
//   (the attacker), regardless of whether tx_B was ever proposed
```

The attack entry point is the block submission path (`blocking_process_block` → `ContextualBlockVerifier::verify` → `TwoPhaseCommitVerifier::verify`), reachable by any miner submitting a block to the network. [8](#0-7)

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L28-33)
```rust
    /// Creates a new `ProposalShortId` from a transaction hash.
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L187-212)
```rust
        let committed_ids: HashSet<_> = self
            .block
            .transactions()
            .iter()
            .skip(1)
            .map(TransactionView::proposal_short_id)
            .collect();

        if committed_ids.difference(&proposal_txs_ids).next().is_some() {
            error_target!(
                crate::LOG_TARGET,
                "BlockView {} {}",
                self.block.number(),
                self.block.hash()
            );
            error_target!(crate::LOG_TARGET, "proposal_window {:?}", proposal_window);
            error_target!(crate::LOG_TARGET, "Committed Ids:");
            for committed_id in committed_ids.iter() {
                error_target!(crate::LOG_TARGET, "    {:?}", committed_id);
            }
            error_target!(crate::LOG_TARGET, "Proposal Txs Ids:");
            for proposal_txs_id in proposal_txs_ids.iter() {
                error_target!(crate::LOG_TARGET, "    {:?}", proposal_txs_id);
            }
            return Err((CommitError::Invalid).into());
        }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L666-668)
```rust
        if !self.switch.disable_two_phase_commit() {
            TwoPhaseCommitVerifier::new(&self.context, block).verify()?;
        }
```

**File:** verification/src/block_verifier.rs (L170-174)
```rust
    pub fn verify(&self, block: &BlockView) -> Result<(), Error> {
        let mut seen = HashSet::with_capacity(block.transactions().len());
        if !block.transactions().iter().all(|tx| seen.insert(tx.hash())) {
            return Err((BlockErrorKind::CommitTransactionDuplicate).into());
        }
```

**File:** util/proposal-table/src/lib.rs (L15-18)
```rust
pub struct ProposalView {
    pub(crate) gap: HashSet<ProposalShortId>,
    pub(crate) set: HashSet<ProposalShortId>,
}
```

**File:** util/reward-calculator/src/lib.rs (L204-235)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L207-208)
```rust
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
```

**File:** sync/src/relayer/block_transactions_process.rs (L12-22)
```rust
// Keeping in mind that short_ids are expected to occasionally collide.
// On receiving block-transactions message,
// while the reconstructed the block has a different transactions_root,
// 1. If the BlockTransactions includes all the transactions matched short_ids in the compact block,
// In this situation, the peer sends all the transactions by either prefilled or block-transactions,
// no one transaction from the tx-pool or store,
// the node should ban the peer but not mark the block invalid
// because of the block hash may be wrong.
// 2. If not all the transactions comes from the peer,
// there may be short_id collision in transaction pool.
// the node retreat to request all the short_ids from the peer.
```
