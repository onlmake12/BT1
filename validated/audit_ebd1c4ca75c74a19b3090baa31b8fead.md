### Title
Attacker-Controlled Compact Block Poisons `pending_compact_blocks`, Causing Legitimate Peer Banning and Block Propagation Delay - (File: `sync/src/relayer/compact_block_process.rs`)

---

### Summary

The `missing_or_collided_post_process` function uses an `or_insert_with` "get-or-create" pattern to store the compact block from the **first** peer that triggers a missing-transaction request. An unprivileged attacker peer can race to send a structurally valid but content-corrupted compact block (with fake `short_ids`) for a real block hash before the legitimate peer does. The victim stores the attacker's compact block. When the legitimate peer later responds to a `GetBlockTransactions` request, `BlockTransactionsVerifier::verify` cross-checks the received transactions against the **stored** (attacker-poisoned) compact block's short IDs, the check fails, and the legitimate peer is banned. This is a direct analog to the "get-or-create pool" frontrunning class: attacker-controlled initialization state is silently accepted and then used to validate a victim's subsequent interaction.

---

### Finding Description

**Root cause — `missing_or_collided_post_process`, line 359:**

```rust
// sync/src/relayer/compact_block_process.rs  lines 354-361
shared
    .state()
    .pending_compact_blocks()
    .await
    .entry(block_hash.clone())
    .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
    .1
    .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```

`or_insert_with` stores the compact block from whichever peer arrives first. Subsequent peers' compact blocks are silently discarded; only their per-peer missing-index lists are appended to the existing entry.

**Why a different peer can reach this path:**

`contextual_check` only rejects a compact block if the **same** peer already has a pending entry for that block hash:

```rust
// sync/src/relayer/compact_block_process.rs  lines 285-291
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{
    return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
}
```

A **different** peer (the attacker) can therefore insert a compact block for the same hash without any check.

**Why the attacker's compact block passes structural verification:**

`CompactBlockVerifier::verify` only checks that the cellbase is prefilled, that short IDs have no duplicates, and that short IDs do not intersect with prefilled transactions:

```rust
// sync/src/relayer/compact_block_verifier.rs  lines 11-15
pub(crate) fn verify(block: &packed::CompactBlock) -> Status {
    attempt!(PrefilledVerifier::verify(block));
    attempt!(ShortIdsVerifier::verify(block));
    Status::ok()
}
```

It does **not** verify that the `short_ids` correspond to the actual transactions committed in the block's `transactions_root`. The attacker can craft a compact block carrying the legitimate block's header (correct hash) but with arbitrary fake `ProposalShortId` values in `short_ids`.

CKB's `CompactBlock` schema has no nonce field:

```
// util/gen-types/schemas/extensions.mol  lines 138-144
table CompactBlock {
    header:                     Header,
    short_ids:                  ProposalShortIdVec,
    prefilled_transactions:     IndexTransactionVec,
    uncles:                     Byte32Vec,
    proposals:                  ProposalShortIdVec,
}
```

The `short_ids` are a free P2P-layer field; they are not committed to by the block hash.

**How the poisoned state causes legitimate peer banning:**

