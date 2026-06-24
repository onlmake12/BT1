Audit Report

## Title
Unbounded `peers_map` Growth in `pending_compact_blocks` via Multi-Peer Compact Block Relay — (`sync/src/relayer/compact_block_process.rs`)

## Summary
The inner `peers_map` of `PendingCompactBlockMap` has no size cap. The only guard prevents the *same* peer from inserting twice, but N distinct peers can each insert a separate `(Vec<u32>, Vec<u32>)` entry for the same block hash. Combined with a TOCTOU race between the check in `contextual_check` and the insert in `missing_or_collided_post_process`, and the explicit disabling of rate limiting for `CompactBlock` messages, an attacker controlling many connections can drive unbounded heap growth, eventually crashing the node.

## Finding Description

**Type definition — no per-block peer cap:**
`PendingCompactBlockMap` is defined as a nested `HashMap` with no bound on the inner map's size. [1](#0-0) 

**Guard checks only the same peer:**
`contextual_check` acquires the `pending_compact_blocks` async mutex and returns early only if *this specific peer* already has an entry for the block hash. [2](#0-1) 

**TOCTOU — lock released before insert:**
`contextual_check` drops the mutex guard when it returns `Status::ok()` at line 341. The caller (`execute`) then performs header insertion, proposal requests, and block reconstruction before calling `missing_or_collided_post_process`, which re-acquires the mutex independently. Two concurrent tasks processing the same block from two different peers can both pass the check before either inserts, allowing both to proceed to the uncapped insert. [3](#0-2) 

**No cap enforced at insert time:**
`missing_or_collided_post_process` calls `.insert(peer, ...)` with no check on `peers_map.len()`. [3](#0-2) 

**Rate limiting explicitly disabled for `CompactBlock`:** [4](#0-3) 

**`MAX_RELAY_PEERS` is unused for this purpose:**
`MAX_RELAY_PEERS = 128` is only used to cap outbound broadcast recipients, not to bound `peers_map.len()`. [5](#0-4) [6](#0-5) 

**`non_contextual_check` does not limit `short_ids` count:**
Uncles and proposals are bounded, but `compact_block.short_ids().len()` is unchecked, allowing a compact block to carry up to the consensus-maximum number of short IDs. [7](#0-6) 

**Cleanup is deferred:**
Pending entries are only removed on block acceptance or epoch-boundary pruning, meaning an attacker can keep entries alive for the duration of an epoch (~1800 blocks) by ensuring all short IDs remain unresolvable. [8](#0-7) 

## Impact Explanation

With 128 connected peers each relaying the same compact block whose short IDs are all absent from the local tx pool, each peer inserts one `(Vec<u32>, Vec<u32>)` entry where the first `Vec` holds up to ~6 000 `u32` indices. That is approximately `128 × 6 000 × 4 B ≈ 3 MB` per pending block. Multiple concurrent pending blocks (one per active chain tip candidate) multiply this linearly. Because cleanup is epoch-gated and the attacker can keep blocks unresolvable, the state persists long enough to exhaust available memory and crash the node.

This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attacker does not need to mine a block; they only need to observe a valid compact block propagating on the network and relay it from many simultaneous connections. Rate limiting is explicitly bypassed for `CompactBlock`. CKB's default peer limits allow up to 125+ inbound connections, making 128 attacker-controlled connections achievable from a single machine or a small botnet. The attack is repeatable every epoch and requires no victim interaction or privileged access.

## Recommendation

1. **Cap `peers_map` per block hash** before inserting in `missing_or_collided_post_process`: reject the insert if `peers_map.len() >= MAX_PEERS_PER_PENDING_BLOCK` (e.g., 4–8).
2. **Hold the lock across check and insert** (or use an entry-API pattern that atomically checks and inserts) to close the TOCTOU window.
3. **Limit `short_ids` count** in `non_contextual_check` to the consensus maximum transaction count, consistent with the existing uncle and proposal checks.
4. **Consider per-peer rate limiting** for `CompactBlock` messages rather than a blanket exemption.

## Proof of Concept

```rust
// Pseudocode — run against a local CKB node
let compact_block = observe_from_network(); // valid PoW, all short_ids absent locally

// Open 128 distinct peer connections to the victim node
for peer_index in 0..128 {
    connect_new_peer(peer_index);
    // Each peer sends the same compact block; all short_ids are unknown,
    // so each triggers the Missing branch and calls missing_or_collided_post_process
    send_compact_block(peer_index, compact_block.clone());
}

// After processing, assert unbounded growth:
let pending = shared.state().pending_compact_blocks().await;
let (_, peers_map, _) = pending.get(&block_hash).unwrap();
assert_eq!(peers_map.len(), 128);
// Each entry holds up to txs_len u32 values → ~3 MB heap per pending block
```

To reproduce deterministically: write a unit test that spawns 128 concurrent `CompactBlockProcess::execute` tasks sharing the same `SyncShared`, each with a distinct `PeerIndex` and the same compact block whose short IDs are all absent from a mock tx pool. Assert `peers_map.len() == 128` after all tasks complete.

### Citations

**File:** sync/src/types/mod.rs (L979-987)
```rust
// <CompactBlockHash, (CompactBlock, <PeerIndex, (Vec<TransactionsIndex>, Vec<UnclesIndex>)>, timestamp)>
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,
        u64,
    ),
>;
```

**File:** sync/src/relayer/compact_block_process.rs (L106-117)
```rust
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
                // remove all pending request below this block epoch
                //
                // use epoch as the judgment condition because we accept
                // all block in current epoch as uncle block
                pending_compact_blocks.retain(|_, (v, _, _)| {
                    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
                        .number()
                        >= block.epoch().number()
                });
                shrink_to_fit!(pending_compact_blocks, 20);
```

**File:** sync/src/relayer/compact_block_process.rs (L190-222)
```rust
fn non_contextual_check(
    compact_block: &CompactBlock,
    header: &HeaderView,
    consensus: &Consensus,
    active_chain: &ActiveChain,
) -> Status {
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

    // Only accept blocks with a height greater than tip - N
    // where N is the current epoch length
    let block_hash = header.hash();
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }

    Status::ok()
```

**File:** sync/src/relayer/compact_block_process.rs (L284-291)
```rust
    let pending_compact_blocks = shared.state().pending_compact_blocks().await;
    if pending_compact_blocks
        .get(&block_hash)
        .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
        .unwrap_or(false)
    {
        return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```

**File:** sync/src/relayer/mod.rs (L59-59)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
```

**File:** sync/src/relayer/mod.rs (L112-114)
```rust
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
```

**File:** sync/src/relayer/mod.rs (L727-732)
```rust
    let selected_peers: Vec<PeerIndex> = nc
        .connected_peers()
        .into_iter()
        .filter(|target_peer| peer != *target_peer)
        .take(MAX_RELAY_PEERS)
        .collect();
```
