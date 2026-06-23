### Title
Double Block Processing via Concurrent Sync and Relay Paths — (`sync/src/types/mod.rs`, `sync/src/relayer/mod.rs`)

---

### Summary

`SyncShared::accept_remote_block` unconditionally forwards every block to the chain processing pipeline regardless of whether the block is already in `BLOCK_RECEIVED` state. Both `Relayer::accept_block` and `Synchronizer::asynchronous_process_remote_block` guard only against `BLOCK_STORED`, not `BLOCK_RECEIVED`. When the same block arrives via both the Sync protocol (SendBlock) and the Relay protocol (CompactBlock/BlockTransactions) within the same processing window, the block is submitted to `ChainService` twice, causing double non-contextual verification, double DB insertion attempts, double enqueuing into the unverified-block pipeline, and a spurious error callback that can falsely punish the relaying peer.

---

### Finding Description

**Two independent entry paths both call `accept_remote_block` without checking `BLOCK_RECEIVED`:**

**Path 1 — Sync protocol** (`sync/src/synchronizer/mod.rs`):

```rust
pub fn asynchronous_process_remote_block(&self, remote_block: RemoteBlock) {
    let status = self.shared.active_chain().get_block_status(&block_hash);
    // NOTE: Filtering BLOCK_STORED but not BLOCK_RECEIVED
    if status.contains(BlockStatus::BLOCK_STORED) {
        error!("Block {} already stored", block_hash);
    } else if status.contains(BlockStatus::HEADER_VALID) {
        self.shared.accept_remote_block(&self.chain, remote_block); // ← called even if BLOCK_RECEIVED
    }
}
```

Because `BLOCK_RECEIVED` is a superset of `HEADER_VALID` (bitflag encoding: `BLOCK_RECEIVED = 1 | (HEADER_VALID << 1)`), a block already in `BLOCK_RECEIVED` state passes the `status.contains(BlockStatus::HEADER_VALID)` check and is forwarded again.

**Path 2 — Relay protocol** (`sync/src/relayer/mod.rs`):

```rust
pub fn accept_block(...) {
    if self.shared().active_chain()
        .contains_block_status(&block.hash(), BlockStatus::BLOCK_STORED) {
        return; // only guards BLOCK_STORED, not BLOCK_RECEIVED
    }
    ...
    self.shared.accept_remote_block(&self.chain, remote_block); // ← called even if BLOCK_RECEIVED
}
```

**The shared gateway `accept_remote_block` does not gate on `BLOCK_RECEIVED`** (`sync/src/types/mod.rs`):

```rust
pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
    {
        let entry = self.shared().block_status_map()
            .entry(remote_block.block.header().hash());
        if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
            entry.insert(BlockStatus::BLOCK_RECEIVED);
        }
        // ↑ Vacant guard only protects the status write, NOT the forwarding below
    }
    chain.asynchronous_process_remote_block(remote_block) // ← unconditional
}
```

The `Entry::Vacant` guard prevents a second status write but does **not** prevent the block from being forwarded to `ChainController::asynchronous_process_remote_block` a second time.

**`ChainService::asynchronous_process_block` has no deduplication guard** (`chain/src/chain_service.rs`):

```rust
fn asynchronous_process_block(&self, lonely_block: LonelyBlock) {
    ...
    if let Err(err) = self.insert_block(&lonely_block) {
        self.shared.block_status_map().remove(&block_hash); // ← removes status on failure
        lonely_block.execute_callback(Err(err));            // ← fires error callback
        return;
    }
    self.orphan_broker.process_lonely_block(lonely_block.into()); // ← enqueues for verification
}
```

When the second copy arrives:
1. `insert_block` fails (block already in RocksDB) or silently overwrites.
2. `block_status_map().remove(&block_hash)` deletes the `BLOCK_RECEIVED` entry, making the block appear `BLOCK_UNKNOWN` to subsequent status checks.
3. `execute_callback(Err(err))` fires the relay callback, which calls `post_sync_process` to punish the peer that relayed the valid block.

If `insert_block` succeeds (RocksDB `put` semantics overwrite), the block is enqueued for verification a second time via `orphan_broker.process_lonely_block` → `send_unverified_block`, causing the `ConsumeUnverifiedBlocks` thread to verify the same block twice.

---

### Impact Explanation

1. **False peer punishment**: A peer that legitimately relays a valid block via the Relay protocol is punished (`post_sync_process` with `BlockIsInvalid`) because the second processing attempt fails after the first already inserted the block. This can cause the node to ban honest peers, degrading network connectivity.

2. **Block status corruption**: `block_status_map().remove(&block_hash)` is called while the first processing path is still active. Subsequent `get_block_status` calls fall through to the DB, which may return `BLOCK_UNKNOWN` (if `block_ext` is not yet written), causing the block to be re-requested and re-processed in a loop.

