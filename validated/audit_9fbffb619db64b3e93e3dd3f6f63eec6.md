### Title
Unbounded `pending_get_block_proposals` Growth via Unauthenticated `GetBlockProposal` Relay Messages - (File: `sync/src/types/mod.rs`)

---

### Summary

`SyncState::insert_get_block_proposals` inserts peer-supplied proposal IDs into the `pending_get_block_proposals` DashMap with no total-size guard. Any relay peer can send a continuous stream of `GetBlockProposal` messages carrying unique, non-existent proposal IDs, causing the map to grow without bound between periodic drain calls, leading to unbounded memory consumption and node DoS.

---

### Finding Description

`GetBlockProposalProcess::execute()` handles the `GetBlockProposal` relay P2P message. It enforces a per-message length cap:

```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit { ... reject ... }
```

After filtering out proposals already present in the tx pool, the remainder are cached:

```rust
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();

self.relayer.shared().state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
```

`insert_get_block_proposals` performs no size check before inserting:

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

`pending_get_block_proposals` is declared as an unbounded `DashMap`:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

initialized with no capacity limit:

```rust
pending_get_block_proposals: DashMap::new(),
```

The map is drained by `prune_tx_proposal_request` on a periodic timer, but there is no bound on how many entries can accumulate between drain calls. There is no per-peer quota, no total-map size cap, and no rate limit on `GetBlockProposal` messages at the relay protocol handler level.

A `ProposalShortId` is 10 bytes. The 2^80 possible values make the key space effectively inexhaustible for an attacker generating unique IDs. On mainnet, `max_block_proposals_limit = 1500` and `max_uncles_num = 2`, so each message can inject up to 3 000 new unique entries. Sending messages at network speed can grow the map by millions of entries per second before the drain timer fires.

---

### Impact Explanation

Unbounded growth of `pending_get_block_proposals` causes heap memory exhaustion on the victim node. As memory pressure increases, the OS will begin swapping, degrading node performance, and ultimately the process will be OOM-killed. This halts block relay, transaction propagation, and all RPC services, effectively taking the node offline. Because the attack requires only a single connected relay peer sending valid-format messages, it is a low-cost, high-impact denial-of-service.

---

### Likelihood Explanation

Any peer that completes the CKB relay protocol handshake can send `GetBlockProposal` messages. No stake, no key, and no special privilege is required. The per-message limit (≤ 3 000 proposals) is the only check; there is no per-connection rate limit, no per-peer quota on cached proposals, and no total-map size cap. A single attacker-controlled node can sustain the flood indefinitely.

---

### Recommendation

1. **Add a total-size cap** to `pending_get_block_proposals`. Reject or drop new insertions once the map exceeds a configurable maximum (e.g., `max_block_proposals_limit * MAX_RELAY_PEERS`).
2. **Add a per-peer quota** inside `insert_get_block_proposals`: count how many entries in the map already carry `pi` and refuse to insert more once the per-peer limit is reached.
3. **Rate-limit `GetBlockProposal` messages** at the relay protocol layer, analogous to the existing `MAX_RELAY_TXS_NUM_PER_BATCH` guard used elsewhere in the relayer.

---

### Proof of Concept

1. Connect to a target CKB full node as a relay peer (standard `RelayV3` handshake).
2. In a tight loop, construct `GetBlockProposal` messages each containing `max_block_proposals_limit * max_uncles_num` (≈ 3 000) unique `ProposalShortId` values that are not present in the node's tx pool (e.g., sequential byte strings).
3. Send each message over the relay connection.
4. Observe via `/proc/<pid>/status` or `get_tip_tx_pool_info` RPC that the node's RSS grows continuously; within seconds the node becomes unresponsive and is eventually OOM-killed.

**Root-cause call chain:**

```
P2P relay peer
  → Relayer::received() [sync/src/relayer/mod.rs]
    → GetBlockProposalProcess::execute() [sync/src/relayer/get_block_proposal_process.rs:32-97]
      → per-message length check passes (≤ 3 000)
      → tx_pool.fetch_txs() returns empty (IDs are fabricated)
      → SyncState::insert_get_block_proposals() [sync/src/types/mod.rs:1594-1601]
        → pending_get_block_proposals.entry(id).or_default().insert(pi)
          ← NO size limit, NO per-peer quota
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** sync/src/relayer/get_block_proposal_process.rs (L32-77)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
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

        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
        }

        let fetched_transactions = {
            let tx_pool = self.relayer.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(proposals.clone()).await;
            if let Err(e) = fetch_txs {
                debug_target!(
                    crate::LOG_TARGET_RELAY,
                    "relayer tx_pool_controller send fetch_txs error: {:?}",
                    e
                );
                return Status::ok();
            }
            fetch_txs.unwrap()
        };
        // Transactions that do not exist on this node
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

**File:** sync/src/types/mod.rs (L1016-1028)
```rust
        let state = SyncState {
            shared_best_header,
            tx_filter: Mutex::new(TtlFilter::default()),
            unknown_tx_hashes: Mutex::new(KeyedPriorityQueue::new()),
            peers: Peers::default(),
            pending_get_block_proposals: DashMap::new(),
            pending_compact_blocks: tokio::sync::Mutex::new(HashMap::default()),
            inflight_proposals: DashMap::new(),
            inflight_blocks: RwLock::new(InflightBlocks::default()),
            pending_get_headers: RwLock::new(LruCache::new(GET_HEADERS_CACHE_SIZE)),
            tx_relay_receiver,
            min_chain_work: sync_config.min_chain_work,
        };
```

**File:** sync/src/types/mod.rs (L1318-1336)
```rust
pub struct SyncState {
    /* Status irrelevant to peers */
    shared_best_header: RwLock<HeaderIndexView>,
    tx_filter: Mutex<TtlFilter<Byte32>>,

    // The priority is ordering by timestamp (reversed), means do not ask the tx before this timestamp (timeout).
    unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>,

    /* Status relevant to peers */
    peers: Peers,

    /* Cached items which we had received but not completely process */
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
    pending_get_headers: RwLock<LruCache<(PeerIndex, Byte32), Instant>>,
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,

    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,
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
