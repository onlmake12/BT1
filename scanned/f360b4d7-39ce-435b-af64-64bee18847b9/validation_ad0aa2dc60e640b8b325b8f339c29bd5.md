### Title
Unbounded `pending_get_block_proposals` Cache Enables Memory/CPU DoS via Repeated `GetBlockProposal` Relay Messages — (File: sync/src/types/mod.rs)

---

### Summary

`SyncState::pending_get_block_proposals` is a `DashMap` with no size cap. Any connected peer can flood the node with `GetBlockProposal` relay messages containing unique fake `ProposalShortId` values, causing the map to grow without bound. The periodic `prune_tx_proposal_request` timer then iterates the entire accumulated map, compounding CPU and memory exhaustion into a node-level DoS.

---

### Finding Description

**Root cause — unbounded insertion in `insert_get_block_proposals`:**

`SyncState` in `sync/src/types/mod.rs` holds:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

with no declared capacity limit. [1](#0-0) 

The insertion method is unconditional:

```rust
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
``` [2](#0-1) 

**Attacker-controlled entry path — `GetBlockProposalProcess::execute()`:**

This insertion is called from the relay message handler with `not_exist_proposals` — proposals that are absent from the local tx pool:

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
``` [3](#0-2) 

The only per-message guard is a size check on the incoming message:

```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [4](#0-3) 

With default consensus values (`max_block_proposals_limit = 1500`, `max_uncles_num = 2`), each message may carry up to **3,000 unique proposal IDs**. There is no rate limit on how many `GetBlockProposal` messages a peer may send, and no cap on the total size of `pending_get_block_proposals`. An attacker sends random 10-byte `ProposalShortId` values that will never match any tx-pool entry, so every ID passes the filter and is inserted.

**Unbounded iteration in `prune_tx_proposal_request`:**

The periodic timer drains and fully iterates the accumulated map:

```rust
async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
    let get_block_proposals = self.shared().state().drain_get_block_proposals();
    let tx_pool = self.shared.shared().tx_pool_controller();

    let fetch_txs = tx_pool
        .fetch_txs(
            get_block_proposals
                .iter()
                .map(|kv_pair| kv_pair.key().clone())
                .collect(),   // O(N) allocation
        )
        .await;
    ...
    for (id, peer_indices) in get_block_proposals.into_iter() {  // O(N) loop
        if let Some(tx) = txs.get(&id) { ... }
    }
}
``` [5](#0-4) 

This is O(N) CPU work and O(N) memory allocation where N is the number of accumulated entries — directly controlled by the attacker.

---

### Impact Explanation

**Impact: High**

- **Memory exhaustion**: Each `DashMap` entry holds a `ProposalShortId` (10 bytes) plus a `HashSet<PeerIndex>`. At 3,000 entries per message and 10,000 messages, the map holds 30 million entries, consuming several gigabytes of heap. The node crashes with OOM.
- **CPU exhaustion**: When `prune_tx_proposal_request` fires, it serialises all N keys into a `Vec`, sends them to the tx-pool actor, and iterates the entire map. With millions of entries this stalls the async relay task for seconds, blocking all relay processing.
- **Cascading effect**: The `fetch_txs` call is an async channel message to the tx-pool service. A massive payload can saturate the tx-pool's message queue, degrading transaction admission for all users.

The combined effect is a complete, sustained DoS of the relay subsystem and potential node crash — funds are not directly locked, but the node becomes unable to relay or validate transactions.

---

### Likelihood Explanation

**Likelihood: Medium**

- Any unprivileged peer that completes the CKB handshake can send `GetBlockProposal` relay messages.
- No authentication, stake, or special role is required.
- The attacker needs only a single TCP connection and the ability to send well-formed (but content-fake) relay messages.
- The attack is cheap: generating random 10-byte IDs costs negligible CPU on the attacker side.
- The only natural friction is the per-message proposal count cap (3,000), but sending thousands of messages per second is trivial.

---

### Recommendation

1. **Cap `pending_get_block_proposals`**: Enforce a maximum size (e.g., `max_block_proposals_limit × max_uncles_num × max_connected_peers`) and drop or evict oldest entries when the cap is reached.
2. **Per-peer rate limiting**: Track how many proposals each peer has contributed to `pending_get_block_proposals` and reject further insertions once a per-peer quota is exceeded.
3. **Replace with an LRU cache**: Use a bounded `LruCache` keyed by `ProposalShortId` so the map self-limits without explicit eviction logic.
4. **Validate proposal IDs against known blocks**: Only cache proposals that correspond to a block hash the node has recently seen, reducing the attack surface for fake IDs.

---

### Proof of Concept

```
1. Establish a peer connection to the target CKB node (standard P2P handshake).

2. In a tight loop, send GetBlockProposal relay messages:
   - Each message contains 3,000 unique random ProposalShortId values
     (10 random bytes each, guaranteed not to exist in any tx pool).
   - The per-message limit check passes (3,000 ≤ max_block_proposals_limit × max_uncles_num).

3. GetBlockProposalProcess::execute() calls tx_pool.fetch_txs() for each message.
   All 3,000 IDs are absent → all 3,000 are passed to insert_get_block_proposals().

4. After N messages, pending_get_block_proposals holds N × 3,000 entries with no eviction.

5. When prune_tx_proposal_request fires (periodic timer):
   - drain_get_block_proposals() clones and clears the map (O(N×3000) allocation).
   - fetch_txs() is called with N×3000 keys (large tx-pool message).
   - The for loop iterates all N×3000 entries (O(N×3000) CPU).

6. At N = 10,000 messages: 30,000,000 map entries → multi-GB heap → OOM crash,
   or multi-second relay stall → effective DoS for all connected peers.
```

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L35-44)
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

**File:** sync/src/relayer/mod.rs (L549-601)
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
        if let Err(err) = fetch_txs {
            debug_target!(
                crate::LOG_TARGET_RELAY,
                "relayer prune_tx_proposal_request internal error: {:?}",
                err,
            );
            return;
        }

        let txs = fetch_txs.unwrap();

        let mut peer_txs = HashMap::new();
        for (id, peer_indices) in get_block_proposals.into_iter() {
            if let Some(tx) = txs.get(&id) {
                for peer_index in peer_indices {
                    let tx_set = peer_txs.entry(peer_index).or_insert_with(Vec::new);
                    tx_set.push(tx.clone());
                }
            }
        }

        let mut relay_bytes = 0;
        let mut relay_proposals = Vec::new();
        for (peer_index, txs) in peer_txs {
            for tx in txs {
                let data = tx.data();
                let tx_size = data.total_size();
                if relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH {
                    send_block_proposals(nc, peer_index, std::mem::take(&mut relay_proposals))
                        .await;
                    relay_bytes = tx_size;
                } else {
                    relay_bytes += tx_size;
                }
                relay_proposals.push(data);
            }
            if !relay_proposals.is_empty() {
                send_block_proposals(nc, peer_index, std::mem::take(&mut relay_proposals)).await;
                relay_bytes = 0;
            }
        }
```
