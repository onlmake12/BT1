### Title
Unpermissioned Peer Can Delay Sync Block Downloads by Injecting Stale Timestamp into `pending_compact_blocks` - (File: `sync/src/relayer/compact_block_process.rs`)

---

### Summary

Any connected peer can send a `CompactBlock` message with missing transactions for a target block hash. This causes `missing_or_collided_post_process` to insert an entry into the shared `pending_compact_blocks` map with the current timestamp. The sync block fetcher (`BlockFetcher`) reads this timestamp via `compare_with_pending_compact` and refuses to schedule a `GetBlocks` download for that hash for 2 seconds. An attacker who sends this message before the legitimate compact block relay completes can delay the victim node's fallback sync path by 2 seconds per targeted block.

---

### Finding Description

**Step 1 — Timestamp is written by any peer via compact block relay.**

In `sync/src/relayer/compact_block_process.rs`, when a compact block cannot be fully reconstructed (missing transactions or short-ID collision), `missing_or_collided_post_process` is called:

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

The `or_insert_with` call records `unix_time_as_millis()` as the insertion timestamp the first time a given `block_hash` is seen. Any peer that sends a `CompactBlock` message whose reconstruction fails triggers this path. [1](#0-0) 

**Step 2 — The timestamp gates the sync block fetcher.**

In `sync/src/types/mod.rs`, `compare_with_pending_compact` returns `false` (blocking sync) while `now <= time + 2000`:

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

**Step 3 — The gate is enforced in the block fetcher for non-IBD nodes.**

In `sync/src/synchronizer/block_fetcher.rs`, a block is only added to the inflight download queue when `compare_with_pending_compact` returns `true` (or the node is in IBD):

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

**Step 4 — The `contextual_check` does not prevent a second peer from triggering the path.**

The only guard in `contextual_check` rejects a message only if the **same** peer already has the block in the pending map:

```rust
if pending_compact_blocks
    .get(&block_hash)
    .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
    .unwrap_or(false)
{
    return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
}
``` [4](#0-3) 

A different peer (the attacker) is not rejected and proceeds to `missing_or_collided_post_process`. Because `or_insert_with` only fires on the first insertion, the timestamp is frozen at the attacker's injection time, not refreshed by the legitimate peer's arrival.

---

### Impact Explanation

For a non-IBD node (i.e., a node that is already synced and receiving new blocks), the sync fallback path is blocked for 2 seconds after an attacker injects a compact block with missing transactions for a target block hash. During those 2 seconds, `BlockFetcher` will not issue a `GetBlocks` request for that hash to any peer. If the compact block relay also fails to complete (e.g., because the attacker's compact block uses different short-ID nonces than the legitimate one, or the attacker withholds the `BlockTransactions` response), the node is delayed by the full 2-second window before it can fall back to sync. Repeated across successive blocks, this constitutes a sustained block-propagation delay against a targeted node.

---

### Likelihood Explanation

The attacker must:
1. Be a connected peer of the victim node (no special privilege required beyond a TCP connection).
2. Learn the hash of a newly mined block before the victim's legitimate compact block relay completes — achievable by being better-connected to the mining pool or by racing on the same network segment.
3. Send a `CompactBlock` message for that hash with at least one missing transaction index.

No cryptographic material, operator keys, or majority hashpower is required. The attack is reachable from any unpermissioned inbound or outbound peer.

---

### Recommendation

Separate the timestamp used for the 2-second gate from the one set by the first peer to send a compact block. Specifically:

- Record the insertion timestamp only when the **local node itself** initiates the compact block request (i.e., when the node is the one that sent `GetBlockTransactions`), not when any arbitrary peer delivers a `CompactBlock`.
- Alternatively, do not use `pending_compact_blocks` as the gate for `BlockFetcher` at all; instead, check `inflight_blocks` directly, which is only written by the local node's own download scheduler.

---

### Proof of Concept

1. Victim node V is non-IBD, connected to attacker peer A and legitimate peer L.
2. Block H is mined; L receives it and prepares to relay it to V.
3. A races L: A sends `CompactBlock(H)` to V with `missing_transactions = [0, 1, ...]` (all short IDs claimed missing).
4. V calls `missing_or_collided_post_process`: `pending_compact_blocks.entry(H).or_insert_with(|| (..., unix_time_as_millis()))` — timestamp `T` is recorded.
5. V's `BlockFetcher` runs: `compare_with_pending_compact(H, now)` returns `false` because `now ≤ T + 2000`. Block H is **not** added to `inflight_blocks`. No `GetBlocks` is sent.
6. L's legitimate `CompactBlock(H)` arrives. `contextual_check` passes (L is not yet in `peers_map`). `missing_or_collided_post_process` runs again, but `or_insert_with` does **not** update the timestamp (entry already exists). L is added to `peers_map`.
7. A ignores the `GetBlockTransactions` request V sends to it. L responds correctly, but if A's compact block used different nonces, reconstruction from L's transactions against A's short IDs fails.
8. For the full 2-second window, V cannot fall back to sync for block H.
9. Attacker repeats for each new block, sustaining a ≥2-second propagation delay against V.

### Citations

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
