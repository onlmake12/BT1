### Title
Unbounded Memory Growth in `pending_get_block_proposals` DashMap via Repeated `GetBlockProposal` Messages — (`sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

### Summary

An unprivileged relay peer can cause unbounded heap growth in the `pending_get_block_proposals` DashMap by sending repeated `GetBlockProposal` messages containing unique, non-existent proposal IDs before the `prune_tx_proposal_request` timer drains the map. The rate limiter provides partial mitigation but does not prevent the attack because the per-message payload can contain thousands of unique IDs.

### Finding Description

The attack path is:

1. **Entry point:** Any peer connected via the relay protocol sends `GetBlockProposal` messages.

2. **Per-message size check** in `GetBlockProposalProcess::execute()` only validates that a single message does not exceed `max_block_proposals_limit * max_uncles_num` IDs: [1](#0-0) 

3. **Intra-message deduplication** only rejects duplicates *within* a single message: [2](#0-1) 

4. **Uncapped insertion:** Proposals not found in the tx pool are inserted into `pending_get_block_proposals` with no size cap: [3](#0-2) 

5. **`insert_get_block_proposals` has no bound check:** [4](#0-3) 

6. **The DashMap is only drained by the timer:** [5](#0-4) 

7. **Rate limiter exists but is insufficient:** The `Relayer` applies a 30 req/sec per-peer per-message-type rate limit: [6](#0-5) [7](#0-6) 

   This limits *message frequency* but not the total number of unique proposal IDs inserted per timer interval. With mainnet defaults (`max_block_proposals_limit = 1500`, `max_uncles_num = 2`), each message can carry up to **3,000 unique IDs**. At 30 msg/sec, a single peer can insert **~90,000 entries per second** into the DashMap.

8. **`pending_get_block_proposals` field is an unbounded DashMap:** [8](#0-7) 

### Impact Explanation

Between timer ticks (approximately every second), a single peer can insert ~90,000 `ProposalShortId → HashSet<PeerIndex>` entries. With `MAX_RELAY_PEERS = 128` peers each sending at the rate limit: [9](#0-8) 

The DashMap can accumulate tens of millions of entries per timer interval, consuming gigabytes of heap memory. This causes **OOM / node crash**, a denial-of-service against the victim node. The attacker only needs to send fake `ProposalShortId` values (10-byte arbitrary values) that are not in the tx pool — no PoW, no valid transactions, no privileged access required.

### Likelihood Explanation

The attack is straightforward: connect as a relay peer, send `GetBlockProposal` messages at 30 rps with 3,000 unique random 10-byte proposal IDs per message. The IDs will not be in the tx pool, so all are forwarded to `insert_get_block_proposals`. No consensus validity, PoW, or authentication is required. The only barrier is the 30 rps rate limit, which is insufficient given the large per-message payload.

### Recommendation

- Enforce a **global size cap** on `pending_get_block_proposals` (e.g., reject or evict entries when the map exceeds a configurable maximum, such as `max_block_proposals_limit * max_uncles_num`).
- Alternatively, enforce a **per-peer cap** on how many pending proposal IDs can be queued at once.
- Consider reducing the per-message limit or adding a per-peer per-interval byte/entry budget that accounts for the DashMap accumulation, not just message frequency.

### Proof of Concept

```
1. Connect to a CKB node as a relay peer (RelayV3 protocol).
2. In a loop (≤30 iterations/second to stay under rate limit):
   a. Generate 3000 random 10-byte ProposalShortIds not in the tx pool.
   b. Send GetBlockProposal { block_hash: tip_hash, proposals: [3000 random IDs] }.
3. Before the prune_tx_proposal_request timer fires (~1 second):
   - pending_get_block_proposals.len() grows by ~3000 per message.
   - After 30 messages: ~90,000 entries in the DashMap.
4. With 128 peers doing the same: ~11.5M entries (~1+ GB heap) per timer interval.
5. Sustained attack causes OOM and node crash.
```

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
