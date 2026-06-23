### Title
`TwoPhaseCommitVerifier` Matches Only Truncated `ProposalShortId` (Excludes Witnesses), Allowing Witness Substitution at Commit Time — (`verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

CKB's two-phase transaction confirmation is supposed to give observers a window to verify what will be committed. The `TwoPhaseCommitVerifier` enforces this by checking that every committed transaction was previously proposed. However, the proposal identifier (`ProposalShortId`) is derived from only the first 10 bytes of the `RawTransaction` hash — which itself excludes witnesses entirely. A miner can propose a transaction with one set of witnesses and commit the same `RawTransaction` with entirely different witnesses. The verifier accepts this substitution because the short IDs match. This is the direct structural analog to the diamond report: a critical field (witnesses / `_calldata`) is excluded from the commitment identifier, allowing the proposer to substitute execution data at commit time.

---

### Finding Description

**Root cause 1 — `ProposalShortId` is only 10 bytes of `RawTransaction` hash, witnesses excluded:**

`ProposalShortId::from_tx_hash` takes only the first 10 bytes of the 32-byte tx hash:

```rust
// util/gen-types/src/extension/shortcut.rs
pub fn from_tx_hash(h: &packed::Byte32) -> Self {
    let mut inner = [0u8; 10];
    inner.copy_from_slice(&h.as_slice()[..10]);  // only first 10 bytes
    inner.into()
}
``` [1](#0-0) 

`calc_tx_hash` hashes only `RawTransaction`, which does not include the `witnesses` field:

```rust
// util/gen-types/src/extension/calc_hash.rs
pub fn calc_tx_hash(&self) -> packed::Byte32 {
    self.raw().calc_tx_hash()   // hashes RawTransaction only, not witnesses
}
``` [2](#0-1) 

The molecule schema confirms `Transaction` has a separate `witnesses` field outside `raw`:

```
table Transaction {
    raw:       RawTransaction,
    witnesses: BytesVec,        // excluded from tx_hash / ProposalShortId
}
``` [3](#0-2) 

**Root cause 2 — `TwoPhaseCommitVerifier` only checks `ProposalShortId`, not full transaction identity:**

```rust
// verification/contextual/src/contextual_block_verifier.rs
let committed_ids: HashSet<_> = self
    .block.transactions().iter().skip(1)
    .map(TransactionView::proposal_short_id)   // only 10-byte short ID
    .collect();

if committed_ids.difference(&proposal_txs_ids).next().is_some() {
    return Err((CommitError::Invalid).into());
}
``` [4](#0-3) 

The verifier never checks the full `tx_hash` or `witness_hash` of the committed transaction against what was proposed. It only checks that the 10-byte short ID appears in the set of previously proposed short IDs.

**The substitution attack:**

Two transactions `tx_A` and `tx_B` with identical `RawTransaction` but different `witnesses` have:
- Identical `tx_hash` (since `tx_hash = hash(RawTransaction)`)
- Identical `ProposalShortId`
- Different `witness_hash`

This is explicitly confirmed in the codebase:

```rust
// test/src/specs/tx_pool/collision.rs
let tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build();
assert_eq!(tx1.hash(), tx2.hash());
assert_eq!(tx1.proposal_short_id(), tx2.proposal_short_id());
assert_ne!(tx1.witness_hash(), tx2.witness_hash());
``` [5](#0-4) 

A miner can:
1. Propose `ProposalShortId(tx_A)` — observers see the proposal and inspect `tx_A`'s structure
2. At commit time, include `tx_B` (same `RawTransaction`, different `witnesses`) in the block
3. `TwoPhaseCommitVerifier` passes because `ProposalShortId(tx_B) == ProposalShortId(tx_A)`
4. The block's `transactions_root` commits to `tx_B`'s witness hash, not `tx_A`'s

No collision search is required. The substitution is zero-cost.

Additionally, the 10-byte (80-bit) truncation creates a birthday-attack surface: a miner can find two structurally different transactions (different inputs/outputs) sharing the same `ProposalShortId` with approximately 2^40 hash operations — feasible for a mining operation — and propose one while committing the other.

---

### Impact Explanation

The two-phase commit mechanism's core security property is that observers can inspect proposed transactions during the proposal window and react before they are committed. By substituting witnesses at commit time, a miner undermines this guarantee:

- **Witness data is authorization data** (signatures, lock script arguments). A miner who controls a lock script (e.g., a multisig where the miner is one signer, or a custom script) can propose a transaction with witnesses that appear valid to observers, then commit the same `RawTransaction` with different witnesses that authorize a different execution path.
- For transactions using non-standard or always-success lock scripts (common in DeFi-style CKB scripts), the miner can commit with entirely different witness content, changing the observable behavior of the transaction without any on-chain evidence visible during the proposal window.
- The `transactions_root` in the committed block header will reflect the substituted `witness_hash`, but by then the block is already accepted.

---

### Likelihood Explanation

The witness-substitution variant requires no computational work — any miner can do it for any block they produce. The attacker-controlled entry path is: a miner submits a block via the standard block submission path (`process_block`). The `TwoPhaseCommitVerifier` is the only check enforcing the proposal-commit link, and it does not cover witnesses. The miner is an unprivileged network participant (not a trusted role); the two-phase commit mechanism is specifically designed to constrain miner behavior, making this a relevant threat model.

---

### Recommendation

The `TwoPhaseCommitVerifier` should verify that each committed transaction's full `tx_hash` (or `witness_hash`) matches the transaction that was originally proposed, not just the truncated `ProposalShortId`. This requires storing the full tx hash at proposal time rather than only the 10-byte short ID. Alternatively, the `ProposalShortId` derivation should include the witness hash so that witness substitution changes the short ID and is caught by the verifier.

---

### Proof of Concept

1. Miner constructs `tx_A` with `RawTransaction R` and witnesses `W1`. Computes `ProposalShortId = first_10_bytes(blake2b(R))`.
2. Miner includes `ProposalShortId` in a block's proposals list. Observers inspect `tx_A` during the proposal window.
3. Miner constructs `tx_B` with the same `RawTransaction R` but witnesses `W2 ≠ W1`. Confirms `tx_B.proposal_short_id() == tx_A.proposal_short_id()` (as demonstrated in `test/src/specs/tx_pool/collision.rs` lines 217–221).
4. Miner includes `tx_B` (not `tx_A`) in the commit block's transactions list.
5. `TwoPhaseCommitVerifier::verify()` at `verification/contextual/src/contextual_block_verifier.rs` lines 187–212 computes `committed_ids` from `proposal_short_id()` of committed transactions, finds `ProposalShortId(tx_B)` in `proposal_txs_ids`, and returns `Ok(())`.
6. The block is accepted with `tx_B`'s witnesses — different from what observers saw during the proposal window — with no verification failure.

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L29-32)
```rust
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
```

**File:** util/gen-types/src/extension/calc_hash.rs (L140-142)
```rust
    pub fn calc_tx_hash(&self) -> packed::Byte32 {
        self.raw().calc_tx_hash()
    }
```

**File:** util/gen-types/schemas/blockchain.mol (L66-69)
```text
table Transaction {
    raw:            RawTransaction,
    witnesses:      BytesVec,
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

**File:** test/src/specs/tx_pool/collision.rs (L217-221)
```rust
    let tx1 = node.new_transaction_spend_tip_cellbase();
    let tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build();
    assert_eq!(tx1.hash(), tx2.hash());
    assert_eq!(tx1.proposal_short_id(), tx2.proposal_short_id());
    assert_ne!(tx1.witness_hash(), tx2.witness_hash());
```
