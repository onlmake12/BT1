Audit Report

## Title
Stale `pending_compact_blocks` Entries Lack Time-Based Expiry, Enabling Within-Epoch Memory Accumulation — (File: `sync/src/types/mod.rs`, `sync/src/relayer/compact_block_process.rs`)

## Summary
`PendingCompactBlockMap` stores a millisecond timestamp at insertion but never uses it for eviction. The only removal path is an epoch-number-based `retain` that fires only on successful block acceptance, leaving all entries from the current epoch alive for up to the full epoch duration (~4 hours on mainnet). An attacker who can produce valid PoW can insert entries that will never be resolved if they withhold the missing transactions, causing unbounded within-epoch map growth.

## Finding Description
`PendingCompactBlockMap` is defined as `HashMap<Byte32, (CompactBlock, HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>, u64)>` where the `u64` is a millisecond timestamp captured at insertion. [1](#0-0) 

Entries are inserted in `missing_or_collided_post_process` via `or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))` whenever compact block reconstruction fails due to missing transactions or a short-ID collision. [2](#0-1) 

The only removal path is inside `CompactBlockProcess::execute` on successful block acceptance, which calls `retain` to drop entries whose epoch number is strictly less than the accepted block's epoch — entries from the *current* epoch are never removed by this path. [3](#0-2) 

The stored timestamp is consulted solely in `compare_with_pending_compact` to impose a 2-second back-off before the synchronizer creates download tasks; it is never used to evict entries. [4](#0-3) 

`non_contextual_check` only rejects blocks whose number falls below `tip - epoch_length`; blocks within the current epoch pass this gate and proceed to PoW verification and insertion. [5](#0-4) 

`contextual_check` does run `HeaderVerifier` which enforces PoW validity, so each inserted entry requires a valid proof of work. However, no periodic timer, no maximum map size, and no wall-clock TTL eviction exists anywhere in the sync codebase. The `shrink_to_fit!` macro at line 117 only reclaims allocator capacity, not logical entries. [6](#0-5) 

## Impact Explanation
Each unresolved entry holds a full `CompactBlock` (bounded by `max_block_bytes`) plus per-peer index vectors. With no size cap and no TTL, the map grows for the entire epoch duration. On low-difficulty deployments (testnet, devnet, or any private network) an attacker can mine many blocks cheaply and fill the map, potentially exhausting node memory and crashing the process. This maps to **High: Vulnerabilities which could easily crash a CKB node**. On mainnet the cost scales with hashrate, reducing practical severity, but the design flaw is unconditional.

## Likelihood Explanation
The attacker must produce valid PoW for each inserted block — insertion is gated by `contextual_check` which enforces header PoW validity via `HeaderVerifier`. On mainnet this is expensive, placing the realistic likelihood at Low–Medium. On testnet or any low-difficulty network the bar is trivially low and the attack is fully repeatable within an epoch. No privileged access or victim mistake is required; the attack is reachable via the standard relay P2P protocol.

## Recommendation
Add a wall-clock TTL eviction pass over `pending_compact_blocks` in the existing periodic maintenance tick (alongside `inflight_blocks` pruning). The timestamp field already present makes this a minimal change:

```rust
let now = unix_time_as_millis();
const PENDING_COMPACT_TTL_MS: u64 = 30_000; // 30 seconds
pending_compact_blocks.retain(|_, (_, _, ts)| now < ts + PENDING_COMPACT_TTL_MS);
```

Additionally, enforce a hard cap on the number of entries (e.g., 32) to bound worst-case memory regardless of timing. Both measures together close the within-epoch accumulation window that the existing epoch-based `retain` leaves open.

## Proof of Concept
1. Attacker mines block `B` at height `h` within the current epoch (valid PoW required).
2. Attacker constructs a `CompactBlock` for `B` with `short_ids` referencing transactions absent from the victim's mempool.
3. Attacker sends the `CompactBlock` to the victim via the relay protocol.
4. Victim passes `non_contextual_check` (height ≥ `tip - epoch_length`) and `contextual_check` (PoW valid, parent known).
5. Reconstruction returns `ReconstructionResult::Missing`; `missing_or_collided_post_process` inserts `(compact_block, {attacker_peer: missing_indexes}, now_ms)` into `pending_compact_blocks`.
6. Attacker never responds to `GetBlockTransactions`. The entry persists until the epoch advances (~4 hours on mainnet).
7. Attacker repeats for each block mined within the epoch. The map grows without bound.
8. Observable effect: node RSS grows proportionally to the number of inserted entries; `compare_with_pending_compact` returns `false` for affected hashes within the 2-second window, delaying sync task creation for those hashes.

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

**File:** sync/src/types/mod.rs (L1362-1370)
```rust
    pub fn compare_with_pending_compact(&self, hash: &Byte32, now: u64) -> bool {
        let pending = self.pending_compact_blocks.blocking_lock();
        // After compact block request 2s or pending is empty, sync can create tasks
        pending.is_empty()
            || pending
                .get(hash)
                .map(|(_, _, time)| now > time + 2000)
                .unwrap_or(true)
    }
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

**File:** sync/src/relayer/compact_block_process.rs (L211-222)
```rust
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

**File:** sync/src/relayer/compact_block_process.rs (L324-338)
```rust
    let header_verifier = HeaderVerifier::new(&median_time_context, shared.consensus());
    if let Err(err) = header_verifier.verify(compact_block_header) {
        if err
            .downcast_ref::<HeaderError>()
            .map(|e| e.is_too_new())
            .unwrap_or(false)
        {
            return Status::ignored();
        } else {
            shared
                .shared()
                .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
            return StatusCode::CompactBlockHasInvalidHeader
                .with_context(format!("{block_hash} {err}"));
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
