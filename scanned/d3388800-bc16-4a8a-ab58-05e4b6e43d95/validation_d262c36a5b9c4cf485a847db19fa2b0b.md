### Title
Unbounded `pending_get_block_proposals` Map Allows Memory Exhaustion via Unauthenticated P2P Peer - (File: sync/src/types/mod.rs)

### Summary
`SyncState::pending_get_block_proposals` is a `DashMap` with no size cap or per-entry expiration. Any connected peer can flood the node with `GetBlockProposal` messages containing arbitrary proposal short IDs that do not exist in the tx pool, causing the map to grow without bound between periodic drain calls, exhausting node memory.

### Finding Description
`SyncState` in `sync/src/types/mod.rs` declares `pending_get_block_proposals` as a plain `DashMap<packed::ProposalShortId, HashSet<PeerIndex>>` with no capacity limit:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

When a peer sends a `GetBlockProposal` P2P message, `GetBlockProposalProcess::execute` in `sync/src/relayer/get_block_proposal_process.rs` fetches matching transactions from the tx pool. Any proposal IDs that are **not** found in the pool are forwarded to `insert_get_block_proposals`:

```rust
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();

self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
```

`insert_get_block_proposals` inserts every ID unconditionally:

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

The map is drained by `prune_tx_proposal_request`, which is scheduled via `TX_PROPOSAL_TOKEN` every **100 ms**:

```rust
nc.set_notify(Duration::from_millis(100), TX_PROPOSAL_TOKEN)
```

```rust
TX_PROPOSAL_TOKEN => self.prune_tx_proposal_request(&nc).await,
```

Between drain calls the map is unbounded. The only per-message guard is a count check against `max_block_proposals_limit * max_uncles_num` (1 500 × 2 = 3 000 IDs per message). There is no limit on the number of messages a peer may send, no per-peer quota inside `pending_get_block_proposals`, and no expiration on individual entries.

### Impact Explanation
A single malicious peer can send thousands of `GetBlockProposal` messages per 100 ms window, each carrying up to 3 000 unique 10-byte proposal short IDs that are absent from the tx pool. Every ID is inserted into the unbounded `DashMap`. At 3 000 entries per message and no rate limit, the map can reach millions of entries within seconds, consuming gigabytes of heap memory and causing the node process to be OOM-killed or become unresponsive. This disrupts block relay, transaction propagation, and consensus participation for the victim node.

### Likelihood Explanation
Any peer that completes the P2P handshake can send `GetBlockProposal` messages. No authentication, stake, or privilege is required. The attack requires only a single TCP connection and the ability to generate arbitrary 10-byte proposal short IDs. The 100 ms drain window is wide enough for a high-throughput attacker to accumulate millions of entries before the map is cleared.

### Recommendation
Add a hard size cap to `pending_get_block_proposals`. When the cap is reached, either drop new entries or disconnect the offending peer. A per-peer quota (similar to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` used for `unknown_tx_hashes`) should also be enforced inside `insert_get_block_proposals`. Optionally, record a timestamp per entry and evict stale entries during `prune_tx_proposal_request` rather than only draining the entire map.

### Proof of Concept

1. Connect to a CKB node as a relay peer (complete the `RelayV3` handshake).
2. In a tight loop, send `GetBlockProposal` messages where each message contains 3 000 randomly generated `ProposalShortId` values (10 random bytes each) that are guaranteed not to exist in the remote node's tx pool.
3. Because `GetBlockProposalProcess::execute` filters out IDs absent from the pool and passes them to `insert_get_block_proposals` without any size check, every ID is inserted into `pending_get_block_proposals`.
4. The map is only drained every 100 ms by `prune_tx_proposal_request`. Sending ~1 000 messages per 100 ms window inserts ~3 000 000 entries before the next drain, consuming hundreds of megabytes of memory per cycle.
5. Sustained for a few seconds, this exhausts available heap and crashes or severely degrades the victim node.

**Relevant code locations:**

- `SyncState` field declaration: [1](#0-0) 
- `insert_get_block_proposals` (no size check): [2](#0-1) 
- `GetBlockProposalProcess::execute` — unconditional insertion of missing proposals: [3](#0-2) 
- Per-message count guard (only check present): [4](#0-3) 
- `prune_tx_proposal_request` drain (called every 100 ms): [5](#0-4) 
- Timer registration (100 ms interval): [6](#0-5)

### Citations

**File:** sync/src/types/mod.rs (L1330-1330)
```rust
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
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

**File:** sync/src/relayer/mod.rs (L549-551)
```rust
    async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let get_block_proposals = self.shared().state().drain_get_block_proposals();
        let tx_pool = self.shared.shared().tx_pool_controller();
```

**File:** sync/src/relayer/mod.rs (L798-800)
```rust
        nc.set_notify(Duration::from_millis(100), TX_PROPOSAL_TOKEN)
            .await
            .expect("set_notify at init is ok");
```
