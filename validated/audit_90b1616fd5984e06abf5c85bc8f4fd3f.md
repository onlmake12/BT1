All four code references are confirmed against the actual source. Let me verify the complete picture:

All code references are confirmed against the actual source. Here is the validation result:

---

Audit Report

## Title
Unpermissioned Peer Can Freeze `pending_compact_blocks` Timestamp to Delay Sync Fallback by 2 Seconds Per Block - (File: `sync/src/relayer/compact_block_process.rs`)

## Summary
Any connected peer can send a `CompactBlock` message that fails reconstruction, causing `missing_or_collided_post_process` to record the current timestamp in `pending_compact_blocks` via `or_insert_with`. Because `or_insert_with` only fires on the first insertion, a racing attacker peer freezes the timestamp before the legitimate peer's compact block arrives. `BlockFetcher::fetch` then refuses to schedule `GetBlocks` for that hash for 2 seconds, delaying the sync fallback path for every targeted block.

## Finding Description

**Root cause — timestamp frozen by first inserter:**

In `sync/src/relayer/compact_block_process.rs` lines 354–361, `missing_or_collided_post_process` uses `or_insert_with` to record the timestamp only on the first insertion for a given `block_hash`:

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

Any peer whose compact block fails reconstruction reaches this path. The first peer to reach it freezes the timestamp; all subsequent peers only add themselves to `peers_map` without updating it. [1](#0-0) 

**Gate in `compare_with_pending_compact`:**

In `sync/src/types/mod.rs` lines 1362–1370, the sync fetcher is blocked while `now <= time + 2000`:

```rust
pub fn compare_with_pending_compact(&self, hash: &Byte32, now: u64) -> bool {
    let pending = self.pending_compact_blocks.blocking_lock();
    pending.is_empty()
        || pending
            .get(hash)
            .map(|(_, _, time)| now > time + 2000)
            .unwrap_or(true)
}
``` [2](#0-1) 

**Gate enforced in `BlockFetcher`:**

In `sync/src/synchronizer/block_fetcher.rs` lines 271–284, a block is only added to `inflight_blocks` (and a `GetBlocks` issued) when `compare_with_pending_compact` returns `true`, unless the node is in IBD:

```rust
} else if (matches!(self.ibd, IBDState::In)
    || state.compare_with_pending_compact(&hash, now))
    && state
        .write_inflight_blocks()
        .insert(self.peer, (header.number(), hash).into())
{
    fetch.push(header)
}
``` [3](#0-2) 

**Existing guard is insufficient:**

`contextual_check` in `sync/src/relayer/compact_block_process.rs` lines 284–291 only rejects a message if the **same** peer already appears in `peers_map`:

```rust
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{
    return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
}
```

A different (attacker) peer is not rejected and proceeds to `missing_or_collided_post_process`, freezing the timestamp before the legitimate peer arrives. [4](#0-3) 

## Impact Explanation

For a non-IBD node, the sync fallback path (`BlockFetcher` → `GetBlocks`) is suppressed for 2 seconds after an attacker injects a compact block with missing transactions for a target block hash. CKB's block time is ~10 seconds, so a 2-second delay represents ~20% of a block interval. Repeated across successive blocks, this constitutes a sustained block-propagation delay against a targeted node. This maps to **Low (501–2000 points): any other important performance improvement for CKB**, as it degrades block propagation reliability for targeted nodes without crashing them or causing consensus deviation.

## Likelihood Explanation

The attacker requires only a standard TCP peer connection — no keys, no hashpower, no operator privilege. The attacker must learn the hash of a newly mined block before the victim's legitimate compact block relay completes, which is achievable by being better-connected to the mining pool or on the same network segment. The attacker can receive the legitimate compact block from the network and relay it to the victim with a different nonce, causing different short IDs and thus reconstruction failure. The attack is repeatable for every new block and requires no state beyond a single open connection.

## Recommendation

Decouple the 2-second gate timestamp from peer-driven insertion:

- Record `unix_time_as_millis()` only when the **local node** sends a `GetBlockTransactions` request (i.e., after the node itself decides to request missing transactions), not when any arbitrary peer delivers a `CompactBlock`.
- Alternatively, replace the `compare_with_pending_compact` gate in `BlockFetcher` with a direct check on `inflight_blocks`, which is only written by the local scheduler and cannot be poisoned by remote peers.

## Proof of Concept

1. Victim node V (non-IBD) is connected to attacker peer A and legitimate peer L.
2. Block H is mined; L prepares to relay it to V.
3. A races L: A sends `CompactBlock(H)` to V with a different nonce, causing `missing_transactions = [0, 1, ...]`.
4. V calls `missing_or_collided_post_process`: `or_insert_with` fires, recording timestamp `T`.
5. V's `BlockFetcher` runs: `compare_with_pending_compact(H, now)` returns `false` (`now ≤ T + 2000`). No `GetBlocks` is issued.
6. L's `CompactBlock(H)` arrives. `contextual_check` passes (L not yet in `peers_map`). `or_insert_with` does **not** update `T`. L is added to `peers_map`.
7. If A used different short-ID nonces than L, reconstruction against A's stored compact block fails when L's `BlockTransactions` response arrives.
8. For the full 2-second window, V cannot fall back to sync for block H.
9. Attacker repeats for each new block, sustaining ≥2-second propagation delay against V.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L285-291)
```rust
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

**File:** sync/src/synchronizer/block_fetcher.rs (L271-284)
```rust
                } else if (matches!(self.ibd, IBDState::In)
                    || state.compare_with_pending_compact(&hash, now))
                    && state
                        .write_inflight_blocks()
                        .insert(self.peer, (header.number(), hash).into())
                {
                    debug!(
                        "block: {}-{} added to inflight, block_status: {:?}",
                        header.number(),
                        header.hash(),
                        status
                    );
                    fetch.push(header)
                }
```
