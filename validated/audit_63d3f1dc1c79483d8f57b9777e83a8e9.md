### Title
Early Return on Single Invalid Hash Aborts Entire `GetBlocks` Batch and Bans Peer — (File: `sync/src/synchronizer/get_blocks_process.rs`)

### Summary

In `GetBlocksProcess::execute()`, when a node receives a `GetBlocks` message containing a batch of block hashes, encountering a single genesis hash or a single duplicate hash causes an immediate `return` with a 4xx error status. This aborts processing of all remaining valid block hashes in the batch and, because 4xx status codes trigger `should_ban()`, causes the requesting peer to be banned. The correct behavior is to `continue` (skip the offending hash) rather than `return` (abort the entire batch).

### Finding Description

`GetBlocksProcess::execute()` iterates over a peer-supplied list of block hashes and serves each one back to the requesting peer. Inside the loop, two checks perform an early `return` instead of a `continue`:

```rust
// sync/src/synchronizer/get_blocks_process.rs, lines 48–58
for block_hash in iter {
    let block_hash = block_hash.to_entity();

    if block_hash == self.synchronizer.shared().consensus().genesis_hash() {
        return StatusCode::RequestGenesis.with_context("Request genesis block"); // line 53
    }

    if !dedup.insert(block_hash.clone()) {
        return StatusCode::RequestDuplicate.with_context("Request duplicate block"); // line 57
    }
    // ... serve block ...
}
```

Both `RequestGenesis` (417) and `RequestDuplicate` (418) are 4xx status codes. The `Status::should_ban()` function maps every 4xx code (except `GetHeadersMissCommonAncestors`) to `Some(BAD_MESSAGE_BAN_TIME)`:

```rust
// sync/src/status.rs, lines 164–179
pub fn should_ban(&self) -> Option<Duration> {
    if !(400..500).contains(&(self.code as u16)) {
        return None;
    }
    match self.code {
        StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
        _ => Some(BAD_MESSAGE_BAN_TIME),
    }
}
```

So a batch of `[valid_hash_A, genesis_hash, valid_hash_B]` causes:
1. `valid_hash_A` is served correctly.
2. `genesis_hash` triggers `return StatusCode::RequestGenesis`.
3. `valid_hash_B` is **never served**.
4. The peer is **banned** for `BAD_MESSAGE_BAN_TIME`.

The same pattern exists in `sync/src/relayer/get_block_proposal_process.rs` and `sync/src/relayer/get_transactions_process.rs`, which also return `StatusCode::RequestDuplicate` from inside a per-item loop.

### Impact Explanation

- **Block sync stall**: A peer requesting a batch of blocks where any single hash is the genesis hash or a duplicate will never receive the remaining valid blocks in that batch. If the peer is also banned, it cannot retry.
- **Incorrect peer banning**: A legitimate peer with a minor implementation quirk (e.g., occasionally including a duplicate hash in a batch) is permanently banned, severing the sync connection. This degrades the node's peer pool and can slow or stall IBD/sync.
- **Attacker-amplified disruption**: An attacker who controls a peer can craft a `GetBlocks` message with a genesis hash or duplicate at position 0 to ensure zero blocks are served and the connection is terminated, forcing the victim node to find a new sync peer.

### Likelihood Explanation

The `GetBlocks` message is a standard P2P sync protocol message reachable by any unprivileged connected peer. No special privileges, keys, or majority hashpower are required. The triggering condition (genesis hash or duplicate in a batch) is trivially constructable. A legitimate peer implementation bug (e.g., off-by-one in locator construction) could also trigger this accidentally.

### Recommendation

Replace the two `return` statements inside the per-hash loop with `continue` so that the offending hash is skipped and the remaining valid hashes in the batch are still served:

```rust
if block_hash == self.synchronizer.shared().consensus().genesis_hash() {
    debug!("Skipping genesis hash request from peer={}", self.peer);
    continue; // was: return StatusCode::RequestGenesis...
}

if !dedup.insert(block_hash.clone()) {
    debug!("Skipping duplicate hash request from peer={}", self.peer);
    continue; // was: return StatusCode::RequestDuplicate...
}
```

If banning is still desired for a batch that is *entirely* composed of genesis/duplicate hashes, a counter can be tracked and the ban applied after the loop. Apply the same fix to the analogous loops in `get_block_proposal_process.rs` and `get_transactions_process.rs`.

### Proof of Concept

1. Peer A connects to a CKB node and sends a `GetBlocks` message:
   ```
   block_hashes: [valid_block_hash_1, genesis_hash, valid_block_hash_2]
   ```
2. The node enters `GetBlocksProcess::execute()`.
3. `valid_block_hash_1` is served normally (loop iteration 1).
4. `genesis_hash` is encountered → `return StatusCode::RequestGenesis` (loop iteration 2).
5. `valid_block_hash_2` is **never processed** (loop aborted).
6. The caller sees a 4xx status → `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` → Peer A is banned.
7. Peer A never receives `valid_block_hash_2`'s block data and cannot reconnect to retry. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** sync/src/synchronizer/get_blocks_process.rs (L48-58)
```rust
        for block_hash in iter {
            debug!("get_blocks {} from peer {:?}", block_hash, self.peer);
            let block_hash = block_hash.to_entity();

            if block_hash == self.synchronizer.shared().consensus().genesis_hash() {
                return StatusCode::RequestGenesis.with_context("Request genesis block");
            }

            if !dedup.insert(block_hash.clone()) {
                return StatusCode::RequestDuplicate.with_context("Request duplicate block");
            }
```

**File:** sync/src/status.rs (L107-110)
```rust
    /// Request Genesis
    RequestGenesis = 417,
    /// Request Duplicate data
    RequestDuplicate = 418,
```

**File:** sync/src/status.rs (L164-179)
```rust
    /// Whether should ban session
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```
