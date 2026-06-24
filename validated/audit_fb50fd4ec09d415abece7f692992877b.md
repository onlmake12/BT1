Audit Report

## Title
Unbounded `pending_get_block_proposals` Growth via `GetBlockProposal` Message Flooding — (`sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

## Summary
`SyncState::pending_get_block_proposals` is a `DashMap` with no capacity bound. Any relay peer can send `GetBlockProposal` messages containing `ProposalShortId`s absent from the tx pool, causing unconditional insertion into the map. Because the map is only cleared on a periodic timer drain and no size cap exists, an attacker can accumulate a large number of entries between drain ticks, leading to memory exhaustion and potential node crash.

## Finding Description
`pending_get_block_proposals` is declared and initialized with no capacity limit: [1](#0-0) [2](#0-1) 

`insert_get_block_proposals` performs no size check before inserting: [3](#0-2) 

In `GetBlockProposalProcess::execute`, every short ID absent from the tx pool is passed unconditionally to `insert_get_block_proposals`: [4](#0-3) 

The only per-message guard is a count ceiling (`max_block_proposals_limit * max_uncles_num`, ≈3000 on mainnet), not a map-size ceiling: [5](#0-4) 

The drain is timer-based and performs a full `DashMap::clone()` before clearing — it does not bound the map size: [6](#0-5) 

**Note on the rate limiter:** The claim incorrectly states there is no per-peer rate limit. A `RateLimiter<(PeerIndex, u32)>` keyed by `(peer, message_type)` is present and applies to `GetBlockProposal` at 30 req/sec per peer: [7](#0-6) [8](#0-7) 

However, this rate limiter does **not** eliminate the vulnerability. At 30 req/sec × 3000 IDs/req = 90,000 new map entries per second per peer. With up to `MAX_RELAY_PEERS = 128` peers, the insertion rate reaches ~11.5 million entries/sec. The map remains unbounded and no size cap prevents accumulation between drain ticks. [9](#0-8) 

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.**

Memory exhaustion from unbounded map growth can exhaust heap memory and crash the node process. Additionally, `drain_get_block_proposals` performs a full `DashMap::clone()` of the accumulated map, causing a CPU and allocation spike at drain time. The subsequent `fetch_txs` call sends all accumulated IDs to the tx pool in a single batch, potentially stalling the tx pool service: [10](#0-9) 

## Likelihood Explanation
An attacker needs only a single unprivileged P2P relay connection. Fabricating `ProposalShortId`s absent from the pool is trivial (10 bytes each). The rate limiter caps each peer at 30 req/sec, but multiple concurrent peers multiply the insertion rate linearly. The attack is repeatable and requires no special privileges, PoW, or stake.

## Recommendation
1. **Cap `pending_get_block_proposals`** at a fixed maximum size (e.g., `max_block_proposals_limit * max_uncles_num * max_connected_peers`) and silently drop inserts that would exceed it.
2. **Replace the full-clone drain** with `std::mem::take` or a swap to avoid the O(n) clone cost at drain time.
3. **Consider lowering the per-peer rate limit** for `GetBlockProposal` specifically, since the current 30 req/sec still allows 90,000 entries/sec per peer.

## Proof of Concept
```
1. Connect to victim as a relay peer (SupportProtocols::RelayV3).
2. From a single peer, send GetBlockProposal messages at the rate limit
   (30/sec), each containing ~3000 distinct ProposalShortIds absent
   from the victim's tx pool.
3. Between two prune_tx_proposal_request timer ticks, observe
   pending_get_block_proposals growing by ~90,000 entries/sec.
4. With 128 concurrent attacker peers, observe ~11.5M entries/sec growth.
5. At drain time, observe CPU spike from DashMap::clone() and large
   fetch_txs batch sent to the tx pool service.
6. Monitor RSS of the ckb process for unbounded heap growth.
```

### Citations

**File:** sync/src/types/mod.rs (L1021-1021)
```rust
            pending_get_block_proposals: DashMap::new(),
```

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

**File:** sync/src/relayer/mod.rs (L59-59)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
```

**File:** sync/src/relayer/mod.rs (L81-92)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
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
