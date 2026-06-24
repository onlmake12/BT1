Audit Report

## Title
Unbounded `pending_get_block_proposals` State Inflation via Unauthenticated `GetBlockProposal` P2P Messages — (File: `sync/src/relayer/get_block_proposal_process.rs`)

## Summary
Any unprivileged P2P peer can send repeated `GetBlockProposal` messages containing crafted `ProposalShortId` values absent from the local tx pool. These IDs are unconditionally inserted into the shared `pending_get_block_proposals` DashMap with no per-peer or global size cap, allowing unbounded memory growth between periodic drain cycles. A single attacker peer can exhaust node memory and trigger an OOM crash.

## Finding Description
`GetBlockProposalProcess::execute()` in `sync/src/relayer/get_block_proposal_process.rs` (lines 32–77) applies two checks: a per-message count bound (`max_block_proposals_limit × max_uncles_num`, ≈3,000 on mainnet) and an intra-message deduplication check. Proposals not found in the tx pool are collected as `not_exist_proposals` and passed directly to `insert_get_block_proposals` with no further guard.

`insert_get_block_proposals` in `sync/src/types/mod.rs` (lines 1594–1601) performs no size check:

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

`pending_get_block_proposals` is declared as a plain unbounded `DashMap` (line 1330). The only drain is `drain_get_block_proposals` (lines 1586–1592), which clones and clears the entire map on a timer. Between drain cycles, an attacker can insert an arbitrary number of entries.

By contrast, `add_ask_for_txs` (lines 1483–1532) enforces `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) and `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) and returns `StatusCode::TooManyUnknownTransactions` (triggering a peer ban) when exceeded. No equivalent guard exists for `pending_get_block_proposals`, and no constant `MAX_PENDING_GET_BLOCK_PROPOSALS_SIZE` is defined anywhere in the codebase.

**Exploit flow:**
1. Attacker connects as a standard P2P peer.
2. In a tight loop, sends `GetBlockProposal` messages each containing ≈3,000 distinct, crafted `ProposalShortId` values not present in the victim's tx pool.
3. Each message passes the count check (3,000 ≤ limit) and the dedup check (all IDs distinct within the message).
4. All ≈3,000 IDs are inserted into `pending_get_block_proposals` per message with no rejection.
5. No ban or rate-limit is triggered by this code path.
6. The map grows without bound until the next drain cycle, which itself becomes increasingly expensive as the map grows.

## Impact Explanation
`pending_get_block_proposals` is an `Arc`-shared `DashMap` held in `SyncState`, accessible to all relay and sync handlers. Unbounded growth causes memory exhaustion: each `ProposalShortId` key is 10 bytes; with `HashSet<PeerIndex>` overhead, each entry costs ~100–200 bytes. At 3,000 proposals per message and a high message rate, the map can consume gigabytes of RAM, crashing the node via OOM. Additionally, the periodic drain clones and clears the entire map, making the drain itself a stall point as map size grows. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attacker requires only a standard P2P connection — no keys, stake, or special role. The P2P layer imposes a per-message size limit but no per-message-type rate limit for `GetBlockProposal`. The insertion path is deterministic: any crafted ID absent from the pool will always be inserted. No existing check bans or throttles a peer for inflating this map. The attack is fully reproducible and sustainable indefinitely.

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