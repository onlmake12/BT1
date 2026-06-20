### Title
Unbounded `pending_get_block_proposals` DashMap Enables O(n) Work Amplification via `GetBlockProposal` Flooding - (File: `sync/src/types/mod.rs`, `sync/src/relayer/mod.rs`)

---

### Summary

`SyncState::pending_get_block_proposals` is a `DashMap<ProposalShortId, HashSet<PeerIndex>>` with no total-size cap. Any connected peer can send `GetBlockProposal` relay messages containing non-existent proposal IDs; those IDs are inserted into the map without bound. When the relayer's periodic timer fires `prune_tx_proposal_request()`, it drains the entire map and issues a single `fetch_txs()` call to the tx-pool with all accumulated IDs, performing O(n) work proportional to the total number of attacker-inserted entries. This is the direct CKB analog of the Nomad `improperUpdate()` / `Queue.contains()` O(n) scan vulnerability: an unprivileged network peer can inflate the collection and force expensive linear work on the victim node.

---

### Finding Description

**Root cause — unbounded insertion path**

In `sync/src/types/mod.rs`, `SyncState` declares:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
``` [1](#0-0) 

It is initialized with no capacity limit:

```rust
pending_get_block_proposals: DashMap::new(),
``` [2](#0-1) 

The insertion function `insert_get_block_proposals` appends every supplied ID unconditionally:

```rust
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
``` [3](#0-2) 

**Attacker-controlled entry path**

`GetBlockProposalProcess::execute()` in `sync/src/relayer/get_block_proposal_process.rs` accepts a `GetBlockProposal` relay message from any connected peer. It enforces a per-message limit of `max_block_proposals_limit * max_uncles_num` IDs (≈ 1500 × 2 = 3000 on mainnet), then looks up which IDs already exist in the tx pool. IDs that are **not** found in the pool are forwarded to `insert_get_block_proposals`:

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
``` [4](#0-3) 

Because `ProposalShortId` is 10 bytes (2^80 possible values), an attacker can trivially generate an unbounded stream of unique IDs that will never match any real tx-pool entry, causing every ID to be inserted into the map.

**O(n) drain on timer**

`prune_tx_proposal_request()` is called on the relayer's periodic timer. It drains the entire map in one shot and issues a single `fetch_txs()` call with **all** accumulated IDs:

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
    ...
    for (id, peer_indices) in get_block_proposals.into_iter() {
        if let Some(tx) = txs.get(&id) { ... }
    }
}
``` [5](#0-4) 

`drain_get_block_proposals()` clones and clears the map atomically, but the subsequent `fetch_txs()` call acquires the tx-pool lock and performs a lookup for every accumulated ID:

```rust
pub fn drain_get_block_proposals(
    &self,
) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
    let ret = self.pending_get_block_proposals.clone();
    self.pending_get_block_proposals.clear();
    ret
}
``` [6](#0-5) 

The work done per timer tick is O(n) in the number of unique IDs accumulated since the last tick. There is no cap on this number.

---

### Impact Explanation

- **CPU exhaustion**: Each timer tick, the node performs O(n) tx-pool lookups and O(n) iteration over the drained map. With many peers each sending many messages between ticks, n can be made arbitrarily large.
- **Tx-pool lock contention**: `fetch_txs()` holds the tx-pool read lock for the duration of all n lookups, blocking concurrent tx submission and block assembly (`get_block_template`) for the entire duration.
- **Memory pressure**: The `DashMap` and the cloned copy in `drain_get_block_proposals` both hold n entries simultaneously, each 10 bytes for the key plus overhead.
- **Availability degradation**: A sustained flood can cause the relayer timer loop to spend most of its time in `prune_tx_proposal_request`, delaying all other relay duties (compact block reconstruction, tx broadcasting).

Severity: **High** — reachable by any unprivileged connected peer, no PoW or stake required.

---

### Likelihood Explanation

- Any peer that completes the P2P handshake can send `GetBlockProposal` messages.
- The per-message limit of ~3000 IDs is enforced, but there is no rate limit on how many messages a peer can send per second, and no cap on the total map size across all peers.
- A single attacker with one connection can send thousands of messages per second, each with 3000 unique random IDs, filling the map with millions of entries before the next timer tick.
- The attack requires no special knowledge of the chain state; random 10-byte values are sufficient since they will never match real tx-pool entries.

---

### Recommendation

1. **Cap the total size of `pending_get_block_proposals`**: Enforce a hard upper bound (e.g., `max_block_proposals_limit * max_uncles_num * max_peers`) on the total number of entries in the map. Reject or evict entries when the cap is reached.
2. **Per-peer quota**: Track how many pending proposal IDs each peer has contributed and reject insertions that exceed a per-peer limit, analogous to how `OrphanPool` enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS`.
3. **Rate-limit `GetBlockProposal` messages per peer**: Apply a message-rate limit at the protocol handler level before any map insertion occurs.
4. **Batch `fetch_txs` calls**: Instead of issuing one `fetch_txs` call with all n IDs, process the drained map in fixed-size batches to bound the per-tick work and lock-hold duration.

---

### Proof of Concept

1. Connect to a CKB node as a peer using the `RelayV3` protocol.
2. In a tight loop, send `GetBlockProposal` messages where `proposals` contains 3000 random 10-byte IDs (guaranteed not to exist in the tx pool):
   ```
   for i in 0..∞:
       ids = [random_bytes(10) for _ in range(3000)]
       send GetBlockProposal { block_hash: tip_hash, proposals: ids }
   ```
3. Between timer ticks (typically every few hundred milliseconds), the `pending_get_block_proposals` map accumulates millions of entries.
4. When `prune_tx_proposal_request()` fires, the node calls `fetch_txs()` with all accumulated IDs, holding the tx-pool lock for an extended period and consuming significant CPU.
5. Observable effect: `get_block_template` RPC calls stall or time out; tx submission latency spikes; the relayer timer loop falls behind.

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
