### Title
Unbounded Growth of `pending_get_block_proposals` Causes Resource Exhaustion in `prune_tx_proposal_request` - (File: sync/src/relayer/mod.rs, sync/src/types/mod.rs)

### Summary
The `SyncState::pending_get_block_proposals` DashMap accumulates entries from any connected peer via `GetBlockProposalProcess::execute()` without any total-size cap. When the periodic `prune_tx_proposal_request` fires, it drains and iterates the entire map in one synchronous pass, forwarding all accumulated proposal IDs to the tx-pool service. An unprivileged peer can continuously inject large batches of non-existent proposal IDs, causing the map to grow without bound and making each timer tick increasingly expensive, ultimately degrading or blocking the tx-pool service.

### Finding Description

**Root cause — no total-size cap on `pending_get_block_proposals`**

`SyncState` declares the map with no capacity limit:

```rust
// sync/src/types/mod.rs line 1330
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
``` [1](#0-0) 

`insert_get_block_proposals` inserts every proposal ID from every peer with no guard on the total map size:

```rust
// sync/src/types/mod.rs lines 1594-1601
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
``` [2](#0-1) 

**Attacker-controlled growth path**

`GetBlockProposalProcess::execute()` is the network handler for the `GetBlockProposal` relay message. It applies only a per-message count check:

```rust
// sync/src/relayer/get_block_proposal_process.rs lines 38-44
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [3](#0-2) 

Proposals absent from the local tx pool are then unconditionally inserted into the map:

```rust
// sync/src/relayer/get_block_proposal_process.rs lines 68-77
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [4](#0-3) 

There is no validation that the proposals correspond to any known compact block, so an attacker can send arbitrary `ProposalShortId` values. With `max_block_proposals_limit = 1500` and `max_uncles_num = 2`, each message can inject up to 3 000 unique entries. Multiple peers sending repeated messages accumulate entries across timer intervals.

**Unbounded iteration at drain time**

`prune_tx_proposal_request`, called on a periodic timer, drains the entire map in one pass:

```rust
// sync/src/relayer/mod.rs lines 549-601
async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
    let get_block_proposals = self.shared().state().drain_get_block_proposals();
    let fetch_txs = tx_pool
        .fetch_txs(
            get_block_proposals
                .iter()
                .map(|kv_pair| kv_pair.key().clone())
                .collect(),   // entire map sent to tx-pool in one shot
        )
        .await;
    ...
    for (id, peer_indices) in get_block_proposals.into_iter() { ... }
}
``` [5](#0-4) 

`drain_get_block_proposals` clones and clears the map atomically but returns the full snapshot for processing: [6](#0-5) 

### Impact Explanation

- **Memory exhaustion**: the DashMap grows proportionally to the number of unique proposal IDs injected by all peers since the last timer tick. With many peers each sending maximum-size messages, this can reach millions of 10-byte entries plus associated `HashSet<PeerIndex>` allocations.
- **CPU exhaustion**: each timer tick iterates the entire drained map, performs a tx-pool lookup for every entry, and constructs per-peer response batches — all proportional to the unbounded map size.
- **Tx-pool service blocking**: `fetch_txs` sends a `HashSet<ProposalShortId>` of arbitrary size to the single-threaded tx-pool service and awaits a response. A very large set stalls the tx-pool service, delaying transaction admission and block assembly for all other callers.
- Net effect: sustained DOS of the relay and tx-pool subsystems without requiring any privileged access.

### Likelihood Explanation

Any connected peer can send `GetBlockProposal` messages. No authentication, stake, or special role is required. The per-message limit (`max_block_proposals_limit * max_uncles_num ≈ 3 000`) is generous, and there is no per-peer rate limit or global cap visible in the code. A single attacker with a handful of connections can continuously fill the map between timer ticks.

### Recommendation

1. **Cap the total size of `pending_get_block_proposals`**: enforce a global maximum (e.g., `MAX_INFLIGHT_PROPOSALS`) in `insert_get_block_proposals` and evict oldest or lowest-priority entries when the cap is reached.
2. **Validate proposals against known compact blocks**: only accept proposal IDs that appear in a compact block the node has already received and stored in `pending_compact_blocks`, rejecting unsolicited `GetBlockProposal` messages.
3. **Chunk the drain**: in `prune_tx_proposal_request`, process the drained map in bounded batches rather than forwarding the entire set to the tx-pool in one call.
4. **Per-peer rate limiting**: track how many proposals each peer has injected and disconnect or ban peers that exceed a threshold.

### Proof of Concept

1. Attacker opens N connections to the victim CKB node.
2. Each connection repeatedly sends `GetBlockProposal` relay messages containing 3 000 unique, non-existent `ProposalShortId` values (10 random bytes each).
3. Because none of these IDs exist in the victim's tx pool, all are inserted into `pending_get_block_proposals` via `insert_get_block_proposals` with no size check.
4. Between timer ticks the map accumulates `N × 3000 × tick_rate` entries.
5. On the next `prune_tx_proposal_request` tick, the node clones the entire map, sends all IDs to the tx-pool service in a single `fetch_txs` call, and iterates all entries — causing proportional CPU, memory, and tx-pool service latency spikes.
6. Sustained attack keeps the map perpetually large, degrading relay and tx-pool performance until the node becomes unresponsive.

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
