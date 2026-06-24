Audit Report

## Title
Unbounded Heap Growth via Repeated `GetBlockProposal` Messages in `pending_get_block_proposals` DashMap — (`sync/src/relayer/get_block_proposal_process.rs`, `sync/src/types/mod.rs`)

## Summary

Any relay peer can send `GetBlockProposal` messages containing up to 3,000 unique, non-existent `ProposalShortId` values per message. These IDs are unconditionally inserted into the unbounded `pending_get_block_proposals` DashMap with no per-peer or global size cap. The only drain is a periodic timer, so between ticks a single peer can accumulate ~90,000 entries/sec; with 128 peers the map can reach tens of millions of entries, exhausting heap memory and crashing the node.

## Finding Description

**Step 1 — Per-message size check is the only guard:**
`GetBlockProposalProcess::execute()` rejects messages exceeding `max_block_proposals_limit * max_uncles_num` IDs (mainnet: 1500 × 2 = 3,000), but imposes no cross-message or cumulative limit. [1](#0-0) 

**Step 2 — Intra-message deduplication only:**
The `HashSet` deduplication at L47–52 only rejects duplicates *within* a single message; repeated unique IDs across successive messages are not deduplicated. [2](#0-1) 

**Step 3 — All non-pool IDs are unconditionally inserted:**
IDs absent from the tx pool are passed directly to `insert_get_block_proposals` with no size check. [3](#0-2) 

**Step 4 — `insert_get_block_proposals` is unbounded:**
The function iterates and inserts every ID into the DashMap with no cap or eviction policy. [4](#0-3) 

**Step 5 — `pending_get_block_proposals` is an unbounded DashMap:**
The field has no capacity limit. [5](#0-4) 

**Step 6 — The only drain is the `prune_tx_proposal_request` timer:**
`drain_get_block_proposals` is called exclusively from this timer handler; between ticks the map grows without bound. [6](#0-5) 

**Step 7 — Rate limiter limits message frequency, not entry volume:**
The 30 req/sec per-peer rate limit does not bound the number of unique IDs inserted. At 30 msg/sec × 3,000 IDs/msg = 90,000 entries/sec per peer. [7](#0-6) [8](#0-7) 

## Impact Explanation

A single attacker-controlled peer can insert ~90,000 `ProposalShortId → HashSet<PeerIndex>` entries per second into the DashMap. With `MAX_RELAY_PEERS = 128` peers each operating at the rate limit, the map accumulates ~11.5 million entries per timer interval. [9](#0-8) 

Each DashMap entry carries heap overhead well beyond the 10-byte key, causing gigabytes of heap growth within seconds of sustained attack. This results in OOM and node crash. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation

The attack requires only a standard relay peer connection — no PoW, no valid transactions, no authentication, and no privileged access. The attacker generates random 10-byte `ProposalShortId` values that are guaranteed not to be in the tx pool. The 30 rps rate limit is the only barrier, and it is insufficient given the 3,000-ID-per-message payload. The attack is repeatable, automatable, and requires minimal resources from the attacker.

## Recommendation

- Enforce a **global size cap** on `pending_get_block_proposals` (e.g., reject insertions when the map exceeds `max_block_proposals_limit * max_uncles_num` total entries).
- Alternatively, enforce a **per-peer cap** inside `insert_get_block_proposals`, tracking how many pending IDs each peer has queued and rejecting further insertions once the per-peer limit is reached.
- Consider reducing the per-message ID limit or introducing a per-peer per-interval entry budget that accounts for DashMap accumulation rate, not just message frequency.

## Proof of Concept

1. Connect to a CKB node as a relay peer (RelayV3 protocol).
2. In a loop at ≤30 iterations/second:
   - Generate 3,000 random 10-byte `ProposalShortId` values not present in the tx pool.
   - Send `GetBlockProposal { block_hash: tip_hash, proposals: [3000 random IDs] }`.
3. Observe `pending_get_block_proposals.len()` growing by ~3,000 per message.
4. After ~1 second (before `prune_tx_proposal_request` fires): ~90,000 entries from one peer.
5. With 128 peers doing the same: ~11.5M entries per timer interval, consuming gigabytes of heap.
6. Sustained attack causes OOM and node crash.

### Citations

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-52)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
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

**File:** sync/src/relayer/mod.rs (L59-59)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
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

**File:** sync/src/relayer/mod.rs (L549-551)
```rust
    async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let get_block_proposals = self.shared().state().drain_get_block_proposals();
        let tx_pool = self.shared.shared().tx_pool_controller();
```
