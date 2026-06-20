### Title
`inflight_proposals` Entries Stuck Indefinitely When Peer Silently Drops `GetBlockProposal` — (`File: sync/src/relayer/block_proposal_process.rs`, `sync/src/types/mod.rs`)

---

### Summary

When a CKB node receives a compact block and cannot reconstruct it locally, it calls `request_proposal_txs`, which inserts proposal short IDs into `inflight_proposals` and sends a `GetBlockProposal` message to the originating peer. If that peer never responds with a `BlockProposal` message, the entries in `inflight_proposals` are never cleaned up, because `clear_expired_inflight_proposals` is exclusively triggered inside `BlockProposalProcess::execute()` — i.e., only upon receipt of a `BlockProposal` message. No periodic timer or peer-disconnect handler clears these entries. While stuck, `insert_inflight_proposals` suppresses re-requests for the same proposals from other peers, stalling compact block reconstruction indefinitely.

---

### Finding Description

The `SyncState` struct holds two relevant fields:

```
inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>
``` [1](#0-0) 

When a compact block arrives and reconstruction fails (missing transactions), `request_proposal_txs` is called: [2](#0-1) 

This inserts proposals via `insert_inflight_proposals`. The insertion logic returns `false` (blocking re-request) for any proposal already present with an equal or higher block number: [3](#0-2) 

The only place `clear_expired_inflight_proposals` is ever called is inside `BlockProposalProcess::execute()`: [4](#0-3) 

There is no periodic timer, no peer-disconnect hook, and no other code path that calls `clear_expired_inflight_proposals`. A grep across all production sync source confirms it appears in exactly one call site: [5](#0-4) 

The only other removal path is `remove_inflight_proposals`, called when the send of `GetBlockProposal` itself fails: [6](#0-5) 

If the send succeeds but the peer simply never replies, neither removal path fires.

---

### Impact Explanation

A malicious or faulty peer that sends a syntactically valid compact block (passing all non-contextual and contextual checks) and then silently drops every `GetBlockProposal` response causes the victim node to:

1. Permanently lock the affected `ProposalShortId` entries in `inflight_proposals` at the block number of the attacker's compact block.
2. Suppress re-requests for those proposals from any other peer, because `insert_inflight_proposals` returns `false` for entries already present at the same or higher block number.
3. Fail to reconstruct the compact block via the relay path, forcing fallback to the slower full-block sync path.
4. Accumulate unbounded entries in `inflight_proposals` across repeated attacks (one per compact block sent), since cleanup only fires on receipt of a `BlockProposal` — which the attacker withholds.

The node is not fully blocked (it can still receive the full block via `GetBlocks`/`SendBlock`), but compact block relay — the primary fast-path for block propagation — is stalled for the affected block hashes, and the `inflight_proposals` map grows without bound until some unrelated `BlockProposal` message arrives from any peer and triggers the epoch-window cleanup. [7](#0-6) 

---

### Likelihood Explanation

Any unprivileged peer connected to the victim node can trigger this. The attacker only needs to:

1. Connect as a normal relay peer.
2. Construct or relay a valid compact block that contains at least one proposal short ID not already in the victim's tx-pool.
3. Respond to the victim's `GetBlockProposal` with silence (drop the message at the network layer or simply not implement the handler).

No special privileges, no hashpower, no Sybil attack required. A single connected peer suffices. The compact block passes all rate-limit and structural checks before `request_proposal_txs` is called: [8](#0-7) 

---

### Recommendation

1. **Add a periodic cleanup timer** in the relayer's `notify` or `poll` loop that calls `clear_expired_inflight_proposals` unconditionally, keyed on the current tip number minus the farthest proposal window — independent of whether any `BlockProposal` has been received.

2. **Clear `inflight_proposals` entries on peer disconnect** so that proposals locked to a now-disconnected peer are immediately freed for re-request from other peers.

3. **Allow re-request from alternate peers** when a proposal has been in-flight beyond a configurable timeout (analogous to `BLOCK_DOWNLOAD_TIMEOUT` used for `inflight_blocks`), rather than suppressing re-requests solely based on block number comparison.

---

### Proof of Concept

**Setup**: Victim node V connected to attacker peer A and honest peer H.

1. A sends V a valid `CompactBlock` for block B at height N, containing proposal short ID `P` not in V's tx-pool.
2. V calls `request_proposal_txs` → `insert_inflight_proposals([P], N)` succeeds (vacant entry) → V sends `GetBlockProposal` to A.
3. A drops the `GetBlockProposal` silently (no `BlockProposal` reply ever sent).
4. H also sends the same `CompactBlock` for block B. V calls `request_proposal_txs` again → `insert_inflight_proposals([P], N)` returns `false` (occupied, same block number) → V does **not** send `GetBlockProposal` to H.
5. V cannot reconstruct block B via the relay path. `inflight_proposals` retains `P → N` indefinitely.
6. `clear_expired_inflight_proposals` is never called because no `BlockProposal` message arrives. [9](#0-8) [5](#0-4)

### Citations

**File:** sync/src/types/mod.rs (L1334-1336)
```rust
    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,
```

**File:** sync/src/types/mod.rs (L1548-1568)
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
```

**File:** sync/src/types/mod.rs (L1577-1580)
```rust
    pub fn clear_expired_inflight_proposals(&self, keep_min_block_number: BlockNumber) {
        self.inflight_proposals
            .retain(|_, block_number| *block_number >= keep_min_block_number);
    }
```

**File:** sync/src/relayer/mod.rs (L249-267)
```rust
            let to_ask_proposals: Vec<ProposalShortId> = shared
                .state()
                .insert_inflight_proposals(fresh_proposals.clone(), block_hash_and_number.number)
                .into_iter()
                .zip(fresh_proposals)
                .filter_map(|(firstly_in, id)| if firstly_in { Some(id) } else { None })
                .collect();
            if !to_ask_proposals.is_empty() {
                let content = packed::GetBlockProposal::new_builder()
                    .block_hash(block_hash_and_number.hash)
                    .proposals(to_ask_proposals.clone())
                    .build();
                let message = packed::RelayMessage::new_builder().set(content).build();
                if !async_quick_send_message_to(&nc, peer, &message)
                    .await
                    .is_ok()
                {
                    shared.state().remove_inflight_proposals(&to_ask_proposals);
                }
```

**File:** sync/src/relayer/block_proposal_process.rs (L16-24)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
        let sync_state = shared.state();
        sync_state.clear_expired_inflight_proposals(
            shared
                .active_chain()
                .tip_number()
                .saturating_sub(shared.consensus().tx_proposal_window().farthest()),
        );
```

**File:** sync/src/relayer/compact_block_process.rs (L56-88)
```rust
    pub async fn execute(self) -> Status {
        let instant = Instant::now();
        let shared = self.relayer.shared();
        let active_chain = shared.active_chain();
        let compact_block = self.message.to_entity();
        let header = compact_block.header().into_view();
        let block_hash = header.hash();

        let status =
            non_contextual_check(&compact_block, &header, shared.consensus(), &active_chain);
        if !status.is_ok() {
            return status;
        }

        let status = contextual_check(&header, shared, &active_chain, &self.nc, self.peer).await;
        if !status.is_ok() {
            return status;
        }

        // The new arrived has greater difficulty than local best known chain
        attempt!(CompactBlockVerifier::verify(&compact_block));
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);

        // Request proposal
        let proposals: Vec<_> = compact_block.proposals().into_iter().collect();
        self.relayer.request_proposal_txs(
            &self.nc,
            self.peer,
            (header.number(), block_hash.clone()).into(),
            proposals,
        );

```
