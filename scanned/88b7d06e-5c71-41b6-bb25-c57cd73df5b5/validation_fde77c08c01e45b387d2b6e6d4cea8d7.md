Audit Report

## Title
Unbounded `pending_get_block_proposals` Map Allows Memory Exhaustion via Unauthenticated P2P Peer - (File: sync/src/types/mod.rs)

## Summary
`SyncState::pending_get_block_proposals` is a `DashMap` with no size cap, no per-peer quota, and no per-message rate limit. Any peer that completes the P2P handshake can flood the node with `GetBlockProposal` messages containing arbitrary proposal short IDs absent from the tx pool, causing the map to grow without bound between 100 ms drain cycles and exhausting node memory.

## Finding Description
`SyncState` declares the field with no capacity bound:

```rust
// sync/src/types/mod.rs L1330
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

`GetBlockProposalProcess::execute` enforces only a single per-message count guard (`max_block_proposals_limit * max_uncles_num`, typically 3 000 IDs). IDs absent from the tx pool are forwarded unconditionally to `insert_get_block_proposals`:

```rust
// sync/src/relayer/get_block_proposal_process.rs L68-77
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();
self.relayer.shared().state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
```

`insert_get_block_proposals` inserts every ID with no size check:

```rust
// sync/src/types/mod.rs L1594-1601
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
```

The map is only cleared by `prune_tx_proposal_request`, which is scheduled every 100 ms via `TX_PROPOSAL_TOKEN`. Between drain calls the map is entirely unbounded. There is no per-peer quota analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` used for `unknown_tx_hashes`, no message-rate limit on `GetBlockProposal`, and no misbehavior scoring or ban in `get_block_proposal_process.rs`. The network-layer search found no general message-rate limiting applicable to this path.

## Impact Explanation
A single malicious peer can send thousands of `GetBlockProposal` messages per 100 ms window, each carrying up to 3 000 unique 10-byte proposal short IDs absent from the tx pool. Every ID is inserted into the unbounded `DashMap`. Sustained for a few seconds, this exhausts available heap and OOM-kills or severely degrades the victim node, disrupting block relay, transaction propagation, and consensus participation. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."**

## Likelihood Explanation
Any peer completing the `RelayV3` handshake can send `GetBlockProposal` messages. No authentication, stake, or privilege is required — only a single TCP connection and the ability to generate random 10-byte IDs. The 100 ms drain window is wide enough for a high-throughput attacker to accumulate millions of entries before the map is cleared. The attack is repeatable and requires no victim interaction.

## Recommendation
1. Add a hard global size cap to `pending_get_block_proposals`; drop new entries or disconnect the offending peer when the cap is reached.
2. Enforce a per-peer quota inside `insert_get_block_proposals`, mirroring `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` used for `unknown_tx_hashes`.
3. Apply misbehavior scoring or a short ban in `GetBlockProposalProcess::execute` when a peer repeatedly sends IDs absent from the pool.
4. Optionally, record a timestamp per entry and evict stale entries during `prune_tx_proposal_request` rather than only draining the entire map.

## Proof of Concept
1. Connect to a CKB node as a relay peer (complete the `RelayV3` handshake).
2. In a tight loop, send `GetBlockProposal` messages where each message contains 3 000 randomly generated `ProposalShortId` values (10 random bytes each) guaranteed not to exist in the remote node's tx pool.
3. Because `GetBlockProposalProcess::execute` filters out IDs absent from the pool and passes them to `insert_get_block_proposals` without any size check, every ID is inserted into `pending_get_block_proposals`.
4. The map is only drained every 100 ms by `prune_tx_proposal_request`. Sending ~1 000 messages per 100 ms window inserts ~3 000 000 entries before the next drain, consuming hundreds of megabytes of memory per cycle.
5. Sustained for a few seconds, this exhausts available heap and crashes or severely degrades the victim node.