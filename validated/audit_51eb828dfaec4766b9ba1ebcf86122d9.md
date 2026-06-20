### Title
Unbounded `pending_get_block_proposals` Growth via Unauthenticated `GetBlockProposal` P2P Messages — (File: `sync/src/types/mod.rs`)

---

### Summary

The `pending_get_block_proposals` field in `SyncState` is a `DashMap` with no capacity bound. It is populated by any connected P2P peer sending `GetBlockProposal` relay messages that reference proposal IDs absent from the local tx-pool. The only cleanup path is `drain_get_block_proposals`, which is invoked either on a periodic timer or when a `BlockProposal` response is processed. An attacker who never sends `BlockProposal` responses and continuously floods the node with `GetBlockProposal` messages containing unique, non-existent proposal IDs can cause the map to grow without bound, exhausting the node's heap memory and causing a DoS.

---

### Finding Description

**Root cause — unbounded insertion with deferred cleanup**

`SyncState` declares the map without any size limit: [1](#0-0) 

Every incoming `GetBlockProposal` relay message that contains proposal IDs not found in the local tx-pool is forwarded to `insert_get_block_proposals`: [2](#0-1) 

`insert_get_block_proposals` appends each unknown ID to the map with no capacity check: [3](#0-2) 

The only removal path is `drain_get_block_proposals`, which clones and then clears the entire map: [4](#0-3) 

This drain is called from `sync/src/relayer/block_proposal_process.rs` (triggered by a `BlockProposal` response from a peer) and from `sync/src/relayer/mod.rs` (on a periodic timer). An attacker who only sends `GetBlockProposal` requests and never sends `BlockProposal` responses bypasses the response-triggered cleanup path entirely. Between timer ticks the map accumulates entries without bound.

**Per-message size check does not bound total map size**

The handler enforces a per-message limit: [5](#0-4) 

`max_block_proposals_limit` (default 1500) × `max_uncles_num` (default 2) = 3 000 IDs per message. This check prevents a single oversized message from being accepted, but it does not limit the total number of messages a peer may send, nor the cumulative size of the map across messages.

**Structural analogy to the reference report**

| Reference report | CKB analog |
|---|---|
| `client_infos_acc` / `client_infos2_acc` reserved on order creation | `pending_get_block_proposals` entry inserted on `GetBlockProposal` receipt |
| Deallocation only on `finalize_spot` / `move_spot_avail_funds` | Deallocation only on `drain_get_block_proposals` (timer or `BlockProposal` response) |
| Attacker avoids cleanup by never invoking those functions | Attacker avoids cleanup by never sending `BlockProposal` responses |
| Gradual exhaustion of on-chain account space | Gradual exhaustion of node heap memory |

---

### Impact Explanation

Each `DashMap` entry holds a `ProposalShortId` (10 bytes) plus a `HashSet<PeerIndex>` with its allocator overhead (~80–120 bytes per entry in practice). With the default inbound peer limit and no per-peer message rate limit on relay messages, an attacker controlling even a single inbound connection can inject up to 3 000 unique entries per `GetBlockProposal` message. Sustained flooding across the timer interval fills the map to hundreds of thousands or millions of entries, consuming hundreds of megabytes to gigabytes of heap, ultimately triggering OOM on the node process and halting all block relay, transaction relay, and sync activity.

---

### Likelihood Explanation

- **Entry path**: Any peer that completes the P2P handshake can send `GetBlockProposal` relay messages. No PoW, no stake, no privileged key is required.
- **Cost**: Proposal IDs are 10 bytes each; crafting millions of unique random IDs is trivial.
- **Detectability**: The attack is indistinguishable from a legitimate node requesting proposals for transactions it has not yet seen.
- **Existing mitigations**: The periodic timer in `relayer/mod.rs` provides partial relief, but it does not bound the peak map size between ticks. No per-peer rate limit or map capacity cap exists in the current code.

---

### Recommendation

1. **Add a hard capacity cap** to `pending_get_block_proposals`. When the cap is reached, either reject new insertions or evict the oldest entries (e.g., using an LRU policy or a bounded `LinkedHashMap`).
2. **Per-peer accounting**: Track how many pending proposal IDs each peer has contributed and enforce a per-peer limit, disconnecting or banning peers that exceed it.
3. **Reduce the drain interval** or trigger a partial drain whenever the map exceeds a configurable threshold, rather than waiting for the next full timer tick.

---

### Proof of Concept

1. Establish a P2P connection to the target CKB node using the `RelayV3` protocol.
2. In a tight loop, construct `GetBlockProposal` messages each containing 3 000 unique, randomly generated `ProposalShortId` values (10 random bytes each) that are guaranteed not to exist in the node's tx-pool.
3. Send each message without ever sending a corresponding `BlockProposal` response.
4. Observe via `/proc/<pid>/status` or `jemalloc` metrics that the node's `VmRSS` grows monotonically between timer ticks.
5. After sufficient flooding the node process is killed by the OS OOM killer or becomes unresponsive, halting block relay and sync for all legitimate peers.

The relevant insertion site is: [2](#0-1) 

The unbounded map definition is: [1](#0-0) 

The only cleanup function (never triggered by the attacker's message pattern) is: [4](#0-3)

### Citations

**File:** sync/src/types/mod.rs (L1330-1330)
```rust
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

**File:** sync/src/types/mod.rs (L1586-1592)
```rust
    pub fn drain_get_block_proposals(
        &self,
    ) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
        let ret = self.pending_get_block_proposals.clone();
        self.pending_get_block_proposals.clear();
        ret
    }
```

**File:** sync/src/types/mod.rs (L1594-1601)
```rust
    pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
        for id in ids.into_iter() {
            self.pending_get_block_proposals
                .entry(id)
                .or_default()
                .insert(pi);
        }
    }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L36-44)
```rust
            // The block proposal request is separate from uncles,
            // so here the limit is only used to calculate the maximum value of uncles
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if message_len as u64 > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "GetBlockProposal proposals count({message_len}) > consensus max_block_proposals_limit({limit})"
                ));
            }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L67-77)
```rust
        // Transactions that do not exist on this node
        let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
            .into_iter()
            .filter(|short_id| !fetched_transactions.contains_key(short_id))
            .collect();

        // Cache request, try process on timer
        self.relayer
            .shared()
            .state()
            .insert_get_block_proposals(self.peer, not_exist_proposals);
```
