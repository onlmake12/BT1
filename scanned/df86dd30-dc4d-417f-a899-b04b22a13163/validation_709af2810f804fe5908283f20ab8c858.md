Audit Report

## Title
Unbounded `pending_get_block_proposals` Map Enables Memory Exhaustion and Relay DoS — (`sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

## Summary
The `SyncState::pending_get_block_proposals` `DashMap` has no size cap. Any connected peer can send `GetBlockProposal` relay messages containing fabricated `ProposalShortId` values that will never appear in the tx-pool, causing them to accumulate indefinitely. When the periodic `prune_tx_proposal_request` timer fires, it clones and drains the entire map in one unbounded O(n) operation, issuing a single bulk `fetch_txs` call to the tx-pool. This can exhaust memory and stall the relayer task for all peers.

## Finding Description

**No size cap on the map.** `insert_get_block_proposals` unconditionally inserts every ID from every `GetBlockProposal` message:

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
``` [1](#0-0) 

The map is initialized with no capacity bound: [2](#0-1) 

**Per-message check is insufficient.** `GetBlockProposalProcess::execute` only rejects a single message exceeding `max_block_proposals_limit × max_uncles_num` (≈ 3,000). It does not limit cumulative map size across messages: [3](#0-2) 

**Fabricated IDs are cached permanently until the next timer tick.** IDs absent from the tx-pool are inserted into the shared map: [4](#0-3) 

**Unbounded O(n) drain on every timer tick.** `drain_get_block_proposals` clones the entire map before clearing it: [5](#0-4) 

`prune_tx_proposal_request` then collects all keys into a single `HashSet` and issues one bulk `fetch_txs` call: [6](#0-5) 

**Partial mitigation — rate limiter exists but is insufficient.** A `RateLimiter<(PeerIndex, u32)>` keyed by `(peer, message_type)` is applied to all relay messages including `GetBlockProposal`, capped at 30 requests/second per peer: [7](#0-6) 

This limits a single peer to 30 × 3,000 = 90,000 entries/second. However, it does not prevent the attack — it only slows it. A single peer reaches 3,000,000 entries in ~33 seconds; with multiple peers (up to `MAX_RELAY_PEERS = 128`) the rate scales to ~11.5M entries/second: [8](#0-7) 

The rate limiter does not impose any cap on the map's total size, does not evict stale entries, and does not bound the drain operation.

## Impact Explanation

This matches **High: Vulnerabilities which could easily crash a CKB node.** A sustained attack from one or more peers causes:
1. Unbounded heap growth in `pending_get_block_proposals` (hundreds of MB to GB over minutes).
2. On each timer tick: an O(n) clone of the entire map, an O(n) `HashSet` construction, and a single oversized `fetch_txs` channel message to the tx-pool.
3. The relayer async task is blocked for the duration of the drain, delaying compact-block relay, transaction relay, and proposal processing for all peers.

The impact is node-local (not network-wide), placing it at High rather than Critical.

## Likelihood Explanation

A single TCP connection to the victim node is sufficient. The rate limiter at 30 `GetBlockProposal` messages/second per peer allows 90,000 fabricated entries/second per peer. No PoW, stake, or privileged role is required. The attack is repeatable and self-sustaining: because the fabricated IDs never appear in the tx-pool, they are re-inserted on every message and the map never shrinks between timer ticks. With multiple colluding peers the rate scales linearly.

## Recommendation

1. **Cap the map size.** In `insert_get_block_proposals`, reject new entries once `pending_get_block_proposals.len()` exceeds a configurable bound (e.g., `max_block_proposals_limit × max_connected_peers`).
2. **Per-peer quota.** Track how many pending entries each peer has contributed and refuse further insertions from peers that exceed their quota.
3. **Drain in bounded batches.** In `prune_tx_proposal_request`, process at most N entries per tick instead of draining the entire map at once.
4. **Evict stale entries.** Entries that have survived more than K timer ticks without being fulfilled should be dropped, preventing permanent accumulation from peers that have disconnected or are sending fabricated IDs.

## Proof of Concept

1. Attacker connects to a victim CKB node as a relay peer.
2. Attacker sends `GetBlockProposal` messages at the rate-limiter ceiling (30/s), each containing 3,000 unique fabricated `ProposalShortId` values (random 10-byte strings not present in the victim's tx-pool.
3. `GetBlockProposalProcess::execute` passes the per-message length check (≤ 3,000), calls `fetch_txs` (returns empty), and calls `insert_get_block_proposals` with all 3,000 IDs.
4. After ~33 seconds the `pending_get_block_proposals` map holds ~3,000,000 entries (~300 MB+ of heap).
5. When `prune_tx_proposal_request` fires, the node allocates a full clone of the map, sends a 3M-entry `fetch_txs` request to the tx-pool over an async channel, and iterates over all entries. The relayer task is blocked for the duration.
6. The attacker continues indefinitely; the victim's relay subsystem is permanently degraded and the node may OOM-crash.

### Citations

**File:** sync/src/types/mod.rs (L1021-1021)
```rust
            pending_get_block_proposals: DashMap::new(),
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

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L549-560)
```rust
    async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let get_block_proposals = self.shared().state().drain_get_block_proposals();
        let tx_pool = self.shared.shared().tx_pool_controller();

        let fetch_txs = tx_pool
            .fetch_txs(
                get_block_proposals
                    .iter()
                    .map(|kv_pair| kv_pair.key().clone())
                    .collect(),
            )
            .await;
```
