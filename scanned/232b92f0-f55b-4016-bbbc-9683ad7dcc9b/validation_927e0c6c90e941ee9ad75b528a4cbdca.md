Audit Report

## Title
Two-Phase Commit Bypass via `ProposalShortId` Collision — (`verification/contextual/src/contextual_block_verifier.rs`)

## Summary
`TwoPhaseCommitVerifier::verify()` enforces the two-phase commit rule by comparing committed transaction short IDs against proposed short IDs, where a `ProposalShortId` is only the first 10 bytes (80 bits) of the BLAKE2b transaction hash. A miner who finds two transactions sharing the same `ProposalShortId` can propose a decoy transaction and commit a completely different, never-proposed transaction in a later block. Every node runs the same truncated-ID check and accepts the block, silently bypassing a core CKB consensus invariant.

## Finding Description
`ProposalShortId::from_tx_hash` truncates the 32-byte BLAKE2b hash to 10 bytes:

```rust
// util/gen-types/src/extension/shortcut.rs:29-33
inner.copy_from_slice(&h.as_slice()[..10]);
```

`TwoPhaseCommitVerifier::verify()` builds `committed_ids` and `proposal_txs_ids` both as `HashSet<ProposalShortId>` and checks only set membership:

```rust
// verification/contextual/src/contextual_block_verifier.rs:187-195
let committed_ids: HashSet<_> = self.block.transactions().iter().skip(1)
    .map(TransactionView::proposal_short_id).collect();
if committed_ids.difference(&proposal_txs_ids).next().is_some() {
    return Err((CommitError::Invalid).into());
}
```

No full 32-byte hash comparison is ever performed. If `tx_A.hash()[..10] == tx_B.hash()[..10]`, proposing `tx_A` satisfies the check for committing `tx_B`. The tx-pool guard (`check_txid_collision` in `tx-pool/src/util.rs:20-26`) only blocks `tx_B` from entering the mempool when `tx_A` is already present — a miner bypasses this entirely by including `tx_B` directly in a produced block without submitting it to the mempool. The relay layer explicitly acknowledges collisions are possible (`sync/src/relayer/block_transactions_process.rs:12-22`) and handles them gracefully, but the consensus verifier does not.

## Impact Explanation
A successfully committed unproposed transaction constitutes a direct consensus deviation: the two-phase commit rule — a protocol-level invariant — is violated, and every node on the network accepts the invalid block because all nodes run the identical truncated-ID check. This maps to **Critical: Vulnerabilities which could easily cause consensus deviation** (15001–25000 points). Additionally, `RewardCalculator` (`util/reward-calculator/src/lib.rs:204-210`) also uses `ProposalShortId::from_tx_hash` for reward attribution, so a collision also corrupts proposer reward accounting, touching **Critical: Vulnerabilities which could easily damage CKB economy**.

## Likelihood Explanation
`ProposalShortId` is 80 bits. A birthday attack over the 80-bit prefix requires approximately 2^40 BLAKE2b evaluations to find a colliding pair. The attacker controls transaction content (inputs, outputs, witnesses) and can iterate over witness bytes freely without needing to break BLAKE2b — only the 80-bit prefix must match. 2^40 operations is within reach of a miner with dedicated hardware (comparable to mining-level computation). The attack is repeatable, silent, and network-wide once a collision is found. The codebase's own test infrastructure confirms the collision scenario is real and intentionally exercised (`sync/src/relayer/tests/compact_block_process.rs:585-597`).

## Recommendation
The `TwoPhaseCommitVerifier` must compare committed transactions against proposals using **full 32-byte transaction hashes**, not 10-byte short IDs. This is a protocol-level change requiring the proposal zone to carry full hashes. As an interim measure, the proposal short ID length should be increased to at least 20 bytes (160 bits) to make birthday-attack collisions computationally infeasible (2^80 evaluations). The compact-block relay path can retain short IDs for bandwidth efficiency, but consensus verification must use full hashes.

## Proof of Concept
The existing test at `sync/src/relayer/tests/compact_block_process.rs:585-597` directly demonstrates the collision property:

```rust
let fake_hash = missing_tx.hash().as_builder()
    .nth31(0u8).nth30(0u8).nth29(0u8).nth28(0u8).build();
let fake_tx = missing_tx.clone().fake_hash(fake_hash);
assert_eq!(missing_tx.proposal_short_id(), fake_tx.proposal_short_id());
assert_ne!(missing_tx.hash(), fake_tx.hash());
```

**Attack steps:**
1. Craft `tx_A` and `tx_B` such that `tx_A.hash()[..10] == tx_B.hash()[..10]` (~2^40 BLAKE2b evaluations over witness bytes).
2. Submit a block proposing `tx_A`'s short ID in the proposal zone.
3. After `proposal_window.closest()` blocks, mine a block committing `tx_B` (not `tx_A`) in the transaction zone.
4. `TwoPhaseCommitVerifier::verify()` at line 195 computes `committed_ids` containing `tx_B.proposal_short_id()`, finds it in `proposal_txs_ids` (which contains `tx_A.proposal_short_id()` — identical value), and returns `Ok(())`.
5. The block passes all consensus checks on every node. `tx_B` is committed without ever being proposed, bypassing the two-phase commit rule network-wide.