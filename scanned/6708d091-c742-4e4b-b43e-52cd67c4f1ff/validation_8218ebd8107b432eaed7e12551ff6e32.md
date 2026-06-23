### Title
`BlockProposalProcess::execute()` Does Not Verify Response Corresponds to the Requested Block Hash — (`File: sync/src/relayer/block_proposal_process.rs`)

---

### Summary

`BlockProposalProcess::execute()` processes incoming `BlockProposal` relay messages without verifying that the returned transactions correspond to the block hash that was originally requested in the paired `GetBlockProposal` message. The `BlockProposal` wire message carries no `block_hash` field, and the in-flight tracking map stores only `ProposalShortId → BlockNumber`, discarding the block-hash context entirely. A malicious peer that can craft or pre-hold a transaction whose `ProposalShortId` collides with an in-flight proposal can inject it, evict the correct proposal from the in-flight set, mark a wrong transaction as "known", and stall compact-block reconstruction for the targeted block.

---

### Finding Description

**Request side — `request_proposal_txs`** (`sync/src/relayer/mod.rs`, lines 249–259):

When a compact block arrives with missing transactions, the node calls `request_proposal_txs`, which builds a `GetBlockProposal` message that includes a `block_hash` field identifying the specific block whose proposals are needed. [1](#0-0) 

Before sending, the node records the in-flight proposals in `inflight_proposals: DashMap<ProposalShortId, BlockNumber>` — keyed by short ID and block **number** only; the block **hash** is not stored. [2](#0-1) 

**Wire schema asymmetry** (`util/gen-types/schemas/extensions.mol`, lines 185–192):

`GetBlockProposal` carries a `block_hash` field. `BlockProposal` (the response) carries only `transactions` — no `block_hash`. [3](#0-2) 

**Response side — `BlockProposalProcess::execute()`** (`sync/src/relayer/block_proposal_process.rs`, lines 16–77):

The handler:
1. Derives a `ProposalShortId` from each received transaction's hash.
2. Calls `remove_inflight_proposals` — checks only whether that short ID exists in the in-flight map.
3. If it was in-flight, marks the transaction as "known" and forwards it to the tx pool via `notify_txs_async`.

At no point does the handler verify that the returned transaction belongs to the block hash that was originally requested. [4](#0-3) 

The in-flight map's `remove_inflight_proposals` only checks key existence; it has no block-hash dimension to compare against. [5](#0-4) 

---

### Impact Explanation

A malicious peer that holds (or can craft) a transaction T′ whose `ProposalShortId` equals a short ID currently in the victim's `inflight_proposals` can send an unsolicited `BlockProposal { transactions: [T′] }`. The handler will:

1. Remove the short ID from `inflight_proposals`, so the node no longer tracks it as pending.
2. Mark T′ as "known" in the tx filter, preventing re-request of the correct transaction through this path.
3. Submit T′ to the tx pool, polluting it with an irrelevant or malformed transaction.

When the node subsequently attempts compact-block reconstruction using T′, the `transactions_root` check fails (`compact_block_tx_root != reconstruct_block_tx_root`). [6](#0-5) 

The node is left unable to reconstruct the targeted compact block via the proposal path, stalling block propagation for that block.

---

### Likelihood Explanation

Exploiting this requires the attacker to supply a transaction T′ whose first 10 bytes of SHA3 hash match the target `ProposalShortId` (80-bit second-preimage). This is computationally infeasible for a casual attacker (~2^80 hash operations). However:

- A well-resourced adversary with a pre-built database of transactions indexed by short ID could attempt a lookup-based attack.
- The structural flaw (no `block_hash` in `BlockProposal`, no block-hash dimension in `inflight_proposals`) means the protocol provides zero cryptographic binding between a request and its response, making the design unsound regardless of current computational limits.
- The attack entry point is any unprivileged P2P peer — no authentication or special role is required.

Likelihood is **low** in practice today due to the 80-bit short ID space, but the design flaw is real and the impact when triggered is a targeted block-propagation stall.

---

### Recommendation

1. **Add `block_hash` to the `BlockProposal` wire message** so the response can be bound to the originating request:

```
table BlockProposal {
    block_hash:   Byte32,        // add this field
    transactions: TransactionVec,
}
```

2. **Store block hash in `inflight_proposals`**: change the map from `DashMap<ProposalShortId, BlockNumber>` to `DashMap<ProposalShortId, (BlockNumber, Byte32)>` so the response handler can verify the returned short IDs belong to the correct block hash.

3. **In `BlockProposalProcess::execute()`**: after deriving each `ProposalShortId`, look up the expected block hash from the in-flight map and reject any transaction whose associated block hash does not match the `block_hash` field of the incoming `BlockProposal` message.

---

### Proof of Concept

**Setup**: Victim node V has a pending compact block for block hash `H` with one missing transaction whose `ProposalShortId` is `P1`. V has sent `GetBlockProposal { block_hash: H, proposals: [P1] }` to malicious peer M. `inflight_proposals` now contains `{P1 → block_number}`.

**Attack**:
1. M holds transaction T′ with `ProposalShortId::from_tx_hash(T′.hash()) == P1` (pre-computed or brute-forced offline).
2. M sends `BlockProposal { transactions: [T′] }` to V (no `block_hash` field in the message).
3. `BlockProposalProcess::execute()` on V:
   - Line 53: computes `ProposalShortId::from_tx_hash(T′.hash()) == P1`. [7](#0-6) 
   - Line 55: `remove_inflight_proposals([P1])` returns `[true]` — P1 is evicted from the map. [8](#0-7) 
   - Line 59: `mark_as_known_tx(T′.hash())` — T′ is now "known". [9](#0-8) 
   - Line 69: T′ is submitted to the tx pool. [10](#0-9) 
4. V attempts compact-block reconstruction with T′; `transactions_root` mismatch is detected. [6](#0-5) 
5. P1 is no longer in `inflight_proposals`; the correct transaction for block `H` is never re-requested through the proposal path. Block `H` cannot be reconstructed from this peer.

### Citations

**File:** sync/src/relayer/mod.rs (L257-260)
```rust
                let content = packed::GetBlockProposal::new_builder()
                    .block_hash(block_hash_and_number.hash)
                    .proposals(to_ask_proposals.clone())
                    .build();
```

**File:** sync/src/relayer/mod.rs (L507-519)
```rust
            let compact_block_tx_root = compact_block.header().raw().transactions_root();
            let reconstruct_block_tx_root = block.transactions_root();
            if compact_block_tx_root != reconstruct_block_tx_root {
                if compact_block.short_ids().is_empty()
                    || compact_block.short_ids().len() == block_txs_len
                {
                    return ReconstructionResult::Error(
                        StatusCode::CompactBlockHasUnmatchedTransactionRootWithReconstructedBlock
                            .with_context(format!(
                                "Compact_block_tx_root({}) != reconstruct_block_tx_root({})",
                                compact_block.header().raw().transactions_root(),
                                block.transactions_root(),
                            )),
```

**File:** sync/src/types/mod.rs (L1548-1569)
```rust
    pub fn insert_inflight_proposals(
        &self,
        ids: Vec<packed::ProposalShortId>,
        block_number: BlockNumber,
    ) -> Vec<bool> {
        ids.into_iter()
            .map(|id| match self.inflight_proposals.entry(id) {
                dashmap::mapref::entry::Entry::Occupied(mut occupied) => {
                    if *occupied.get() < block_number {
                        occupied.insert(block_number);
                        true
                    } else {
                        false
                    }
                }
                dashmap::mapref::entry::Entry::Vacant(vacant) => {
                    vacant.insert(block_number);
                    true
                }
            })
            .collect()
    }
```

**File:** sync/src/types/mod.rs (L1571-1575)
```rust
    pub fn remove_inflight_proposals(&self, ids: &[packed::ProposalShortId]) -> Vec<bool> {
        ids.iter()
            .map(|id| self.inflight_proposals.remove(id).is_some())
            .collect()
    }
```

**File:** util/gen-types/schemas/extensions.mol (L185-192)
```text
table GetBlockProposal {
    block_hash:                 Byte32,
    proposals:                  ProposalShortIdVec,
}

table BlockProposal {
    transactions:               TransactionVec,
}
```

**File:** sync/src/relayer/block_proposal_process.rs (L51-62)
```rust
        let proposals: Vec<packed::ProposalShortId> = unknown_txs
            .iter()
            .map(|tx| packed::ProposalShortId::from_tx_hash(&tx.hash()))
            .collect();
        let removes = sync_state.remove_inflight_proposals(&proposals);
        let mut asked_txs = Vec::new();
        for (previously_in, tx) in removes.into_iter().zip(unknown_txs) {
            if previously_in {
                sync_state.mark_as_known_tx(tx.hash());
                asked_txs.push(tx);
            }
        }
```

**File:** sync/src/relayer/block_proposal_process.rs (L69-69)
```rust
        if let Err(err) = tx_pool.notify_txs_async(asked_txs).await {
```
