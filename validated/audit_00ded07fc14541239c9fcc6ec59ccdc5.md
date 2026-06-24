Audit Report

## Title
Unbounded `short_ids` Count Enables Memory Exhaustion via Crafted `CompactBlock` - (File: `sync/src/relayer/compact_block_process.rs`, `sync/src/relayer/mod.rs`)

## Summary

The `non_contextual_check` function validates uncles and proposals counts against consensus limits but imposes no upper bound on `compact_block.short_ids().len()`. A malicious unauthenticated peer can craft a `CompactBlock` message with up to ~800,000 short IDs (filling the 8 MB decompression limit), causing `reconstruct_block` to allocate ~35 MB of heap structures per message before any transaction-root check is performed. CompactBlock messages are explicitly excluded from the relay rate limiter, allowing rapid repeated delivery from multiple connections.

## Finding Description

**Missing count bound in `non_contextual_check`:**

`non_contextual_check` enforces limits on uncles and proposals but has no analogous check on `short_ids`:

```rust
if compact_block.uncles().len() > consensus.max_uncles_num() { … }
if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() { … }
// No check on compact_block.short_ids().len()
``` [1](#0-0) 

**Allocation sites in `reconstruct_block`:**

`txs_len()` is purely attacker-controlled: [2](#0-1) 

This drives two large heap allocations: [3](#0-2) [4](#0-3) 

And a third in `block_short_ids()`: [5](#0-4) 

**`CompactBlockVerifier` imposes no absolute count limit** — it only checks ordering, cellbase presence, index bounds relative to `txs_len`, and duplicate short IDs: [6](#0-5) 

**CompactBlock messages are explicitly excluded from the relay rate limiter:**

```rust
let should_check_rate =
    !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
``` [7](#0-6) 

**P2P decompression ceiling is 8 MB:** [8](#0-7) 

Each `ProposalShortId` is 10 bytes, so a single message can carry ~800,000 short IDs. The consensus maximum for block transactions is far lower (~2,985 for `MAX_BLOCK_BYTES = 597,000` bytes with minimum-size transactions). No check enforces this tighter bound.

**`tx_pool.fetch_txs` is called with the full attacker-controlled set** (up to 800k entries) under a lock before any validity check: [9](#0-8) 

**Peer is not banned** after the attack: the resulting `Collided` or `Missing` status codes do not trigger `nc.ban_peer`, so the attacker can repeat indefinitely from the same connection.

## Impact Explanation

Per malicious message: ~35 MB heap allocated (`HashSet` ~16 MB + `Vec<Option<TransactionView>>` ~6.4 MB + `Vec<Option<ProposalShortId>>` ~12.8 MB), plus 800,000 hash-map lookups in the tx pool under a lock. With 10–20 concurrent malicious peers sending such messages in a tight loop (no rate limit on CompactBlock), heap usage rises by 350–700 MB above baseline, potentially triggering OOM on memory-constrained deployments or causing severe latency spikes in the tx pool that degrade block processing.

This matches: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Any unauthenticated P2P peer on the relay protocol can send a `CompactBlock` message; no stake, key, or privileged role is required.
- Construction is trivial: one prefilled cellbase at index 0, then fill the remaining 8 MB with unique 10-byte short IDs.
- The message passes `CompactBlockVerifier::verify` (cellbase present, no duplicates, index 0 < `txs_len`) and `non_contextual_check` (uncles = 0, proposals = 0).
- CompactBlock is explicitly excluded from the 30 req/s rate limiter, so the attacker can send at full network speed.
- The peer is never banned for this behavior; the failure occurs only at the transaction-root comparison after all allocations are complete.
- Multiple connections (up to the node's peer limit) multiply the effect linearly.

## Recommendation

Add an explicit upper-bound check on `short_ids().len() + prefilled_transactions().len()` inside `non_contextual_check`, bounded by the consensus-derived maximum block transaction count, mirroring the existing pattern for uncles and proposals:

```rust
let max_block_txs = consensus.max_block_bytes() / MIN_TRANSACTION_SERIALIZED_SIZE;
if compact_block.short_ids().len() + compact_block.prefilled_transactions().len()
    > max_block_txs as usize
{
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "CompactBlock txs count({}) exceeds consensus max({})",
        compact_block.txs_len(), max_block_txs
    ));
}
```

Additionally, consider applying rate limiting to CompactBlock messages or banning peers whose reconstructed block fails the transaction-root check when all transactions were prefilled.

## Proof of Concept

1. Connect to a CKB mainnet/testnet node as a relay peer (`SupportProtocols::RelayV3`).
2. Construct a `CompactBlock` molecule message:
   - `header`: any valid-looking header (parent hash = tip hash; valid PoW not required to pass `non_contextual_check`).
   - `prefilled_transactions`: one `IndexTransaction` with `index = 0` and a minimal cellbase transaction.
   - `short_ids`: fill with ~800,000 unique 10-byte `ProposalShortId` values until the serialized message approaches 8 MB.
   - `uncles`: empty. `proposals`: empty.
3. Send the message. The node will:
   - Pass `CompactBlockVerifier::verify` (cellbase present, no duplicates, index 0 < `txs_len`).
   - Pass `non_contextual_check` (uncles = 0, proposals = 0).
   - Enter `reconstruct_block`, allocating ~35 MB of heap.
   - Call `tx_pool.fetch_txs` with 800,000 short IDs under a lock.
   - Eventually return `Collided` or `Missing` — without banning the peer.
4. Repeat from multiple connections to amplify memory pressure until OOM or tx-pool stall is observed.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L196-209)
```rust
    if compact_block.uncles().len() > consensus.max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock uncles count({}) > consensus max_uncles_num({})",
            compact_block.uncles().len(),
            consensus.max_uncles_num()
        ));
    }
    if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock proposals count({}) > consensus max_block_proposals_limit({})",
            compact_block.proposals().len(),
            consensus.max_block_proposals_limit(),
        ));
    }
```

**File:** util/gen-types/src/extension/shortcut.rs (L187-189)
```rust
    pub fn txs_len(&self) -> usize {
        self.prefilled_transactions().len() + self.short_ids().len()
    }
```

**File:** sync/src/relayer/mod.rs (L113-114)
```rust
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

**File:** sync/src/relayer/mod.rs (L371-372)
```rust
        let mut short_ids_set: HashSet<ProposalShortId> =
            compact_block.short_ids().into_iter().collect();
```

**File:** sync/src/relayer/mod.rs (L387-392)
```rust
            let tx_pool = self.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(short_ids_set).await;
            if let Err(e) = fetch_txs {
                return ReconstructionResult::Error(StatusCode::TxPool.with_context(e));
            }
            txs_map.extend(fetch_txs.unwrap());
```

**File:** sync/src/relayer/mod.rs (L395-397)
```rust
        let txs_len = compact_block.txs_len();
        let mut block_transactions: Vec<Option<core::TransactionView>> =
            Vec::with_capacity(txs_len);
```

**File:** util/types/src/extension.rs (L121-122)
```rust
        let txs_len = self.txs_len();
        let mut block_short_ids: Vec<Option<packed::ProposalShortId>> = Vec::with_capacity(txs_len);
```

**File:** sync/src/relayer/compact_block_verifier.rs (L11-15)
```rust
    pub(crate) fn verify(block: &packed::CompactBlock) -> Status {
        attempt!(PrefilledVerifier::verify(block));
        attempt!(ShortIdsVerifier::verify(block));
        Status::ok()
    }
```

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```
