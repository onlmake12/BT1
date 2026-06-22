### Title
Two-Phase Commit Verifier Checks Only 10-Byte Short ID, Not Full Transaction Hash — (`verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

`TwoPhaseCommitVerifier::verify()` enforces CKB's two-step-transaction-confirmation rule by checking that every committed transaction's `ProposalShortId` appears in the proposal set of ancestor blocks. Because `ProposalShortId` is only the **first 10 bytes (80 bits)** of the transaction hash, a miner who finds two transactions sharing the same short ID can propose one and commit the other — bypassing the two-phase commit consensus rule entirely.

---

### Finding Description

`ProposalShortId::from_tx_hash` truncates the 32-byte Blake2b transaction hash to its first 10 bytes:

```rust
// util/gen-types/src/extension/shortcut.rs
pub fn from_tx_hash(h: &packed::Byte32) -> Self {
    let mut inner = [0u8; 10];
    inner.copy_from_slice(&h.as_slice()[..10]);
    inner.into()
}
``` [1](#0-0) 

The `TwoPhaseCommitVerifier::verify()` collects the short IDs of all committed (non-cellbase) transactions and checks that each one appears in the set of short IDs that were proposed in the ancestor window:

```rust
let committed_ids: HashSet<_> = self
    .block
    .transactions()
    .iter()
    .skip(1)
    .map(TransactionView::proposal_short_id)   // ← only 10 bytes
    .collect();

if committed_ids.difference(&proposal_txs_ids).next().is_some() {
    return Err((CommitError::Invalid).into());
}
``` [2](#0-1) 

The check verifies **existence of the short ID in the proposal set**, not that the committed transaction is the **same transaction** that was proposed. This is the direct analog of M-07: the Lens protocol checked only that a handle mapped to a non-zero profile ID, not that it mapped to the *specific* profile ID being operated on.

The proposal set itself is built from `get_block_proposal_txs_ids`, which stores 10-byte short IDs — not full 32-byte hashes — so there is no full-hash identity check anywhere in this path: [3](#0-2) 

The codebase already acknowledges that two different transactions can share the same `ProposalShortId`. The compact-block relay layer explicitly handles this case and returns `CompactBlockMeetsShortIdsCollision`:

```rust
// Fake tx with the same ProposalShortId but different hash with tx3
let fake_tx = tx3.clone().fake_hash(fake_hash);
assert_eq!(tx3.proposal_short_id(), fake_tx.proposal_short_id());
assert_ne!(tx3.hash(), fake_tx.hash());
``` [4](#0-3) 

However, `TwoPhaseCommitVerifier` has no equivalent collision guard.

---

### Impact Explanation

A miner who exploits this can commit a transaction `Tx_B` that was **never proposed** to the network, as long as they previously proposed a different transaction `Tx_A` that shares the same 10-byte short ID. The committed block passes `TwoPhaseCommitVerifier` on all nodes because the short ID check succeeds. The two-phase commit rule — which exists to give the network time to validate and propagate transactions before they are committed — is completely bypassed for `Tx_B`. All honest nodes accept the block, including the unannounced `Tx_B`, because the consensus check only inspects short IDs.

---

### Likelihood Explanation

Finding two transactions with the same `ProposalShortId` is an 80-bit birthday-attack problem, requiring approximately 2^40 hash evaluations. This is within reach of a well-resourced miner today (comparable to GPU-scale precomputation). The attacker must also be a miner (or collude with one) to include the crafted block. The barrier is meaningful but not prohibitive for a motivated adversary targeting a high-value chain.

---

### Recommendation

Store full 32-byte transaction hashes in the proposal zone instead of (or in addition to) 10-byte short IDs, and verify the committed transaction's full hash against the stored proposal hashes in `TwoPhaseCommitVerifier::verify()`. Alternatively, enforce that the `proposal_txs_ids` set stores full hashes and the committed-ID comparison uses full hashes. This mirrors the M-07 fix: check `profileIdByHandleHash != profileIds[i]` rather than `== 0`.

---

### Proof of Concept

1. Miner crafts `Tx_A` and `Tx_B` such that `Tx_A.hash()[..10] == Tx_B.hash()[..10]` (birthday attack, ~2^40 work). Both have valid inputs, outputs, and scripts.
2. Miner mines a block at height `H` that includes `Tx_A.proposal_short_id()` in its proposal zone. `Tx_A` is broadcast to the network; nodes add its short ID to their proposal tables.
3. At height `H + w_close` (within the commit window), the miner mines a block containing `Tx_B` (not `Tx_A`) in the commitment zone.
4. `TwoPhaseCommitVerifier::verify()` computes `Tx_B.proposal_short_id()` — which equals `Tx_A.proposal_short_id()` — finds it in `proposal_txs_ids`, and returns `Ok(())`.
5. All nodes accept the block. `Tx_B` is committed on-chain despite never having been proposed or broadcast during the proposal window, violating the two-step-transaction-confirmation protocol defined in CKB RFC 0020.

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L29-33)
```rust
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L161-185)
```rust
        let mut proposal_txs_ids = HashSet::new();

        while proposal_end >= proposal_start {
            let header = self
                .context
                .store
                .get_block_header(&block_hash)
                .ok_or(CommitError::AncestorNotFound)?;
            if header.is_genesis() {
                break;
            }

            if let Some(ids) = self.context.store.get_block_proposal_txs_ids(&block_hash) {
                proposal_txs_ids.extend(ids);
            }
            if let Some(uncles) = self.context.store.get_block_uncles(&block_hash) {
                uncles
                    .data()
                    .into_iter()
                    .for_each(|uncle| proposal_txs_ids.extend(uncle.proposals()));
            }

            block_hash = header.data().raw().parent_hash();
            proposal_end -= 1;
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

**File:** sync/src/relayer/tests/block_transactions_process.rs (L338-342)
```rust
    // Fake tx with the same ProposalShortId but different hash with tx3
    let fake_tx = tx3.clone().fake_hash(fake_hash);

    assert_eq!(tx3.proposal_short_id(), fake_tx.proposal_short_id());
    assert_ne!(tx3.hash(), fake_tx.hash());
```
