### Title
Unbounded `pending_get_block_proposals` Map Growth via Unauthenticated Peer Messages Causes Memory/CPU Exhaustion - (File: sync/src/types/mod.rs)

---

### Summary

Any unauthenticated peer can repeatedly send `GetBlockProposal` relay messages containing unique, non-existent proposal IDs. Each such message causes entries to be inserted into the `pending_get_block_proposals` `DashMap` in `SyncState` without any cap on the map's total size. Over many messages the map grows without bound, consuming unbounded memory and causing O(n) CPU cost when the map is cloned and drained on the relay timer.

---

### Finding Description

`SyncState` in `sync/src/types/mod.rs` holds a `DashMap` for caching unanswered proposal requests from peers:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
``` [1](#0-0) 

The insertion path is `insert_get_block_proposals`, which appends every supplied ID unconditionally:

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

This function is called from `GetBlockProposalProcess::execute()` for every proposal ID that is **not** currently in the local tx-pool:

```rust
// Cache request, try process on timer
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [3](#0-2) 

The only guard in `GetBlockProposalProcess::execute()` is a **per-message** size check:

```rust
let limit = shared.consensus().max_block_proposals_limit()
    * (shared.consensus().max_uncles_num() as u64);
if message_len as u64 > limit {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [4](#0-3) 

On mainnet `max_block_proposals_limit = 1500` and `max_uncles_num = 2`, so each message may carry up to **3 000** proposal IDs. There is no rate limit on how many such messages a peer may send, and no cap on the cumulative size of `pending_get_block_proposals`.

When the relay timer fires, `drain_get_block_proposals` **clones the entire map** before clearing it:

```rust
pub fn drain_get_block_proposals(
    &self,
) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
    let ret = self.pending_get_block_proposals.clone();
    self.pending_get_block_proposals.clear();
    ret
}
``` [5](#0-4) 

Cloning a `DashMap` with millions of entries is O(n) in both time and memory, and requires acquiring locks on every internal shard, stalling all concurrent readers/writers of the map.

---

### Impact Explanation

A single malicious peer can:

1. Connect to a victim CKB node over the standard relay protocol.
2. Repeatedly send `GetBlockProposal` messages, each containing up to 3 000 unique, fabricated `ProposalShortId` values that do not exist in the node's tx-pool.
3. Because none of the IDs are found in the tx-pool, all of them are inserted into `pending_get_block_proposals` on every message.
4. After sending N messages the map holds up to 3 000 × N entries.
5. On the next relay timer tick, `drain_get_block_proposals` clones the entire inflated map, causing:
   - **Memory exhaustion**: each entry is ~10 bytes key + `HashSet` overhead; millions of entries consume hundreds of MB to GB.
   - **CPU/latency spike**: the O(n) clone and subsequent iteration over the drained map stalls the relay service, delaying legitimate block-proposal processing and transaction propagation.

Sustained flooding can render the relay subsystem unresponsive, degrading block propagation and transaction relay for the victim node.

---

### Likelihood Explanation

- The `GetBlockProposal` message is part of the standard unauthenticated relay protocol (`RelayV3`); any peer that completes the handshake can send it.
- No rate limiting, connection-level quota, or map-size cap exists for this path.
- Fabricating unique 10-byte `ProposalShortId` values is trivial.
- The CHANGELOG already records analogous fixes for `inflight_proposals` (`#3093`) and `inflight_blocks` (`#3094`) memory bloat, confirming the pattern is known and exploitable; `pending_get_block_proposals` was not addressed by those fixes. [6](#0-5) 

---

### Recommendation

1. **Cap the map size**: in `insert_get_block_proposals`, refuse to insert new entries once `pending_get_block_proposals` exceeds a configurable maximum (e.g., `max_block_proposals_limit * max_uncles_num * MAX_PEERS`).
2. **Per-peer quota**: track how many pending entries each peer has contributed and reject further insertions once a per-peer limit is reached, disconnecting or banning peers that exceed it.
3. **Avoid full clone on drain**: instead of cloning the entire `DashMap`, swap it with a fresh empty map or drain it incrementally to avoid the O(n) allocation under lock.

---

### Proof of Concept

```
Attacker peer  →  victim node (RelayV3 protocol)

loop forever:
    ids = [random_10_byte_id() for _ in range(3000)]   # unique, non-existent
    msg = GetBlockProposal { block_hash: tip_hash, proposals: ids }
    send(msg)   # passes the per-message limit check (3000 ≤ 3000)
    # victim inserts all 3000 IDs into pending_get_block_proposals
    # no rate limit → repeat immediately

After N iterations:
    pending_get_block_proposals.len() == 3000 * N

On next relay timer tick:
    drain_get_block_proposals() clones 3000*N-entry DashMap
    → O(N) memory allocation + O(N) CPU under shard locks
    → relay timer stalls; block-proposal and tx propagation delayed
```

Entry point: `GetBlockProposalProcess::execute` in `sync/src/relayer/get_block_proposal_process.rs`, reachable from any unauthenticated relay peer via the `RelayV3` protocol handler. [7](#0-6)

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

**File:** CHANGELOG.md (L779-781)
```markdown
- #3094: Fix inflight block potential memory bloat issues (@driftluo)
- #3093: Resolve inflight proposals memory bloat issue (@quake)
- #3110: Fix pending compact block memory bloat on abnormal flow (@driftluo)
```
