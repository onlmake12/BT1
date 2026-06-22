### Title
Unbounded `pending_get_block_proposals` DashMap Growth via Repeated `GetBlockProposal` P2P Messages — (`File: sync/src/types/mod.rs`)

---

### Summary

`SyncState.pending_get_block_proposals` is a `DashMap` with no capacity bound. An unprivileged peer can repeatedly send `GetBlockProposal` relay messages containing unique, non-existent proposal short IDs. Each message inserts up to `max_block_proposals_limit × max_uncles_num` entries into the map. Because no total-size guard exists and the map is only drained on a periodic timer, a fast-sending peer can grow the map without limit, exhausting node memory and causing a denial-of-service.

---

### Finding Description

`SyncState` declares `pending_get_block_proposals` as a plain, unbounded `DashMap`:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

It is initialised with no capacity limit:

```rust
pending_get_block_proposals: DashMap::new(),
``` [1](#0-0) [2](#0-1) 

The insertion path is `insert_get_block_proposals`, which performs no total-size check:

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

This function is called from `GetBlockProposalProcess::execute` after filtering out proposals that already exist in the tx-pool. The proposals that do **not** exist in the tx-pool — `not_exist_proposals` — are inserted unconditionally:

```rust
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();

self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [4](#0-3) 

The only per-message guard is a count check against `max_block_proposals_limit × max_uncles_num`:

```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [5](#0-4) 

This limits entries **per message**, not the **total accumulated** across many messages. The map is only emptied by `drain_get_block_proposals`, which is called inside the periodic `prune_tx_proposal_request` timer handler:

```rust
let get_block_proposals = self.shared().state().drain_get_block_proposals();
``` [6](#0-5) 

Between timer ticks, an attacker sending messages faster than the drain interval causes unbounded accumulation. The `ProposalShortId` type is 10 bytes, giving 2^80 unique values — effectively unlimited distinct keys.

---

### Impact Explanation

Each `ProposalShortId` key (10 bytes) plus its `HashSet<PeerIndex>` value occupies heap memory. With `max_block_proposals_limit` = 1 500 and `max_uncles_num` = 2, a single message can insert up to 3 000 new entries. Sending thousands of such messages per second causes the node's heap to grow without bound, ultimately triggering an OOM kill or severe memory pressure that degrades all node operations (block relay, tx-pool, RPC).

---

### Likelihood Explanation

The attack requires only a single connected peer. No PoW, no valid transactions, and no privileged role are needed. The attacker generates random 10-byte proposal IDs (guaranteed not to exist in the tx-pool) and sends `GetBlockProposal` relay messages in a tight loop. The per-message limit of ~3 000 entries is not a meaningful barrier when the attacker can send hundreds of messages per second over a persistent TCP connection.

---

### Recommendation

Add a total-size cap to `pending_get_block_proposals`. Before inserting in `insert_get_block_proposals`, check the current map length against a configurable maximum (e.g., `MAX_PENDING_GET_BLOCK_PROPOSALS`). If the limit is reached, either drop the incoming IDs or disconnect/penalise the offending peer. Alternatively, replace the plain `DashMap` with a bounded structure (e.g., an LRU map) so old entries are evicted automatically, mirroring the pattern already used for `pending_get_headers`:

```rust
pending_get_headers: RwLock::new(LruCache::new(GET_HEADERS_CACHE_SIZE)),
``` [7](#0-6) 

---

### Proof of Concept

1. Connect to a CKB node as a relay peer (standard P2P handshake).
2. In a tight loop, send `RelayMessage / GetBlockProposal` messages, each containing up to `max_block_proposals_limit × max_uncles_num` randomly generated `ProposalShortId` values (10 random bytes each) that are guaranteed not to exist in the target node's tx-pool.
3. Because `GetBlockProposalProcess::execute` calls `insert_get_block_proposals` with all IDs not found in the tx-pool, every random ID is inserted into `pending_get_block_proposals`.
4. The `drain_get_block_proposals` timer fires periodically but cannot keep pace with the flood; the `DashMap` grows without bound.
5. The node's heap is exhausted, causing OOM termination or severe performance degradation affecting all peers and RPC callers.

### Citations

**File:** sync/src/types/mod.rs (L1021-1021)
```rust
            pending_get_block_proposals: DashMap::new(),
```

**File:** sync/src/types/mod.rs (L1025-1025)
```rust
            pending_get_headers: RwLock::new(LruCache::new(GET_HEADERS_CACHE_SIZE)),
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

**File:** sync/src/relayer/get_block_proposal_process.rs (L34-45)
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

**File:** sync/src/relayer/mod.rs (L549-550)
```rust
    async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let get_block_proposals = self.shared().state().drain_get_block_proposals();
```
