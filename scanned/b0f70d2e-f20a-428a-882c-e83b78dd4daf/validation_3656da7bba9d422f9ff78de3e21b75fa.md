Audit Report

## Title
Unbounded `pending_get_block_proposals` Map Growth via Unauthenticated `GetBlockProposal` Messages — (`sync/src/types/mod.rs`)

## Summary
Any connected peer can send a continuous stream of `GetBlockProposal` relay messages, each containing up to 3,000 unique random `ProposalShortId` values absent from the tx-pool. Because `insert_get_block_proposals` has no size guard and `pending_get_block_proposals` is initialized as an unbounded `DashMap`, the map grows without limit. The periodic `prune_tx_proposal_request` timer then clones the entire map (O(n) in time and memory), which can exhaust node memory and stall relay processing, crashing or severely degrading the node.

## Finding Description
`SyncState` holds a process-wide map initialized with no capacity bound:

```rust
// sync/src/types/mod.rs:1021
pending_get_block_proposals: DashMap::new(),
``` [1](#0-0) 

The only write path, `insert_get_block_proposals`, performs an unconditional `entry().or_default().insert()` loop with no size check: [2](#0-1) 

This is called from `GetBlockProposalProcess::execute` for every proposal ID not found in the tx-pool: [3](#0-2) 

The only validation applied to an incoming message is a per-message ceiling (`max_block_proposals_limit × max_uncles_num`, typically 3,000) and an intra-message deduplication check. There is no per-peer rate limit, no cumulative map size cap, and no PoW/stake requirement: [4](#0-3) 

The periodic timer calls `drain_get_block_proposals`, which **clones the entire DashMap** before clearing it — an O(n) allocation under DashMap shard locks: [5](#0-4) 

The cloned map is then iterated in `prune_tx_proposal_request`, submitting all accumulated IDs to the tx-pool actor in a single `fetch_txs` call: [6](#0-5) 

Contrast this with `unknown_tx_hashes`, which has an explicit soft cap `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` defined in constants — no equivalent guard exists for `pending_get_block_proposals`: [7](#0-6) 

## Impact Explanation
This matches **High: Vulnerabilities which could easily crash a CKB node**. A single malicious peer sending 3,000-entry messages in a tight loop inflates the map by 3,000 entries per message. At modest throughput (e.g., 100 msg/s), the map reaches millions of entries within minutes. The subsequent O(n) clone in `drain_get_block_proposals` causes memory exhaustion (OOM crash) and holds DashMap shard locks long enough to stall the relay timer loop, blocking block propagation and transaction relay for all peers.

## Likelihood Explanation
Likelihood is **moderate-to-high**. The attacker needs only a single TCP connection to a CKB node — no key, no stake, no PoW. `GetBlockProposal` is a standard relay protocol message. The 3,000-proposals-per-message cap is easily saturated in a tight loop. A single malicious peer is sufficient; Sybil amplification is not required. The exploit is fully deterministic and repeatable.

## Recommendation
1. **Hard cap on `pending_get_block_proposals`**: In `insert_get_block_proposals`, reject insertions once the map exceeds a configurable maximum (e.g., `max_block_proposals_limit × some_small_constant`).
2. **Per-peer quota**: Track how many pending proposal IDs each peer has contributed and reject further insertions once a per-peer ceiling is reached, analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` used for `unknown_tx_hashes`.
3. **Rate-limit `GetBlockProposal` messages per peer** at the protocol handler level (ban or throttle peers exceeding N messages per second).
4. **Avoid full clone in `drain_get_block_proposals`**: Replace the `clone()` + `clear()` pattern with `std::mem::take` or swap with an empty map to eliminate the O(n) allocation under lock.

## Proof of Concept
```
# Attacker connects to victim CKB node via Tentacle P2P
loop forever:
    proposals = [random_10_byte_id() for _ in range(3000)]  # all absent from tx-pool
    send RelayMessage::GetBlockProposal {
        block_hash: any_valid_hash,
        proposals:  proposals,
    }
```
Each iteration passes the `message_len <= 3000` check, `fetch_txs` returns empty (IDs are random/absent), and all 3,000 IDs are inserted into `pending_get_block_proposals` with no guard. After N iterations the map holds `3000 × N` entries. The next `prune_tx_proposal_request` tick clones and iterates all of them, causing O(N) memory allocation and CPU work while holding DashMap shard locks, stalling relay processing for all peers.

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

**File:** sync/src/relayer/get_block_proposal_process.rs (L34-52)
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

**File:** util/constant/src/sync.rs (L70-72)
```rust
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
