Audit Report

## Title
Unbounded `short_ids` Count in CompactBlock Causes Excessive Memory Allocation in `reconstruct_block` - (File: `sync/src/relayer/compact_block_process.rs`, `sync/src/relayer/mod.rs`)

## Summary

The `non_contextual_check` function validates uncles and proposals counts against consensus limits but imposes no upper bound on `compact_block.short_ids().len()`. Any unauthenticated P2P peer can craft a `CompactBlock` message with up to ~800,000 short IDs (filling the 8 MB decompression limit), causing `reconstruct_block` to allocate large heap structures proportional to the attacker-controlled `txs_len` before any transaction-root or block-validity check is performed. Rate limiting is explicitly bypassed for `CompactBlock` messages, and the peer is not banned after the message passes all structural checks.

## Finding Description

**Missing bound in `non_contextual_check`:**

`non_contextual_check` enforces limits on uncles and proposals:

```rust
if compact_block.uncles().len() > consensus.max_uncles_num() { … }
if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() { … }
``` [1](#0-0) 

There is no analogous check on `compact_block.short_ids().len()`.

**Allocation sites in `reconstruct_block`:**

`txs_len()` is fully attacker-controlled: [2](#0-1) 

The function allocates a `HashSet` from all short IDs and a `Vec` with capacity `txs_len`: [3](#0-2) 

`block_short_ids()` allocates a second `Vec` with capacity `txs_len`: [4](#0-3) 

`tx_pool.fetch_txs` is then called with the full attacker-controlled set under a lock: [5](#0-4) 

**`CompactBlockVerifier` does not bound the count:**

`CompactBlockVerifier::verify` only checks ordering, cellbase presence, index bounds relative to `txs_len`, and duplicate short IDs — none of which limit the absolute count. [6](#0-5) 

**Rate limiting explicitly bypassed for `CompactBlock`:**

The relay handler skips rate limiting for `CompactBlock` messages: [7](#0-6) 

**P2P decompression limit is the only constraint:**

`MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB) is the sole upper bound on message size. [8](#0-7) 

Each `ProposalShortId` is 10 bytes, allowing ~800,000 short IDs per message. The consensus maximum for block transactions is far lower (~2,985 for mainnet), but this tighter bound is never enforced on incoming compact blocks.

**No peer ban after exploitation:**

When `reconstruct_block` returns `Collided` or `Missing` (the outcomes for a crafted message), the peer is not banned — only a debug-level status is returned. [9](#0-8) 

## Impact Explanation

Per malicious compact block message, the node allocates approximately:
- `HashSet<ProposalShortId>` (800k entries): ~16 MB
- `Vec<Option<TransactionView>>` (800k capacity): ~6.4 MB
- `Vec<Option<ProposalShortId>>` in `block_short_ids()`: ~12.8 MB
- **Total per message: ~35 MB**

Additionally, `tx_pool.fetch_txs` is called with 800,000 entries, causing 800,000 hash-map lookups under a lock per message.

A small number of concurrent malicious peers (10–20) sending such messages in a loop can push heap usage by 350–700 MB above baseline, potentially triggering OOM on memory-constrained deployments or causing severe latency spikes in the tx pool. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- **Entry path:** Any unauthenticated P2P peer on the relay protocol can send a `CompactBlock` message. No stake, key, or privileged role is required.
- **Ease of construction:** The attacker builds a `CompactBlock` with 1 prefilled cellbase (index 0) and fills the remaining 8 MB with unique 10-byte short IDs. `CompactBlockVerifier` passes this message.
- **No rate limiting:** `CompactBlock` messages are explicitly excluded from the per-peer rate limiter.
- **No ban:** The peer is never banned because the message passes all structural checks; failure only occurs later at the transaction-root comparison, which returns `Collided` or `Missing` without banning.
- **Amplification:** The attacker can open multiple connections (up to the node's peer limit) and send such messages in a tight loop.

## Recommendation

Add an explicit upper-bound check on `short_ids().len()` (and `prefilled_transactions().len()`) inside `non_contextual_check`, bounded by the consensus-derived maximum block transaction count, mirroring the existing pattern for uncles and proposals:

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

This check should be placed in `non_contextual_check` alongside the existing uncle and proposal checks. [1](#0-0) 

## Proof of Concept

1. Connect to a CKB mainnet/testnet node as a relay peer (`SupportProtocols::RelayV3`).
2. Construct a `CompactBlock` molecule message:
   - `header`: any valid-looking header (parent hash = tip hash; valid PoW not required to pass `non_contextual_check`).
   - `prefilled_transactions`: one `IndexTransaction` with `index = 0` and a minimal cellbase transaction.
   - `short_ids`: fill with ~800,000 unique 10-byte `ProposalShortId` values until the serialized message approaches 8 MB.
   - `uncles`: empty. `proposals`: empty.
3. Send the message. The node will:
   - Pass `CompactBlockVerifier::verify` (cellbase present, no duplicates, index 0 < txs_len).
   - Pass `non_contextual_check` (uncles = 0, proposals = 0).
   - Enter `reconstruct_block`, allocating ~35 MB of heap.
   - Call `tx_pool.fetch_txs` with 800,000 short IDs under a lock.
   - Return `Collided` or `Missing` — **without banning the peer**.
4. Repeat from multiple connections to amplify memory pressure until OOM or severe degradation occurs.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L128-171)
```rust
            ReconstructionResult::Missing(transactions, uncles) => {
                let missing_transactions: Vec<u32> =
                    transactions.into_iter().map(|i| i as u32).collect();

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_fresh_tx_cnt
                        .inc_by(missing_transactions.len() as u64);
                    metrics.ckb_relay_cb_reconstruct_fail.inc();
                }

                let missing_uncles: Vec<u32> = uncles.into_iter().map(|i| i as u32).collect();
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;

                StatusCode::CompactBlockRequiresFreshTransactions.with_context(&block_hash)
            }
            ReconstructionResult::Collided => {
                let missing_transactions: Vec<u32> = compact_block
                    .short_id_indexes()
                    .into_iter()
                    .map(|i| i as u32)
                    .collect();
                let missing_uncles: Vec<u32> = vec![];
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;
                StatusCode::CompactBlockMeetsShortIdsCollision.with_context(&block_hash)
            }
```

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

**File:** sync/src/relayer/mod.rs (L112-114)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

**File:** sync/src/relayer/mod.rs (L371-397)
```rust
        let mut short_ids_set: HashSet<ProposalShortId> =
            compact_block.short_ids().into_iter().collect();

        let mut txs_map: HashMap<ProposalShortId, core::TransactionView> = received_transactions
            .into_iter()
            .filter_map(|tx| {
                let short_id = tx.proposal_short_id();
                if short_ids_set.remove(&short_id) {
                    Some((short_id, tx))
                } else {
                    None
                }
            })
            .collect();

        if !short_ids_set.is_empty() {
            let tx_pool = self.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(short_ids_set).await;
            if let Err(e) = fetch_txs {
                return ReconstructionResult::Error(StatusCode::TxPool.with_context(e));
            }
            txs_map.extend(fetch_txs.unwrap());
        }

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
