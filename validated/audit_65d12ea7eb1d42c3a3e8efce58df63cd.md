### Title
Unbounded Peer-Controlled `insert_get_block_proposals` Cache Enables Relayer Timer DoS — (`sync/src/relayer/get_block_proposal_process.rs`)

### Summary

Any connected peer can repeatedly send `GetBlockProposal` P2P messages containing proposal short IDs that do not exist in the local tx-pool. Each message passes the per-message size check but appends its full payload to an in-memory cache via `insert_get_block_proposals`. Because only a per-message cap is enforced — not a total or per-peer cap on the accumulated cache — a peer can inflate this structure without bound. Every subsequent relayer timer tick must iterate over the entire accumulated cache to re-check and dispatch responses, causing O(n) CPU work per tick where n is fully attacker-controlled.

### Finding Description

In `sync/src/relayer/get_block_proposal_process.rs`, the `execute` method performs the following steps:

**Step 1 — Per-message size gate (lines 38–44):**
```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed ...
}
```
This rejects any single message that exceeds `max_block_proposals_limit × max_uncles_num` (≈ 1 500 × 2 = 3 000 on mainnet). It does **not** limit how many such messages a peer may send over time.

**Step 2 — Filter to proposals absent from the local tx-pool (lines 68–71):**
```rust
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();
```
An attacker trivially satisfies this filter by using random or fabricated proposal short IDs that will never appear in the local pool.

**Step 3 — Unconditional cache insertion (lines 74–77):**
```rust
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
```
There is no guard on the total accumulated size of this cache — neither globally nor per peer. Each message from the attacker appends up to 3 000 entries. Sending *k* messages injects up to 3 000 × k entries.

The relayer timer subsequently iterates over every cached entry to re-query the tx-pool and dispatch `BlockProposal` responses. That iteration is O(total cached entries), which is entirely attacker-controlled.

### Impact Explanation

A single malicious peer can drive the relayer timer loop to consume arbitrarily large amounts of CPU on every tick. Because the timer runs continuously, the node's relay thread is degraded proportionally to the cache size. At sufficient scale this starves legitimate compact-block relay, delays transaction propagation, and can render the node effectively unable to participate in block relay — a sustained, peer-triggered DoS with no mining power required.

### Likelihood Explanation

The attack requires only a standard P2P connection (no keys, no stake, no mining). The attacker's cost is network bandwidth to send repeated `GetBlockProposal` messages; the victim pays CPU on every timer tick for as long as the cache remains inflated. The per-message cap of 3 000 entries means each message is cheap to send but contributes a non-trivial iteration burden. The absence of any rate-limit or total-cache-size eviction policy makes the attack sustainable.

### Recommendation

1. **Cap the total cache size** — enforce a global or per-peer upper bound on the number of entries held in the `insert_get_block_proposals` cache; evict oldest entries (LRU) or reject new insertions once the cap is reached.
2. **Rate-limit `GetBlockProposal` messages per peer** — count messages per peer per time window and disconnect or penalise peers that exceed the threshold.
3. **Evict stale entries** — entries for proposal IDs that have not appeared in the tx-pool after N timer ticks should be dropped rather than re-checked indefinitely.

### Proof of Concept

1. Establish a standard P2P connection to a target CKB node.
2. In a tight loop, send `GetBlockProposal` messages each containing 3 000 randomly generated `ProposalShortId` values (all absent from the target's tx-pool). Each message passes the per-message size check.
3. After *k* iterations, the `insert_get_block_proposals` cache holds up to 3 000 × k entries.
4. Observe that on every relayer timer tick the node iterates over all 3 000 × k entries, performing a tx-pool lookup for each. CPU usage on the relay thread scales linearly with k.
5. Sustain the message stream to keep the cache inflated; the node's block-relay throughput degrades proportionally. [1](#0-0) [2](#0-1)

### Citations

**File:** sync/src/relayer/get_block_proposal_process.rs (L34-44)
```rust
        let message_len = self.message.proposals().len();
        {
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
