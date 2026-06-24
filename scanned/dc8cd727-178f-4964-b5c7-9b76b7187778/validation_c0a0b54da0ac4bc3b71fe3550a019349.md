Audit Report

## Title
Unauthenticated `CompactBlock.proposals` Consumed Before `proposals_hash` Integrity Check Enables Spurious Proposal Injection — (`sync/src/relayer/compact_block_process.rs`)

## Summary
In `CompactBlockProcess::execute`, the `proposals` field of a received `CompactBlock` is extracted and passed to `request_proposal_txs` before the field's integrity is verified against the `proposals_hash` commitment in the block header. A relay peer with no mining capability can replace the `proposals` field on any valid compact block it forwards, causing victim nodes to send spurious `GetBlockProposal` requests, pollute their `inflight_proposals` state, and delay block propagation — all at negligible attacker cost.

## Finding Description
**Execution order in `CompactBlockProcess::execute`:**

1. `non_contextual_check` enforces only a count limit on `compact_block.proposals()` — no hash check.
2. `contextual_check` verifies the block header (PoW, timestamp, parent).
3. `CompactBlockVerifier::verify` runs `PrefilledVerifier` and `ShortIdsVerifier` — neither touches `proposals`.
4. **Line 81–87**: `compact_block.proposals()` is immediately iterated and passed to `request_proposal_txs` — before any hash commitment is verified.
5. Block reconstruction and `accept_block` happen afterward; `MerkleRootVerifier` checks `proposals_hash` only at this late stage.

The `proposals` field is a separate molecule table field in `CompactBlock` and is not covered by the header hash:

```
table CompactBlock {
    header:                     Header,       // hash covers only RawHeader
    short_ids:                  ProposalShortIdVec,
    prefilled_transactions:     IndexTransactionVec,
    uncles:                     Byte32Vec,
    proposals:                  ProposalShortIdVec,  // ← unchecked at relay layer
}
```

`request_proposal_txs` calls `tx_pool.fresh_proposals_filter` to skip already-known proposals, then calls `insert_inflight_proposals` and sends `GetBlockProposal` to the peer for any remaining IDs. An attacker who controls the fake IDs (by pre-computing `ProposalShortId` values for transactions they own) can craft the tampered `proposals` list so that the victim asks for those exact IDs, and then respond via `BlockProposalProcess` to submit those transactions to the victim's tx pool via `notify_txs_async`.

The `proposals_hash` check that would catch this is present in `MerkleRootVerifier::verify` and in `UnclesVerifier` for uncle proposals, but is absent at the compact-block relay layer.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Each tampered compact block causes the victim to:
- Send one `GetBlockProposal` message per injected fake ID (outbound bandwidth amplification).
- Hold fake IDs in `inflight_proposals` until the TTL window expires.
- Reject the block only after full reconstruction and `MerkleRootVerifier` failure, forcing a re-fetch from another peer and delaying block propagation.

With Sybil connections (many cheap relay peers), an attacker can simultaneously deliver tampered compact blocks to many nodes on the network, amplifying spurious request traffic and delaying block propagation network-wide. The attacker's cost is a relay connection and a byte-level field replacement; the victim's cost is proportional to the number of injected fake proposal IDs (up to `max_block_proposals_limit`).

## Likelihood Explanation
No mining capability is required. Any peer that can establish a P2P connection and receive a valid compact block from the network can execute this attack by modifying the `proposals` bytes in the molecule-encoded message before forwarding. The modification is trivial and the attack is reachable on mainnet. The one-shot-per-connection constraint is mitigated by Sybil connections.

## Recommendation
Before calling `request_proposal_txs`, verify that the hash of `compact_block.proposals()` matches the `proposals_hash` committed in the block header. This check should be added to `non_contextual_check` or `CompactBlockVerifier::verify`:

```rust
let expected = compact_block.header().raw().proposals_hash();
let actual = compact_block.proposals().as_reader().calc_proposals_hash();
if expected != actual {
    return StatusCode::ProtocolMessageIsMalformed.with_context(
        "CompactBlock proposals_hash mismatch"
    );
}
```

This mirrors the existing check in `UnclesVerifier` at `verification/contextual/src/uncles_verifier.rs:107–109` and the `MerkleRootVerifier` check at `verification/src/block_verifier.rs:205–207`, bringing the relay-layer validation in line with full-block validation.

## Proof of Concept
1. Attacker establishes a P2P relay connection to a victim CKB node.
2. Attacker receives a valid `CompactBlock` (valid PoW header) from the network.
3. Attacker pre-generates N transactions, computes their `ProposalShortId` values (`first_10_bytes(tx_hash)`).
4. Attacker replaces the `proposals` field in the molecule-encoded `CompactBlock` with those N short IDs (header bytes are untouched).
5. Attacker forwards the tampered `CompactBlock` to the victim.
6. Victim passes `non_contextual_check` (count ≤ limit), `contextual_check` (header PoW valid), and `CompactBlockVerifier::verify` (only checks prefilled/short_ids).
7. Victim executes lines 81–87 of `compact_block_process.rs`: extracts fake proposals, calls `request_proposal_txs`, which inserts them into `inflight_proposals` and sends `GetBlockProposal` back to the attacker.
8. Attacker responds with `BlockProposal` containing the N pre-generated transactions; `BlockProposalProcess::execute` matches them against `inflight_proposals` and calls `tx_pool.notify_txs_async`.
9. Victim attempts block reconstruction; `MerkleRootVerifier` detects `proposals_hash` mismatch and rejects the block; attacker peer is banned.
10. Net result: N spurious `GetBlockProposal` round-trips, `inflight_proposals` polluted for the TTL window, block propagation delayed. Repeatable via fresh Sybil connections.