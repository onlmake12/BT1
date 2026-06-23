### Title
Truncated 80-bit `ProposalShortId` Used as Sole Commitment Identifier in Two-Phase Consensus Verification - (File: `util/gen-types/src/extension/shortcut.rs`)

---

### Summary

CKB's two-phase transaction confirmation protocol uses `ProposalShortId`, a truncated 80-bit (10-byte) prefix of the full 256-bit Blake2b transaction hash, as the sole identifier linking a proposed transaction to a committed transaction. The `TwoPhaseCommitVerifier` checks that every committed transaction's `ProposalShortId` appears in the proposal set — but it never verifies the full transaction hash. An attacker who can craft a transaction whose first 10 bytes of Blake2b hash collide with a legitimately proposed transaction's short ID can commit that substitute transaction in place of the original, bypassing the two-phase commitment check at the consensus layer.

---

### Finding Description

`ProposalShortId` is defined as exactly 10 bytes (80 bits), constructed by taking the first 10 bytes of the full 32-byte Blake2b transaction hash:

```rust
// util/gen-types/src/extension/shortcut.rs, lines 29-33
pub fn from_tx_hash(h: &packed::Byte32) -> Self {
    let mut inner = [0u8; 10];
    inner.copy_from_slice(&h.as_slice()[..10]);
    inner.into()
}
``` [1](#0-0) 

The schema confirms this is a fixed 10-byte array:

```
array ProposalShortId [byte; 10];
``` [2](#0-1) 

The `TwoPhaseCommitVerifier::verify()` enforces that every committed (non-cellbase) transaction's `ProposalShortId` must appear in the set of proposal IDs collected from ancestor blocks within the proposal window:

```rust
// verification/contextual/src/contextual_block_verifier.rs, lines 187-195
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

The check compares only `ProposalShortId` values (80-bit truncated hashes) — it never compares the full 256-bit transaction hash of the committed transaction against the full hash of the originally proposed transaction. The proposal zone stores only `ProposalShortId` values, not full hashes:

```rust
// util/proposal-table/src/lib.rs, lines 16-17
pub(crate) gap: HashSet<ProposalShortId>,
pub(crate) set: HashSet<ProposalShortId>,
``` [4](#0-3) 

The codebase itself acknowledges that short ID collisions are possible and expected in the compact block relay path:

```rust
// sync/src/relayer/mod.rs, lines 352-353
// keeping in mind that short_ids are expected to occasionally collide,
// and that nodes must not be penalized for such collisions, wherever they appear.
``` [5](#0-4) 

The test suite explicitly demonstrates that two different transactions can share the same `ProposalShortId`:

```rust
// sync/src/relayer/tests/compact_block_process.rs, lines 596-597
assert_eq!(missing_tx.proposal_short_id(), fake_tx.proposal_short_id());
assert_ne!(missing_tx.hash(), fake_tx.hash());
``` [6](#0-5) 

---

### Impact Explanation

The `TwoPhaseCommitVerifier` is the consensus-layer gate that enforces CKB's two-phase transaction confirmation rule: a transaction may only be committed if it was previously proposed. Because the check uses only the 80-bit `ProposalShortId` and not the full transaction hash, an attacker who finds (or crafts) a transaction `tx_evil` whose `ProposalShortId` collides with a legitimately proposed `tx_legit` can include `tx_evil` in a block's commitment zone and pass consensus verification — even though `tx_evil` was never proposed.

This means:
- A miner or block submitter can commit an **unproposed transaction** by exploiting an 80-bit collision.
- The committed transaction bypasses the two-phase window entirely, violating a core CKB consensus invariant.
- Since `tx_evil` and `tx_legit` are different transactions (different inputs/outputs/scripts), this can be used to commit arbitrary transactions that were never subject to the proposal-window delay, potentially enabling double-spends or unauthorized cell consumption. [7](#0-6) 

---

### Likelihood Explanation

80 bits of security against a birthday collision requires approximately 2^40 (~1 trillion) hash computations to find a collision with 50% probability. This is within reach of a well-resourced attacker using GPU/ASIC hardware (comparable to the effort already expended by miners). A targeted attacker who controls what transaction they propose (choosing `tx_legit` to have a convenient short ID) can reduce the search space further. The CKB codebase itself acknowledges short ID collisions as a known, expected occurrence in the relay path, confirming the collision space is reachable in practice. [1](#0-0) 

---

### Recommendation

The `TwoPhaseCommitVerifier` should store and compare full 256-bit transaction hashes in the proposal set, not just 80-bit `ProposalShortId` values. Specifically:

1. When a block is stored, persist the full transaction hashes of proposed transactions (not just their `ProposalShortId`).
2. In `TwoPhaseCommitVerifier::verify()`, compare the full hash of each committed transaction against the full hashes of proposed transactions within the window.

Alternatively, if the 10-byte short ID must be retained for bandwidth reasons in the proposal zone, add a secondary check that verifies the committed transaction's full hash matches the full hash of the transaction that was originally proposed under that short ID.

---

### Proof of Concept

1. Attacker observes (or causes) a legitimate transaction `tx_legit` to be proposed in block N. Its `ProposalShortId` = first 10 bytes of `blake2b(tx_legit)`.
2. Attacker searches for a crafted transaction `tx_evil` (different inputs/outputs) such that `blake2b(tx_evil)[..10] == blake2b(tx_legit)[..10]`. With 80-bit output, ~2^40 hashes are needed on average.
3. Attacker mines a block in the commit window (blocks N+2 through N+10) that includes `tx_evil` in the transaction list (not `tx_legit`).
4. `TwoPhaseCommitVerifier::verify()` computes `tx_evil.proposal_short_id()` = first 10 bytes of `blake2b(tx_evil)` = same as `tx_legit`'s short ID, finds it in `proposal_txs_ids`, and returns `Ok(())`.
5. `tx_evil` is committed on-chain despite never having been proposed, violating the two-phase confirmation rule. [8](#0-7) [9](#0-8)

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L27-33)
```rust
impl packed::ProposalShortId {
    /// Creates a new `ProposalShortId` from a transaction hash.
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
    }
```

**File:** util/gen-types/schemas/blockchain.mol (L21-21)
```text
array ProposalShortId [byte; 10];
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L146-214)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if self.block.is_genesis() {
            return Ok(());
        }
        let block_number = self.block.header().number();
        let proposal_window = self.context.consensus.tx_proposal_window();
        let proposal_start = block_number.saturating_sub(proposal_window.farthest());
        let mut proposal_end = block_number.saturating_sub(proposal_window.closest());

        let mut block_hash = self
            .context
            .store
            .get_block_hash(proposal_end)
            .ok_or(CommitError::AncestorNotFound)?;

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
        Ok(())
    }
```

**File:** util/proposal-table/src/lib.rs (L16-17)
```rust
    pub(crate) gap: HashSet<ProposalShortId>,
    pub(crate) set: HashSet<ProposalShortId>,
```

**File:** sync/src/relayer/mod.rs (L352-353)
```rust
    // keeping in mind that short_ids are expected to occasionally collide,
    // and that nodes must not be penalized for such collisions, wherever they appear.
```

**File:** sync/src/relayer/tests/compact_block_process.rs (L596-597)
```rust
    assert_eq!(missing_tx.proposal_short_id(), fake_tx.proposal_short_id());
    assert_ne!(missing_tx.hash(), fake_tx.hash());
```
