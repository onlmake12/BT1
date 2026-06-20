Now I have all the key code. Let me verify the exact logic in both verifiers and the `MerkleRootVerifier` to complete the analysis.

### Title
Two-Phase Commit Invariant Bypass via ProposalShortId Collision in `TwoPhaseCommitVerifier` — (`verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

`TwoPhaseCommitVerifier` deduplicates committed transactions by `ProposalShortId` (10-byte hash prefix), while `DuplicateVerifier` deduplicates by full 32-byte tx hash. A miner who finds two valid transactions sharing the same 10-byte Blake2b prefix can include both in a block's commitment zone with only one proposal, causing `TwoPhaseCommitVerifier` to silently accept the block despite one transaction having no corresponding proposal.

---

### Finding Description

**`ProposalShortId` is the first 10 bytes of the tx hash:**

`util/gen-types/src/extension/shortcut.rs`, lines 29–33:
```rust
pub fn from_tx_hash(h: &packed::Byte32) -> Self {
    let mut inner = [0u8; 10];
    inner.copy_from_slice(&h.as_slice()[..10]);
    inner.into()
}
``` [1](#0-0) 

**`DuplicateVerifier` deduplicates by full tx hash, not short ID:**

`verification/src/block_verifier.rs`, line 172:
```rust
if !block.transactions().iter().all(|tx| seen.insert(tx.hash())) {
``` [2](#0-1) 

Two transactions T1 and T2 with different full hashes but identical first 10 bytes both pass this check.

**`TwoPhaseCommitVerifier` builds `committed_ids` as `HashSet<ProposalShortId>`:**

`verification/contextual/src/contextual_block_verifier.rs`, lines 187–195:
```rust
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
``` [3](#0-2) 

When T1 and T2 share a `ProposalShortId`, the `HashSet` collapses them to a single entry. The `difference` check then sees only one short ID, which was proposed, and returns `Ok(())` — even though two transactions were committed under one proposal slot.

**The tx-pool collision guard does not protect block verification:**

`tx-pool/src/util.rs`, lines 20–26 shows `check_txid_collision` rejects a second transaction with the same `ProposalShortId` from entering the pool. However, a miner crafts the block directly and bypasses the tx-pool entirely. Block verification has no equivalent guard. [4](#0-3) 

---

### Impact Explanation

One transaction per block can be committed without a corresponding proposal, bypassing the two-phase commit invariant. The bypassed transaction still must be a fully valid CKB transaction (valid scripts, capacity, inputs), so it cannot be used to commit an otherwise-invalid transaction. The impact is protocol-level: the proposal mechanism — which exists to give the network advance notice before commitment — is circumvented for one transaction. All nodes run the same verification code and would accept the block, so this is not a consensus split but a silent invariant violation.

---

### Likelihood Explanation

The attacker must:
1. **Be a miner** — a standard, unprivileged role in CKB.
2. **Find two valid transactions sharing the same 10-byte Blake2b prefix** — a birthday attack over an 80-bit space requires ~2^40 hash evaluations. At GPU speeds (10^10+ Blake2b hashes/sec), this is achievable in under two minutes of offline computation. The attacker has significant freedom to vary transaction content (output capacity values, output data, `since` fields) to generate many valid candidate transactions.

The collision search is a one-time offline computation. Once a colliding pair is found, the attack is deterministic and repeatable.

---

### Recommendation

Replace the `HashSet<ProposalShortId>` deduplication in `TwoPhaseCommitVerifier` with a count-based check. Instead of collecting short IDs into a set (which silently drops duplicates), count the number of committed non-cellbase transactions and verify that the number of **distinct** short IDs in `committed_ids` equals that count before checking against `proposal_txs_ids`. Alternatively, reject any block at the `DuplicateVerifier` level that contains two transactions with the same `ProposalShortId` (not just the same full hash), making the two verifiers consistent.

---

### Proof of Concept

1. Offline: run a birthday search over valid CKB transactions (varying output capacity) to find T1 and T2 such that `blake2b(raw(T1))[..10] == blake2b(raw(T2))[..10]`.
2. In block N, include `ProposalShortId(T1)` (= `ProposalShortId(T2)`) in the proposals zone.
3. In block N+k (within the proposal window), build a block with both T1 and T2 in the commitment zone.
4. Submit the block. `DuplicateVerifier` passes (different full hashes). `TwoPhaseCommitVerifier` builds `committed_ids = {shared_short_id}` (one entry for two transactions), finds it in `proposal_txs_ids`, and returns `Ok(())`.
5. T2 is committed on-chain with no individual proposal — the two-phase commit invariant is violated.

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L29-33)
```rust
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L187-195)
```rust
        let committed_ids: HashSet<_> = self
            .block
            .transactions()
            .iter()
            .skip(1)
            .map(TransactionView::proposal_short_id)
            .collect();

        if committed_ids.difference(&proposal_txs_ids).next().is_some() {
```

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```
