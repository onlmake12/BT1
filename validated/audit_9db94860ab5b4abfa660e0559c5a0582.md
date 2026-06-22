### Title
Unbounded `peers_map` Growth in `pending_compact_blocks` via Multi-Peer Compact Block Relay — (`sync/src/relayer/compact_block_process.rs`)

### Summary

The per-block `peers_map` inside `pending_compact_blocks` has no size cap. The only guard prevents the *same* peer from inserting twice, but N distinct peers can each insert a separate `(Vec<u32>, Vec<u32>)` entry for the same block hash, causing memory to grow proportionally to the number of connected peers.

### Finding Description

`PendingCompactBlockMap` is typed as:

```
HashMap<Byte32, (CompactBlock, HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>, u64)>
``` [1](#0-0) 

The guard in `contextual_check` only rejects a message when **the same peer** is already present in `peers_map`:

```rust
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{
    return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
}
``` [2](#0-1) 

After this check the mutex is **released** (the guard is a local variable in `contextual_check`). `missing_or_collided_post_process` then re-acquires the mutex and unconditionally inserts the new peer entry with no cap on `peers_map.len()`:

```rust
.entry(block_hash.clone())
.or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
.1
.insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
``` [3](#0-2) 

Critically, the rate limiter **explicitly skips** `CompactBlock` messages:

```rust
// CompactBlock will be verified by POW, it's OK to skip rate limit checking.
let should_check_rate =
    !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));
``` [4](#0-3) 

`non_contextual_check` validates uncle count and proposal count but places **no limit on `short_ids().len()`**, so a compact block can carry up to the consensus-maximum number of transaction short IDs. [5](#0-4) 

`MAX_RELAY_PEERS = 128` is defined but is **not used** to cap `peers_map.len()`. [6](#0-5) 

### Impact Explanation

With `MAX_RELAY_PEERS = 128` connected peers and a compact block carrying `T` short IDs all absent from the local tx pool:

- Each peer inserts one `(Vec<u32>, Vec<u32>)` entry where the first `Vec` has `T` elements.
- Total heap per pending block ≈ `128 × T × 4 bytes`.
- At the consensus block-size ceiling (~6 000 transactions), that is ≈ **3 MB per pending block**.
- Multiple concurrent pending blocks multiply this further.
- The state is not cleaned up until the block is accepted or pruned by epoch, which can be delayed if the attacker keeps the block unresolvable (all short IDs unknown).

### Likelihood Explanation

The attacker does **not** need to mine a block. They can relay any legitimate compact block observed on the network from 128 simultaneous connections. The rate limiter is explicitly disabled for `CompactBlock`. Establishing 128 connections is within reach of a single machine given CKB's default peer limits.

### Recommendation

1. **Cap `peers_map` per block hash** at a small constant (e.g., 4–8 peers) before inserting in `missing_or_collided_post_process`.
2. **Limit `short_ids` count** in `non_contextual_check` to the consensus maximum transaction count.
3. Consider re-enabling rate limiting for `CompactBlock` on a per-peer basis, or applying a separate per-peer compact-block rate limit.

### Proof of Concept

```rust
// Pseudocode
let compact_block = observe_from_network(); // valid PoW, all short_ids missing locally
for peer_index in 0..128 {
    connect_new_peer(peer_index);
    send_compact_block(peer_index, compact_block.clone());
}
// After processing:
let pending = shared.state().pending_compact_blocks().await;
let (_, peers_map, _) = pending.get(&block_hash).unwrap();
assert_eq!(peers_map.len(), 128);
// Each entry holds up to txs_len u32 values
```

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
