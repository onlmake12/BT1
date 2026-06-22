### Title
Unbounded `pending_get_block_proposals` Map Allows Any Peer to Cause Expensive Batch Processing DoS - (`sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

---

### Summary

The `SyncState::pending_get_block_proposals` map in `sync/src/types/mod.rs` has no size cap. Any connected peer can send repeated `GetBlockProposal` P2P messages containing up to 3,000 fabricated `ProposalShortId` values each. These accumulate in the shared map without eviction. When the periodic `prune_tx_proposal_request` timer fires, it drains the entire map in one shot — cloning it, collecting all IDs into a single `HashSet`, and issuing one bulk `fetch_txs` call to the tx-pool. A single malicious peer can make this batch arbitrarily large, causing memory exhaustion and CPU/channel saturation on every timer tick.

---

### Finding Description

**Root cause — no size limit on `pending_get_block_proposals`:**

`insert_get_block_proposals` unconditionally inserts every proposal ID from every `GetBlockProposal` message into the shared `DashMap`:

```rust
// sync/src/types/mod.rs  lines 1594-1601
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
``` [1](#0-0) 

There is no check on the current map size, no per-peer quota, and no eviction policy.

**Per-message limit is insufficient:**

`GetBlockProposalProcess::execute` only rejects a single message if it exceeds `max_block_proposals_limit × max_uncles_num` (1 500 × 2 = 3 000). It does not limit the cumulative size of the shared map across messages or peers:

```rust
// sync/src/relayer/get_block_proposal_process.rs  lines 38-44
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [2](#0-1) 

Proposals that are absent from the local tx-pool are cached for later retry:

```rust
// sync/src/relayer/get_block_proposal_process.rs  lines 68-77
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();
// Cache request, try process on timer
self.relayer.shared().state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [3](#0-2) 

Because the attacker sends fabricated `ProposalShortId` values that will never appear in the tx-pool, every entry lands in `pending_get_block_proposals` and stays there until the next timer tick.

**Expensive all-at-once drain on every timer tick:**

`prune_tx_proposal_request` clones the entire map, collects every key into a single `HashSet`, and issues one bulk `fetch_txs` call:

```rust
// sync/src/relayer/mod.rs  lines 549-560
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
``` [4](#0-3) 

`drain_get_block_proposals` clones the entire map before clearing it — an O(n) heap allocation:

```rust
// sync/src/types/mod.rs  lines 1586-1592
pub fn drain_get_block_proposals(
    &self,
) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
    let ret = self.pending_get_block_proposals.clone();
    self.pending_get_block_proposals.clear();
    ret
}
``` [5](#0-4) 

The `SyncState` struct confirms the map is initialized with no capacity bound:

```rust
// sync/src/types/mod.rs  lines 1021
pending_get_block_proposals: DashMap::new(),
``` [6](#0-5) 

---

### Impact Explanation

On every `prune_tx_proposal_request` timer tick the node must:
1. Clone the entire bloated map — O(n) heap allocation, potentially hundreds of MB.
2. Collect all keys into a `HashSet` and send them to the tx-pool over an async channel — O(n) serialization and channel pressure.
3. Iterate over all entries to dispatch `BlockProposal` responses — O(n) work even when all lookups return empty.

If the map is large enough, the timer handler stalls the relayer task, delaying or blocking compact-block relay, transaction relay, and proposal processing for all peers. Repeated timer firings against a permanently large map constitute a sustained DoS of the relay subsystem.

---

### Likelihood Explanation

A single connected peer can send `GetBlockProposal` messages at network speed. Each message may carry up to 3,000 fabricated `ProposalShortId` values (10 random bytes each). Sending 1,000 such messages — trivially achievable in seconds — injects 3,000,000 entries. No PoW, no stake, no privileged role, and no Sybil attack is required. The attacker only needs one TCP connection to the victim node.

---

### Recommendation

1. **Cap the map size.** In `insert_get_block_proposals`, reject or drop new entries once `pending_get_block_proposals.len()` exceeds a reasonable bound (e.g., `max_block_proposals_limit × max_connected_peers`).
2. **Per-peer quota.** Track how many pending entries each peer has contributed and refuse further insertions from peers that exceed their quota.
3. **Drain in bounded batches.** In `prune_tx_proposal_request`, process at most N entries per tick instead of draining the entire map at once.
4. **Rate-limit `GetBlockProposal` at the protocol layer.** Disconnect or penalise peers that send this message more than K times per second.

---

### Proof of Concept

1. Attacker connects to a victim CKB node as a relay peer.
2. Attacker sends a stream of `GetBlockProposal` messages, each containing 3,000 unique fabricated `ProposalShortId` values (random 10-byte strings). None of these exist in the victim's tx-pool.
3. `GetBlockProposalProcess::execute` passes the per-message length check (≤ 3,000), calls `fetch_txs` (returns empty), and calls `insert_get_block_proposals` with all 3,000 IDs.
4. After sending ~1,000 messages the `pending_get_block_proposals` map holds ~3,000,000 entries.
5. When `prune_tx_proposal_request` fires, the node allocates hundreds of MB to clone the map, sends a 3M-entry `fetch_txs` request to the tx-pool, and iterates over all entries. The relayer task is blocked for the duration.
6. The attacker repeats continuously; the victim's relay subsystem is permanently degraded.

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