When the legitimate peer later sends a `BlockTransactions` response, `BlockTransactionsProcess::execute` retrieves the **stored** (attacker's) compact block and calls:

```rust
// sync/src/relayer/block_transactions_process.rs  lines 80-84
attempt!(BlockTransactionsVerifier::verify(
    compact_block,                    // ← attacker's compact block
    expected_transaction_indexes,     // ← indexes from legitimate peer's compact block
    &received_transactions,           // ← legitimate peer's real transactions
));
```

`BlockTransactionsVerifier::verify` extracts short IDs from the **attacker's** compact block at the expected indexes and compares them to the real transactions' `proposal_short_id()`:

```rust
// sync/src/relayer/block_transactions_verifier.rs  lines 32-39
for (expected_short_id, tx) in missing_short_ids.into_iter().zip(transactions) {
    let short_id = tx.proposal_short_id();
    if expected_short_id != short_id {
        return StatusCode::BlockTransactionsShortIdsAreUnmatchedWithPendingCompactBlock
            .with_context(...)
    }
}
```

The attacker's fake short IDs do not match the real transactions' short IDs → verification fails → the relayer's `process` function calls `nc.ban_peer`:

```rust
// sync/src/relayer/mod.rs  lines 195-204
if let Some(ban_time) = status.should_ban() {
    ...
    nc.ban_peer(peer, ban_time, status.to_string());
}
```

The legitimate peer is banned. The victim cannot reconstruct the block from any honest peer that already responded.

---

### Impact Explanation

- **Legitimate peers banned**: Any honest peer that responds to a `GetBlockTransactions` request for a poisoned pending entry is banned by the victim node.
- **Block propagation delay / stall**: The victim cannot reconstruct the block from honest peers. It must wait for a timeout or a new compact block relay path.
- **Amplified eclipse risk**: An attacker who is a direct peer of a victim can systematically poison every new block's pending entry, progressively banning all honest peers and isolating the victim.
- **No fund loss directly**, but a stalled or eclipsed node cannot validate the chain, submit transactions, or mine on the correct tip.

---

### Likelihood Explanation

- The attacker only needs to be an **unprivileged P2P peer** of the victim — no keys, no hashpower, no privileged role required.
- The attacker receives the legitimate compact block from the network, replaces `short_ids` with fake values (keeping the header intact so the hash is unchanged), and relays it to the victim.
- The attacker must arrive before the legitimate peer. In practice this is achievable by being a well-connected peer with low latency to the victim, or by being the victim's only peer for that block hash at the moment of relay.
- The attack is repeatable for every new block.

---

### Recommendation

1. **Validate short IDs against the block's `transactions_root` before storing**: After `reconstruct_block` returns `Missing`, verify that the compact block's `short_ids` are a subset of the IDs derivable from the block's committed `transactions_root`. Reject and ban the sender if they are not.
2. **Replace the stored compact block when a later peer's compact block leads to a strictly smaller missing set**: Instead of unconditionally keeping the first-seen compact block, update the stored entry when a new peer's compact block produces fewer missing transactions.
3. **Cross-check stored vs. incoming compact block consistency**: Before appending a new peer's missing indexes to an existing entry, verify that the new compact block's `short_ids` are consistent with the stored one. If they differ, do not mix their missing-index lists.

---

### Proof of Concept

1. Attacker connects to victim as a normal P2P peer.
2. A new block B is mined; its compact block (with correct header hash H and correct `short_ids`) propagates through the network.
3. Attacker intercepts or independently receives the compact block for H.
4. Attacker constructs a modified compact block: same header (hash H unchanged), but `short_ids` replaced with arbitrary fake `ProposalShortId` values. The modified block passes `CompactBlockVerifier::verify` (cellbase prefilled, no duplicates).
5. Attacker sends the modified compact block to the victim **before** the legitimate peer does.
6. Victim's `CompactBlockProcess::execute` runs: `contextual_check` passes (attacker is a new peer for this hash), `CompactBlockVerifier::verify` passes, `reconstruct_block` returns `Missing` (fake short IDs match nothing in the pool).
7. `missing_or_collided_post_process` is called: `or_insert_with` stores the **attacker's** compact block in `pending_compact_blocks[H]`. [1](#0-0) 
8. Legitimate peer sends the real compact block for H. `contextual_check` passes (different peer). `reconstruct_block` returns `Missing`. `missing_or_collided_post_process` is called: `or_insert_with` does **not** replace the stored compact block; legitimate peer's missing indexes (computed from the real compact block) are stored under the legitimate peer's entry. [2](#0-1) 
9. Victim sends `GetBlockTransactions` to the legitimate peer with the missing indexes.
10. Legitimate peer responds with the correct transactions.
11. `BlockTransactionsProcess::execute` retrieves the stored (attacker's) compact block and calls `BlockTransactionsVerifier::verify`. The attacker's fake short IDs do not match the real transactions' `proposal_short_id()`. [3](#0-2) 
12. `attempt!` returns `BlockTransactionsShortIdsAreUnmatchedWithPendingCompactBlock`. The relayer's `process` function calls `nc.ban_peer` on the legitimate peer. [4](#0-3) 
13. The victim has banned its honest peer and cannot reconstruct block B from it.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```

**File:** sync/src/relayer/block_transactions_verifier.rs (L32-39)
```rust
        for (expected_short_id, tx) in missing_short_ids.into_iter().zip(transactions) {
            let short_id = tx.proposal_short_id();
            if expected_short_id != short_id {
                return StatusCode::BlockTransactionsShortIdsAreUnmatchedWithPendingCompactBlock
                    .with_context(format!(
                        "Expected({expected_short_id}) != actual({short_id})",
                    ));
            }
```

**File:** sync/src/relayer/mod.rs (L195-204)
```rust
        if let Some(ban_time) = status.should_ban() {
            error_target!(
                crate::LOG_TARGET_RELAY,
                "receive {} from {}, ban {:?} for {}",
                item_name,
                peer,
                ban_time,
                status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
```
