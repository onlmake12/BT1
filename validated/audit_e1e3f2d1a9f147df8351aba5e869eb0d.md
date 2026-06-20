### Title
Stale `inflight_proposals` Entries Persist After Peer Disconnect, Blocking Proposal Re-Requests — (`sync/src/types/mod.rs`)

---

### Summary

The `inflight_proposals` map in `SyncState` accumulates entries when the node requests transaction proposals from peers. These entries are never removed when the requesting peer disconnects. The only cleanup path (`clear_expired_inflight_proposals`) is gated exclusively on receiving a `BlockProposal` message — it is never called on peer disconnect. A malicious sync peer can exploit this to permanently suppress the node's ability to re-request specific proposals from honest peers, causing compact block reconstruction failures and degraded sync throughput.

---

### Finding Description

`SyncState` maintains two relevant structures:

```
inflight_proposals: DashMap<ProposalShortId, BlockNumber>
``` [1](#0-0) 

When a compact block arrives with missing transactions, `request_proposal_txs` calls `insert_inflight_proposals`, recording each requested proposal ID mapped to the current block number, then sends a `GetBlockProposal` message to the peer: [2](#0-1) 

`insert_inflight_proposals` returns `false` (suppressing re-request) for any proposal already present with an equal or higher block number: [3](#0-2) 

The **only** cleanup path is `clear_expired_inflight_proposals`, which is called exclusively inside `BlockProposalProcess::execute()` — i.e., only when a `BlockProposal` response is actually received: [4](#0-3) [5](#0-4) 

The `disconnected()` handler removes inflight **blocks** but performs **no cleanup of `inflight_proposals`**: [6](#0-5) 

Because `inflight_proposals` maps `ProposalShortId → BlockNumber` (no peer index stored), it is structurally impossible to remove entries by peer at disconnect time. There is no periodic timer that calls `clear_expired_inflight_proposals` independently of receiving a `BlockProposal` message.

---

### Impact Explanation

A malicious peer:
1. Sends a compact block referencing proposal IDs not in the victim's tx-pool.
2. The victim calls `insert_inflight_proposals` and sends `GetBlockProposal` to the attacker.
3. The attacker disconnects without responding.
4. The `inflight_proposals` entries remain, keyed to the block number of the attacker's compact block.
5. When an honest peer later sends a compact block referencing the same proposal IDs at the same or lower block number, `insert_inflight_proposals` returns `false` for all of them — the node does **not** re-request them.
6. The node cannot reconstruct the compact block and must fall back to requesting the full block, increasing bandwidth and latency.
7. Cleanup only occurs when any `BlockProposal` message is eventually received from any peer, which may not happen promptly if the attacker was the primary relay peer.

This is a direct analog to the reported `s_votesByPool[m_pool]` persistence bug: state is incremented on an event (proposal request) and never decremented on the corresponding lifecycle end (peer disconnect), causing stale state to influence future behavior (proposal re-request suppression).

---

### Likelihood Explanation

Any unprivileged sync/relay peer can trigger this by:
- Sending a syntactically valid compact block with missing proposals.
- Disconnecting before responding to `GetBlockProposal`.

No special privileges, keys, or majority hashpower are required. The attack is repeatable and low-cost.

---

### Recommendation

1. **Track the requesting peer** in `inflight_proposals`: change the map value from `BlockNumber` to `(BlockNumber, PeerIndex)`, enabling targeted cleanup in `disconnected()`.
2. **Alternatively**, introduce a periodic timer (analogous to `find_blocks_to_fetch` / `prune`) that calls `clear_expired_inflight_proposals` independently of receiving `BlockProposal` messages.
3. **At minimum**, call `clear_expired_inflight_proposals` from `disconnected()` using the current tip minus the farthest proposal window, even without per-peer tracking, to bound the persistence window.

---

### Proof of Concept

```
1. Attacker peer connects to victim node.
2. Attacker sends RelayMessage::CompactBlock referencing proposal IDs
   [P1, P2, P3] not present in victim's tx-pool.
3. Victim calls request_proposal_txs → insert_inflight_proposals([P1,P2,P3], N)
   → sends GetBlockProposal to attacker.
4. Attacker disconnects immediately (no BlockProposal response).
5. disconnected(attacker_peer) is called:
   - remove_by_peer(attacker_peer) cleans inflight_blocks ✓
   - inflight_proposals still contains {P1→N, P2→N, P3→N} ✗
6. Honest peer sends CompactBlock at block N (same height) referencing [P1,P2,P3].
7. Victim calls insert_inflight_proposals([P1,P2,P3], N):
   - For each: occupied entry found, *occupied.get() == N, not < N → returns false.
8. to_ask_proposals is empty → no GetBlockProposal sent to honest peer.
9. Victim cannot reconstruct compact block; must request full block.
10. clear_expired_inflight_proposals is never called until some unrelated
    BlockProposal message arrives, which may be arbitrarily delayed.
```

### Citations

**File:** sync/src/types/mod.rs (L1334-1336)
```rust
    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,
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

**File:** sync/src/types/mod.rs (L1577-1580)
```rust
    pub fn clear_expired_inflight_proposals(&self, keep_min_block_number: BlockNumber) {
        self.inflight_proposals
            .retain(|_, block_number| *block_number >= keep_min_block_number);
    }
```

**File:** sync/src/types/mod.rs (L1607-1616)
```rust
    pub fn disconnected(&self, pi: PeerIndex) {
        let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
        if removed_inflight_blocks_count > 0 {
            debug!(
                "disconnected {}, remove {} inflight blocks",
                pi, removed_inflight_blocks_count
            )
        }
        self.peers().disconnected(pi);
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

**File:** sync/src/relayer/block_proposal_process.rs (L19-24)
```rust
        sync_state.clear_expired_inflight_proposals(
            shared
                .active_chain()
                .tip_number()
                .saturating_sub(shared.consensus().tx_proposal_window().farthest()),
        );
```
