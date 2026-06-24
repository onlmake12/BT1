Audit Report

## Title
Unbounded `pending_get_block_proposals` State Inflation via Unauthenticated `GetBlockProposal` P2P Messages — (File: `sync/src/relayer/get_block_proposal_process.rs`)

## Summary
Any unprivileged P2P peer can send repeated `GetBlockProposal` messages containing crafted `ProposalShortId` values absent from the local tx pool. These IDs are unconditionally inserted into the shared `pending_get_block_proposals` DashMap with no per-peer or global size cap, allowing unbounded memory growth between periodic drain cycles. A single attacker peer can exhaust node memory and trigger an OOM crash.

## Finding Description
`GetBlockProposalProcess::execute()` applies two checks: a per-message count bound (`max_block_proposals_limit × max_uncles_num`) and an intra-message deduplication check. [1](#0-0)  Proposals not found in the tx pool are collected as `not_exist_proposals` and passed directly to `insert_get_block_proposals` with no further guard. [2](#0-1) 

`insert_get_block_proposals` performs no size check whatsoever — it unconditionally inserts every ID into the DashMap: [3](#0-2) 

`pending_get_block_proposals` is declared as a plain unbounded `DashMap` with no capacity limit. [4](#0-3) 

The only drain is `drain_get_block_proposals`, which clones and clears the entire map on a timer. Between drain cycles, an attacker can insert an arbitrary number of entries. [5](#0-4) 

By contrast, `add_ask_for_txs` enforces `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) and `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) and returns `StatusCode::TooManyUnknownTransactions` (triggering a peer ban) when exceeded. [6](#0-5)  No equivalent constant or guard exists for `pending_get_block_proposals`. [7](#0-6) 

**Exploit flow:**
1. Attacker connects as a standard P2P peer.
2. In a tight loop, sends `GetBlockProposal` messages each containing ~3,000 distinct, crafted `ProposalShortId` values not present in the victim's tx pool.
3. Each message passes the count check and the dedup check.
4. All ~3,000 IDs are inserted into `pending_get_block_proposals` per message with no rejection.
5. No ban or rate-limit is triggered by this code path.
6. The map grows without bound until the next drain cycle, which itself becomes increasingly expensive as the map grows.

## Impact Explanation
`pending_get_block_proposals` is an `Arc`-shared `DashMap` held in `SyncState`. Unbounded growth causes memory exhaustion: each `ProposalShortId` key is 10 bytes; with `HashSet<PeerIndex>` overhead, each entry costs ~100–200 bytes. At 3,000 proposals per message and a high message rate, the map can consume gigabytes of RAM, crashing the node via OOM. This matches: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attacker requires only a standard P2P connection — no keys, stake, or special role. The insertion path is deterministic: any crafted ID absent from the pool will always be inserted. No existing check bans or throttles a peer for inflating this map. The attack is fully reproducible and sustainable indefinitely, as `ProposalShortId` is 10 bytes giving 2^80 possible distinct values.

## Recommendation
Apply the same guard pattern used in `add_ask_for_txs`:
1. Define constants `MAX_PENDING_GET_BLOCK_PROPOSALS_SIZE` and `MAX_PENDING_GET_BLOCK_PROPOSALS_SIZE_PER_PEER` in `util/constant/src/sync.rs`.
2. In `insert_get_block_proposals` (or in `GetBlockProposalProcess::execute()` before calling it), check the current map size and the per-peer contribution after insertion.
3. If either limit is exceeded, return a `ProtocolMessageIsMalformed`-equivalent status and ban the peer for `BAD_MESSAGE_BAN_TIME`, mirroring the `TooManyUnknownTransactions` path in `add_ask_for_txs`.

## Proof of Concept
```
1. Connect to a CKB mainnet/testnet node as a standard P2P peer.
2. In a tight loop, send RelayV3 GetBlockProposal messages:
     block_hash = <any valid tip hash>
     proposals  = [random 10-byte ProposalShortId × 3000]  // none exist in pool
3. Each message passes:
     - count check  (3000 ≤ max_block_proposals_limit × max_uncles_num)
     - dedup check  (all IDs distinct within the message)
4. All 3000 IDs are inserted into pending_get_block_proposals per message.
5. Monitor node RSS: it grows by ~300–600 KB per message with no bound.
6. No ban is issued; the loop continues until the node OOMs or the drain
   cycle catches up (which itself becomes expensive at large map sizes).
```

### Citations

**File:** sync/src/relayer/get_block_proposal_process.rs (L38-52)
```rust
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

**File:** sync/src/types/mod.rs (L1330-1330)
```rust
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

**File:** sync/src/types/mod.rs (L1507-1528)
```rust
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );

            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
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

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
