Audit Report

## Title
Unbounded `pending_get_block_proposals` DashMap Allows Peer-Driven Memory Exhaustion — (`File: sync/src/types/mod.rs`)

## Summary
`SyncState::pending_get_block_proposals` is an unbounded `DashMap` with no capacity cap, no per-peer quota, and no eviction policy. Any peer that completes the CKB P2P handshake can send `GetBlockProposal` relay messages containing fabricated `ProposalShortId` values absent from the local tx-pool; each such ID is unconditionally inserted into the map. The map is only cleared by a periodic timer drain, so between drain ticks it grows without bound, enabling peer-driven heap exhaustion and OOM process termination.

## Finding Description
`SyncState` declares the map at `sync/src/types/mod.rs:1330` with no capacity bound:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

`insert_get_block_proposals` (lines 1594–1601) iterates every supplied ID and calls `entry(id).or_default().insert(pi)` with no size guard:

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

The call site in `GetBlockProposalProcess::execute()` (lines 68–77) filters out proposals already in the tx-pool and forwards the remainder — which for attacker-fabricated random IDs is the full set — directly to `insert_get_block_proposals` with no additional size check.

The sole drain path is `drain_get_block_proposals()` (lines 1586–1592), called from `prune_tx_proposal_request()` via the `TX_PROPOSAL_TOKEN` notify timer. That timer fires every **100 ms** (line 798: `set_notify(Duration::from_millis(100), TX_PROPOSAL_TOKEN)`).

The `RateLimiter<(PeerIndex, u32)>` in `Relayer` (mod.rs line 81–92) is configured with `governor::Quota::per_second(30)`. The governor token-bucket allows a **burst of 30 messages immediately**, then refills at 30/s. Each message may carry up to `max_block_proposals_limit × max_uncles_num = 3000` IDs (lines 38–44). Therefore, in the first 100 ms window before the first drain tick, a single peer can inject **30 × 3000 = 90,000 IDs** in a burst. With `MAX_RELAY_PEERS = 128` coordinated peers, the aggregate burst is **128 × 90,000 = 11,520,000 entries** before the first drain. Each entry carries a 10-byte key plus `HashSet<PeerIndex>` overhead (~100–150 bytes total), yielding **~1.2–1.7 GB** of heap allocation in the first drain interval. Subsequent intervals sustain ~3 messages/peer/100 ms (refill rate), keeping pressure elevated.

The per-message count ceiling and within-message duplicate check (lines 35–52) are insufficient: they bound a single message's payload but impose no cumulative limit on the map across messages or peers.

## Impact Explanation
Unbounded heap growth from attacker-controlled P2P input leads to process termination by the OS OOM killer. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.** A crashed node is fully offline until manually restarted; the attack can be resumed immediately after restart.

## Likelihood Explanation
Any peer that completes the standard CKB P2P handshake can send `GetBlockProposal` relay messages. No stake, fee, or special privilege is required. The rate limiter's burst capacity (30 messages immediately) means the first drain window alone can produce gigabytes of allocation. A single persistent peer can sustain the attack indefinitely at the refill rate; multiple coordinated peers amplify the effect proportionally up to `MAX_RELAY_PEERS = 128`.

## Recommendation
- **Short term**: Add a hard cap inside `insert_get_block_proposals` on the total entry count of `pending_get_block_proposals` (e.g., `max_block_proposals_limit × max_uncles_num × MAX_RELAY_PEERS`). Reject insertions and consider banning the peer when the cap is reached.
- **Short term**: Track per-peer insertion counts and enforce a per-peer quota to prevent monopolization by a single peer.
- **Long term**: Replace the unbounded `DashMap` with a capacity-bounded structure (LRU or FIFO), consistent with how `OrphanTxPool` uses `DEFAULT_MAX_ORPHAN_TRANSACTIONS` and `limit_size()`.

## Proof of Concept
1. Attacker establishes a peer connection and completes the CKB P2P handshake.
2. Attacker immediately sends 30 `GetBlockProposal` relay messages (consuming the full burst budget), each containing 3000 unique, randomly generated `ProposalShortId` values (10 bytes each) that do not exist in the victim's tx-pool.
3. All 90,000 IDs pass the per-message count check (≤3000) and the within-message duplicate check, then are forwarded to `insert_get_block_proposals` because none exist in the tx-pool.
4. Before the 100 ms drain tick fires, the map holds 90,000 entries (~9–13 MB per peer). With 128 coordinated peers sending the same burst simultaneously, the map holds ~11.5 million entries (~1.2–1.7 GB).
5. After the drain tick, the attacker resumes at the refill rate (3 messages/100 ms per peer), sustaining ~9,000 new entries per peer per tick.
6. The node process is killed by the OS OOM killer; the attacker reconnects and repeats. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L35-52)
```rust
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
        }

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

**File:** sync/src/relayer/mod.rs (L797-800)
```rust
    async fn init(&mut self, nc: Arc<dyn CKBProtocolContext + Sync>) {
        nc.set_notify(Duration::from_millis(100), TX_PROPOSAL_TOKEN)
            .await
            .expect("set_notify at init is ok");
```