3. **Resource exhaustion (DoS)**: Double non-contextual verification and double enqueuing into the unverified-block pipeline consume CPU and memory. An attacker controlling multiple peers can amplify this by sending the same block via both protocols repeatedly across many connections.

---

### Likelihood Explanation

**Medium.** During normal operation — especially during IBD or when a node is catching up — it is common for the same block to arrive via both the Sync protocol (explicit `GetBlocks`/`SendBlock`) and the Relay protocol (CompactBlock broadcast). The vulnerable window is the time between `BLOCK_RECEIVED` being set and `BLOCK_STORED` being set, which spans the entire non-contextual verification and DB insertion phase. Under load this window is non-trivial. No special attacker capability is required beyond connecting to the target node as a peer and sending the same block via both protocols.

---

### Recommendation

1. In `SyncShared::accept_remote_block`, gate the forwarding on the `Vacant` result — only call `chain.asynchronous_process_remote_block` when the entry was actually vacant (i.e., the block was not already in `BLOCK_RECEIVED` or higher state):

```rust
pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
    let is_new = {
        let entry = self.shared().block_status_map()
            .entry(remote_block.block.header().hash());
        if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
            entry.insert(BlockStatus::BLOCK_RECEIVED);
            true
        } else {
            false
        }
    };
    if is_new {
        chain.asynchronous_process_remote_block(remote_block);
    }
}
```

2. In `Relayer::accept_block`, add a `BLOCK_RECEIVED` guard analogous to the `BLOCK_STORED` guard, or rely on the fix in `accept_remote_block` above.

3. In `ChainService::asynchronous_process_block`, add an explicit check before `insert_block` to detect if the block is already stored, and return early without removing the status or firing an error callback.

---

### Proof of Concept

1. Connect two sessions (peer A and peer B) to a target CKB node.
2. Peer A sends a `SendBlock` message for block X via the Sync protocol. The node sets `block_status[X] = BLOCK_RECEIVED` and enqueues X in the chain channel.
3. Before the chain thread processes X (i.e., before `BLOCK_STORED` is set), peer B sends a CompactBlock + BlockTransactions for the same block X via the Relay protocol.
4. `Relayer::accept_block` sees `BLOCK_RECEIVED` (not `BLOCK_STORED`) and calls `accept_remote_block`, which unconditionally enqueues X in the chain channel a second time.
5. The chain thread processes the first copy: `insert_block` succeeds, X is enqueued for verification.
6. The chain thread processes the second copy: `insert_block` fails (or overwrites), `block_status_map().remove(X)` is called, and the relay callback fires `Err(...)`, causing peer B to be punished for sending a valid block. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** sync/src/types/mod.rs (L1075-1087)
```rust
    pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
        {
            let entry = self
                .shared()
                .block_status_map()
                .entry(remote_block.block.header().hash());
            if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
                entry.insert(BlockStatus::BLOCK_RECEIVED);
            }
        }

        chain.asynchronous_process_remote_block(remote_block)
    }
```

**File:** sync/src/relayer/mod.rs (L281-287)
```rust
        if self
            .shared()
            .active_chain()
            .contains_block_status(&block.hash(), BlockStatus::BLOCK_STORED)
        {
            return;
        }
```

**File:** sync/src/synchronizer/mod.rs (L470-486)
```rust
    pub fn asynchronous_process_remote_block(&self, remote_block: RemoteBlock) {
        let block_hash = remote_block.block.hash();
        let status = self.shared.active_chain().get_block_status(&block_hash);
        // NOTE: Filtering `BLOCK_STORED` but not `BLOCK_RECEIVED`, is for avoiding
        // stopping synchronization even when orphan_pool maintains dirty items by bugs.
        if status.contains(BlockStatus::BLOCK_STORED) {
            error!("Block {} already stored", block_hash);
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
        } else {
            debug!(
                "Synchronizer process_new_block unexpected status {:?} {}",
                status, block_hash,
            );
            // TODO which error should we return?
        }
    }
```

**File:** chain/src/chain_service.rs (L133-144)
```rust
        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
        }

        self.orphan_broker.process_lonely_block(lonely_block.into());
    }
```

**File:** chain/src/chain_service.rs (L146-151)
```rust
    fn insert_block(&self, lonely_block: &LonelyBlock) -> Result<(), ckb_error::Error> {
        let db_txn = self.shared.store().begin_transaction();
        db_txn.insert_block(lonely_block.block())?;
        db_txn.commit()?;
        Ok(())
    }
```

**File:** shared/src/block_status.rs (L8-17)
```rust
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```
