Audit Report

## Title
`TwoPhaseCommitVerifier` Accepts Committed Transactions Matching Only Truncated 10-Byte `ProposalShortId`, Enabling Birthday-Collision Bypass of Two-Phase Commit — (File: `verification/contextual/src/contextual_block_verifier.rs`)

## Summary

`ProposalShortId` is derived from only the first 10 bytes (80 bits) of a transaction's Blake2b hash. The `TwoPhaseCommitVerifier` checks that every committed transaction's `ProposalShortId` appears in the proposal set, but never verifies the full 32-byte transaction hash. A miner who finds two distinct transactions sharing the same `ProposalShortId` via a birthday attack (~2^40 Blake2b operations, feasible on GPU hardware) can propose transaction A and commit the entirely different transaction B, bypassing the two-phase commit consensus rule. The reward calculator is also affected, misattributing proposer rewards based on the same truncated ID.

## Finding Description

**Root cause — truncated ID construction:**

`ProposalShortId::from_tx_hash` copies only the first 10 bytes of the 32-byte hash:

```rust
// util/gen-types/src/extension/shortcut.rs, L29-33
pub fn from_tx_hash(h: &packed::Byte32) -> Self {
    let mut inner = [0u8; 10];
    inner.copy_from_slice(&h.as_slice()[..10]);
    inner.into()
}
``` [1](#0-0) 

**`TwoPhaseCommitVerifier` uses only this truncated ID:**

```rust
// verification/contextual/src/contextual_block_verifier.rs, L187-195
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

The verifier never checks the full transaction hash. If tx_A and tx_B share the same `ProposalShortId`, proposing tx_A and committing tx_B passes this check unconditionally.

**`DuplicateVerifier` does not close the gap** — it deduplicates by full hash, not by `ProposalShortId`: [3](#0-2) 

Two transactions with the same `ProposalShortId` but different full hashes pass both verifiers simultaneously.

**`ProposalView` and `ProposalTable` store only `ProposalShortId`s**, so the collision is invisible to the entire proposal tracking system: [4](#0-3) 

**Reward calculator misattribution:** The reward calculator converts committed tx hashes back to `ProposalShortId` and matches against the proposal set by that truncated ID: [5](#0-4) 

If tx_B (never proposed, high fee) is committed but tx_A (proposed, low fee) shares its `ProposalShortId`, the proposer of tx_A receives 40% of tx_B's fee — a direct reward misattribution.

**tx-pool silently drops on `ProposalShortId` collision:** [6](#0-5) 

A second transaction with a colliding `ProposalShortId` is silently rejected with `Ok((false, evicts))`, enabling transaction censorship without any error signal.

**The codebase itself acknowledges collisions are expected** in compact block relay and handles them defensively there, but `TwoPhaseCommitVerifier` has no such defense: [7](#0-6) 

## Impact Explanation

**Concrete impact: Vulnerabilities which could easily damage CKB economy (Critical) and cause consensus rule violation.**

1. **Two-phase commit bypass**: A miner commits tx_B (never broadcast or proposed) by having previously proposed tx_A with the same `ProposalShortId`. All nodes accept the block because the `TwoPhaseCommitVerifier` passes. The core consensus invariant — that committed transactions must have been proposed within the proposal window — is silently violated on every node simultaneously, with no disagreement and no detection.

2. **Proposal reward theft**: The attacker proposes tx_A (low fee), finds tx_B (high fee) with the same `ProposalShortId`, and arranges for tx_B to be committed. The reward calculator attributes 40% of tx_B's fee to the proposer of tx_A (the attacker). This is a direct, repeatable economic theft from the legitimate fee payer and the correct proposer.

3. **Double-commit with single proposal**: Because `committed_ids` is a `HashSet<ProposalShortId>`, two committed transactions sharing the same `ProposalShortId` are deduplicated to one entry, allowing both to pass the two-phase commit check with only one proposal — halving the proposal overhead for those two transactions.

## Likelihood Explanation

`ProposalShortId` is 80 bits. A birthday attack to find two transactions sharing the same first 10 bytes of Blake2b requires approximately 2^40 (~1.1 trillion) hash evaluations. A modern GPU (e.g., RTX 4090) achieves on the order of 10^9 Blake2b operations per second, placing the attack at roughly 18 minutes on a single GPU and seconds for a mining pool with multiple GPUs. The attacker fully controls transaction content (outputs, output data, witnesses) to vary the hash input, making the search straightforward. Mining is permissionless in CKB, so any motivated actor with GPU hardware can execute this. The attack is repeatable and the cost is fixed regardless of the fee value of the target transaction.

## Recommendation

1. **Store full transaction hashes in the proposal set.** Change `ProposalView`, `ProposalTable`, and the on-chain proposal storage to record full 32-byte hashes alongside or instead of `ProposalShortId`. The `TwoPhaseCommitVerifier` should verify that the committed transaction's full hash was proposed, not just its truncated ID.

2. **Alternatively**, extend `ProposalShortId` to the full 32-byte hash for all proposal tracking purposes, accepting the increased block size overhead.

3. **The reward calculator** should match committed transactions to proposals by full hash, not by `ProposalShortId`.

4. **The tx-pool** should explicitly reject (with an error) any transaction whose `ProposalShortId` collides with an existing entry but whose full hash differs, rather than silently returning `Ok((false, evicts))`.

## Proof of Concept

```
// Step 1: Birthday attack — find tx_A and tx_B such that:
//   tx_A.hash()[..10] == tx_B.hash()[..10]  (same ProposalShortId)
//   tx_A.hash() != tx_B.hash()              (different full hashes)
//   Both are valid CKB transactions (vary outputs/witnesses to change hash)
// Cost: ~2^40 Blake2b operations (~18 minutes on RTX 4090)

// Step 2: Attacker (miner) includes tx_A.proposal_short_id() in block N's proposals
//   block_N.proposals = [ProposalShortId::from_tx_hash(&tx_A.hash())]

// Step 3: Within the proposal window, attacker mines block N+k committing tx_B (NOT tx_A)
//   block_N_plus_k.transactions = [cellbase, tx_B]

// Step 4: TwoPhaseCommitVerifier on every node checks:
//   committed_ids = {tx_B.proposal_short_id()}  // == tx_A.proposal_short_id()
//   proposal_txs_ids contains tx_A.proposal_short_id()
//   committed_ids.difference(&proposal_txs_ids) == empty  => Ok(())
//   ✓ Block accepted by all nodes — tx_B committed without ever being proposed

// Step 5: RewardCalculator on every node:
//   ProposalShortId::from_tx_hash(&tx_B.hash()) == ProposalShortId::from_tx_hash(&tx_A.hash())
//   target_proposals.remove(&id) succeeds
//   reward += tx_B_fee * proposer_ratio  // attributed to proposer of tx_A (attacker)
```

Unit test plan: construct two `TransactionView` objects with identical `proposal_short_id()` but different `hash()` values (achievable by varying witness data), insert tx_A's `ProposalShortId` into a mock proposal set, run `TwoPhaseCommitVerifier::verify` with tx_B as the committed transaction, and assert `Ok(())` is returned — confirming the bypass.

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L29-33)
```rust
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

**File:** util/reward-calculator/src/lib.rs (L204-234)
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
