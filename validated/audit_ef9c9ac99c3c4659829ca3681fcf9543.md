### Title
Unbounded Growth of `pending_get_block_proposals` via Malicious Peer `GetBlockProposal` Flooding — (`File: sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

---

### Summary

An unprivileged connected peer can repeatedly send `GetBlockProposal` relay messages containing fake `ProposalShortId` values that do not exist in the local tx-pool. Each such message causes up to `max_block_proposals_limit × max_uncles_num` (1,500 × 2 = 3,000) entries to be inserted into the `pending_get_block_proposals` `DashMap` with no per-map size cap. The map is only drained on a periodic timer, so between drains the attacker can grow it without bound, exhausting node memory and degrading or crashing the node.

---

### Finding Description

`SyncState` holds a shared map:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
``` [1](#0-0) 

This map is populated by `insert_get_block_proposals`, which performs no size check:

```rust
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
``` [2](#0-1) 

`insert_get_block_proposals` is called from `GetBlockProposalProcess::execute()` for every proposal ID in the incoming message that is **not** found in the local tx-pool:

```rust
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();

// Cache request, try process on timer
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [3](#0-2) 

The only guard in `execute()` is a per-message count check:

```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed...;
}
``` [4](#0-3) 

`MAX_BLOCK_PROPOSALS_LIMIT` is 1,500 and `max_uncles_num` is 2, giving a per-message ceiling of 3,000 IDs. [5](#0-4) 

The map is only cleared by `drain_get_block_proposals`, which is called inside `prune_tx_proposal_request` on a periodic relay timer:

```rust
pub fn drain_get_block_proposals(
    &self,
) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
    let ret = self.pending_get_block_proposals.clone();
    self.pending_get_block_proposals.clear();
    ret
}
``` [6](#0-5) 

There is no cap on how many entries can accumulate between timer fires. A malicious peer can send messages faster than the drain rate, growing the map without bound.

The CKB CHANGELOG records that analogous unbounded-growth bugs were fixed for `inflight_proposals` (#3093) and `pending_compact_blocks` (#3110) in v0.101.0, but `pending_get_block_proposals` does not appear to have received a corresponding fix. [7](#0-6) 

---

### Impact Explanation

A single malicious peer can continuously send `GetBlockProposal` messages, each carrying up to 3,000 unique fake `ProposalShortId` values (10 bytes each). Between periodic drains the map accumulates millions of entries. At scale this exhausts heap memory, causing the node to slow down or crash (OOM). Because the relay protocol imposes no per-peer rate limit on `GetBlockProposal` messages, the attack requires only a single connected peer and no stake or PoW.

---

### Likelihood Explanation

Any peer that successfully connects to a CKB node can send relay messages. The `GetBlockProposal` message type is part of the standard relay protocol (`RelayV2`/`RelayV3`). No special privilege, key, or majority hashpower is required. The attacker only needs to generate distinct 10-byte `ProposalShortId` values (trivially done by varying transaction content) and send them at a rate exceeding the drain timer. This is a low-cost, low-skill attack.

---

### Recommendation

Add a hard cap on the total number of entries in `pending_get_block_proposals`. When the cap is reached, either reject new insertions or evict the oldest entries (e.g., using an LRU structure). A reasonable bound is `max_block_proposals_limit × max_uncles_num` (3,000), matching the per-message limit already enforced in `GetBlockProposalProcess`. Additionally, consider per-peer rate limiting on `GetBlockProposal` messages in the relay handler.

---

### Proof of Concept

1. Connect a custom peer to a CKB node using the relay protocol.
2. In a tight loop, send `GetBlockProposal` messages each containing 3,000 unique `ProposalShortId` values that do not correspond to any transaction in the node's tx-pool (e.g., random 10-byte values).
3. Observe that `pending_get_block_proposals` grows continuously between timer drains.
4. After sufficient iterations (dependent on available RAM), the node's memory is exhausted and it crashes or becomes unresponsive.

The per-message check at `get_block_proposal_process.rs:38–44` passes because each message is within the 3,000-ID limit; the unbounded accumulation occurs across messages with no inter-message cap.

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L38-44)
```rust
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if message_len as u64 > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "GetBlockProposal proposals count({message_len}) > consensus max_block_proposals_limit({limit})"
                ));
            }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L68-77)
```rust
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

**File:** spec/src/consensus.rs (L86-89)
```rust
/// The default maximum allowed amount of proposals for a block
///
/// Default value from 1.5 * TWO_IN_TWO_OUT_COUNT
pub const MAX_BLOCK_PROPOSALS_LIMIT: u64 = 1_500;
```

**File:** CHANGELOG.md (L779-781)
```markdown
- #3094: Fix inflight block potential memory bloat issues (@driftluo)
- #3093: Resolve inflight proposals memory bloat issue (@quake)
- #3110: Fix pending compact block memory bloat on abnormal flow (@driftluo)
```
