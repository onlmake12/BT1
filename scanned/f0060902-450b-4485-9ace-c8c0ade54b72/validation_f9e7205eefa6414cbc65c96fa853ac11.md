Audit Report

## Title
Unbounded `pending_get_block_proposals` Map Allows Memory Exhaustion via Unauthenticated P2P Peer - (File: sync/src/types/mod.rs)

## Summary
`SyncState::pending_get_block_proposals` is a `DashMap` with no size cap, no per-peer quota, and no entry expiration. Any peer that completes the P2P handshake can flood the node with `GetBlockProposal` messages containing arbitrary proposal short IDs absent from the tx pool, causing the map to grow without bound between 100 ms drain cycles and exhausting node memory.

## Finding Description
`SyncState` declares `pending_get_block_proposals` as a plain, unbounded `DashMap`: [1](#0-0) 

`GetBlockProposalProcess::execute` applies only a single per-message count guard: [2](#0-1) 

Proposal IDs not found in the tx pool are forwarded unconditionally to `insert_get_block_proposals`: [3](#0-2) 

`insert_get_block_proposals` inserts every ID with no size check: [4](#0-3) 

The map is only drained by `prune_tx_proposal_request`, called every 100 ms: [5](#0-4) [6](#0-5) 

There is no per-peer quota analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (which caps `unknown_tx_hashes` per peer): [7](#0-6) 

Between drain calls, an attacker can insert millions of 10-byte `ProposalShortId` entries into the map with no enforcement.

## Impact Explanation
A single malicious peer can send thousands of `GetBlockProposal` messages per 100 ms window, each carrying up to `max_block_proposals_limit * max_uncles_num` unique proposal short IDs absent from the tx pool. Every ID is inserted into the unbounded `DashMap`. Sustained for a few seconds, this exhausts heap memory and OOM-kills or severely degrades the victim node, matching the **High** impact class: "Vulnerabilities which could easily crash a CKB node" (10001–15000 points).

## Likelihood Explanation
Any peer completing the `RelayV3` handshake can send `GetBlockProposal` messages. No authentication, stake, or privilege is required. The attack requires only a single TCP connection and the ability to generate arbitrary 10-byte proposal short IDs. The 100 ms drain window is wide enough for a high-throughput attacker to accumulate millions of entries before the map is cleared.

## Recommendation
Add a hard size cap to `pending_get_block_proposals`. When the cap is reached, drop new entries or disconnect the offending peer. Enforce a per-peer quota inside `insert_get_block_proposals` (analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` used for `unknown_tx_hashes`). Optionally, record a timestamp per entry and evict stale entries during `prune_tx_proposal_request` rather than only draining the entire map.

## Proof of Concept
1. Connect to a CKB node as a relay peer (complete the `RelayV3` handshake).
2. In a tight loop, send `GetBlockProposal` messages where each message contains up to `max_block_proposals_limit * max_uncles_num` randomly generated `ProposalShortId` values (10 random bytes each) guaranteed not to exist in the remote node's tx pool.
3. `GetBlockProposalProcess::execute` filters out IDs absent from the pool and passes them to `insert_get_block_proposals` without any size check, so every ID is inserted into `pending_get_block_proposals`.
4. The map is only drained every 100 ms by `prune_tx_proposal_request`. Sending ~1 000 messages per 100 ms window inserts ~3 000 000 entries before the next drain, consuming hundreds of megabytes of memory per cycle.
5. Sustained for a few seconds, this exhausts available heap and crashes or severely degrades the victim node.

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

**File:** util/constant/src/sync.rs (L70-72)
```rust
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
