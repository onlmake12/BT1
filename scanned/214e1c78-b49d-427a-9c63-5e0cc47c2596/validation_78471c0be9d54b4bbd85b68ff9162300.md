### Title
Unbounded `pending_get_block_proposals` DashMap Growth via Malicious `GetBlockProposal` P2P Messages — (File: `sync/src/types/mod.rs`)

### Summary
A malicious unprivileged peer can repeatedly send `GetBlockProposal` relay messages containing up to 3,000 fake `ProposalShortId` values per message. For each ID not found in the local tx-pool, the node unconditionally inserts the ID into the `pending_get_block_proposals` DashMap with no size cap. This map is only cleared by a periodic drain timer. With multiple colluding peers, the map can grow to hundreds of megabytes between drain cycles, causing sustained memory pressure and degraded node performance — a direct analog to the "Junk Proposals" resource-exhaustion pattern.

---

### Finding Description

`SyncState` holds an unbounded `DashMap`:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
``` [1](#0-0) 

It is initialized with no capacity limit:

```rust
pending_get_block_proposals: DashMap::new(),
``` [2](#0-1) 

When a peer sends a `GetBlockProposal` message, `GetBlockProposalProcess::execute()` checks only that the count does not exceed `max_block_proposals_limit * max_uncles_num` (1,500 × 2 = 3,000):

```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit { ... }
``` [3](#0-2) 

For every proposal ID not found in the tx-pool, the handler calls `insert_get_block_proposals` with no size guard:

```rust
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [4](#0-3) 

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
``` [5](#0-4) 

The only relief valve is `drain_get_block_proposals`, which **clones the entire map** before clearing it — an O(n) allocation:

```rust
pub fn drain_get_block_proposals(
    &self,
) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
    let ret = self.pending_get_block_proposals.clone();
    self.pending_get_block_proposals.clear();
    ret
}
``` [6](#0-5) 

This drain is triggered only by the relay timer, not on every message. Between timer ticks, the map accumulates without bound.

The relay-level rate limiter caps each peer at 30 messages/second:

```rust
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
``` [7](#0-6) 

But this is a **per-peer** cap. With `k` colluding peers, the insertion rate is `k × 30 × 3,000 = 90,000k` entries/second. Each `ProposalShortId` key is 10 bytes; each `HashSet<PeerIndex>` value adds overhead. At 100 peers the map can accumulate ~9 million entries (~162 MB) per second before the drain fires.

---

### Impact Explanation

- **Memory exhaustion**: The node's heap grows proportionally to the number of colluding peers and the drain interval. Sustained attack causes OOM or severe swap pressure, crashing or stalling the node.
- **Drain amplification**: When the drain fires, the O(n) clone of a large map causes a latency spike in the relay timer loop, delaying processing of legitimate compact blocks and transactions.
- **No persistent cost to attacker**: Fake `ProposalShortId` values (10 random bytes) require no tokens, no PoW, and no valid transaction. The attacker only needs a TCP connection to the victim.

---

### Likelihood Explanation

- Any unprivileged peer can send `GetBlockProposal` messages; no authentication or stake is required.
- The per-peer rate limit (30 req/s) is easily saturated by a single connection and is multiplied by the number of Sybil peers an attacker controls.
- The attack is cheap: generating 3,000 random 10-byte IDs per message costs negligible CPU.
- The CHANGELOG already records two prior memory-bloat fixes in this subsystem (`#3093 Resolve inflight proposals memory bloat issue`, `#3094 Fix inflight block potential memory bloat issues`), confirming this class of bug is realistic and has been exploited before. [8](#0-7) 

---

### Recommendation

1. **Cap `pending_get_block_proposals`**: Enforce a maximum entry count (e.g., `max_block_proposals_limit × max_peers`). Reject or drop insertions once the cap is reached.
2. **Per-peer sub-map limit**: Track how many pending IDs each peer has contributed and evict the oldest entries from that peer when its quota is exceeded.
3. **Avoid full clone in drain**: Replace the clone-then-clear pattern with `std::mem::take` or a swap to avoid the O(n) allocation spike.
4. **Reduce drain interval or make it event-driven**: Drain more frequently so the window of accumulation is smaller.

---

### Proof of Concept

```
1. Establish N TCP connections to the victim node (N = number of Sybil peers).
2. For each peer, in a tight loop (≤30 msg/s to stay under rate limit):
   a. Generate 3,000 random 10-byte ProposalShortId values.
   b. Build a GetBlockProposal message with a valid block_hash (any known tip hash)
      and the 3,000 fake proposal IDs.
   c. Send the message over the RelayV3 protocol.
3. None of the fake IDs exist in the victim's tx-pool, so all 3,000 are inserted
   into pending_get_block_proposals on every message.
4. Between relay timer ticks, the DashMap grows by N × 30 × 3,000 entries.
5. Monitor victim RSS; it grows linearly with N and time until OOM or drain fires.
6. When drain fires, the O(n) clone causes a measurable relay-timer latency spike.
```

The per-message count limit of 3,000 is enforced: [9](#0-8) 

but there is no total-map size limit anywhere in the insertion path: [5](#0-4)

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L74-77)
```rust
        self.relayer
            .shared()
            .state()
            .insert_get_block_proposals(self.peer, not_exist_proposals);
```

**File:** sync/src/relayer/mod.rs (L91-92)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** CHANGELOG.md (L779-781)
```markdown
- #3094: Fix inflight block potential memory bloat issues (@driftluo)
- #3093: Resolve inflight proposals memory bloat issue (@quake)
- #3110: Fix pending compact block memory bloat on abnormal flow (@driftluo)
```
