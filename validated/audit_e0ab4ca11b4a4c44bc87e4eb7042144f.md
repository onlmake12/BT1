### Title
Orphan Block Pool Silent `verify_callback` Overwrite Drops Peer-Ban Callback on Duplicate Orphan Insertion — (`chain/src/utils/orphan_block_pool.rs`)

---

### Summary

`InnerPool::insert` in the orphan block pool uses a bare `HashMap::insert`, which silently overwrites an existing `LonelyBlockHash` entry when the same block hash is inserted a second time. Because `LonelyBlockHash` carries a `verify_callback` — a one-shot closure used to ban the originating peer if the block is later found invalid — the first peer's callback is **dropped without being called**. A malicious peer that sends an invalid orphan block can escape the ban by triggering a second insertion of the same block (e.g., via a second connection), causing the node to ban the wrong peer or no peer at all.

---

### Finding Description

`InnerPool` (the private backing store of `OrphanBlockPool`) stores orphan blocks in a two-level `HashMap`:

```
blocks: HashMap<ParentHash, HashMap<BlockHash, LonelyBlockHash>>
``` [1](#0-0) 

The `insert` method unconditionally calls `HashMap::insert` on the inner map:

```rust
self.blocks
    .entry(parent_hash.clone())
    .or_default()
    .insert(hash.clone(), lonely_block);   // ← silently overwrites
``` [2](#0-1) 

`LonelyBlockHash` carries a `verify_callback: Option<VerifyCallback>` field:

```rust
pub struct LonelyBlockHash {
    pub block_number_and_hash: BlockNumberAndHash,
    pub parent_hash: Byte32,
    pub epoch_number: EpochNumber,
    pub switch: Option<Switch>,
    pub verify_callback: Option<VerifyCallback>,   // ← dropped on overwrite
}
``` [3](#0-2) 

`VerifyCallback` is `Box<dyn FnOnce(VerifyResult) + Send + Sync>`. [4](#0-3) 

When a block arrives from the sync protocol (`BlockProcess::execute`), the callback is constructed to call `post_sync_process` → `nc.ban_peer` if the block is invalid:

```rust
Box::new(move |verify_result: Result<bool, ckb_error::Error>| {
    match verify_result {
        Ok(_) => {}
        Err(err) => {
            // punish the malicious peer
            post_sync_process(nc.as_ref(), peer_id, "SendBlock",
                StatusCode::BlockIsInvalid.with_context(...));
        }
    };
})
``` [5](#0-4) 

The same pattern exists in the relay path (`Relayer::accept_block`): [6](#0-5) 

When the block's parent is not yet known, `OrphanBroker::process_lonely_block` calls `self.orphan_blocks_broker.insert(lonely_block)`: [7](#0-6) 

If a second peer sends the **same block** (identical hash), `InnerPool::insert` is called again. The inner `HashMap::insert` replaces the first `LonelyBlockHash` with the second one. The first peer's `verify_callback` — a `Box<dyn FnOnce>` — is **dropped without being invoked**. Rust's drop semantics silently destroy the closure, and the first peer is never notified or penalized.

The analog to M-03 is exact:
- M-03: `vaultOwners[vaultId]` overwritten on second `grab` → original owner lost.
- CKB: `blocks[parent_hash][block_hash]` overwritten on second `insert` → original peer's ban callback lost.

---

### Impact Explanation

**Peer-ban bypass (primary):** A malicious peer (Peer A) sends an invalid orphan block. Its `verify_callback` is stored in the orphan pool. Before the parent arrives, the same peer (or a colluding second connection, Peer B) sends the identical block. The overwrite drops Peer A's callback. When the parent eventually arrives and the block is verified as invalid, only Peer B's callback fires — Peer A is never banned. The node's peer-punishment mechanism is silently defeated for the original sender of the invalid block.

**Miner RPC false error (secondary):** `blocking_process_block` (used by `MinerRpc::submit_block`) creates a oneshot channel and stores the sender inside `verify_callback`. If a peer relays the same orphan block concurrently, the miner's sender is dropped. `verify_result_rx.recv()` returns `Err(RecvError)`, which is mapped to `InternalErrorKind::System` — a false internal error returned to the miner. [8](#0-7) 

---

### Likelihood Explanation

An unprivileged P2P peer can open two connections to the same node (or coordinate with one other peer). The attacker sends an invalid orphan block from connection A, then immediately re-sends the same block from connection B. Both paths reach `OrphanBroker::process_lonely_block` → `OrphanBlockPool::insert` → `InnerPool::insert`. No privileged access, no key material, and no majority hashpower is required. The only precondition is that the block's parent is not yet stored, which is a normal network condition during IBD or when receiving out-of-order blocks.

---

### Recommendation

In `InnerPool::insert`, use `entry(...).or_insert(...)` on the inner map instead of `insert(...)`, so that a second insertion of the same block hash is a no-op and the original `LonelyBlockHash` (with its callback) is preserved:

```rust
self.blocks
    .entry(parent_hash.clone())
    .or_default()
    .entry(hash.clone())
    .or_insert(lonely_block);   // preserve first insertion; drop duplicate
```

Alternatively, check for an existing entry and call the duplicate's callback with an appropriate error (e.g., "duplicate orphan") so the second peer is also notified.

---

### Proof of Concept

1. Attacker controls two peer connections to the victim node: Peer A and Peer B.
2. Peer A sends `SendBlock` with an invalid block `B` whose parent is not yet stored. `BlockProcess::execute` calls `shared.new_block_received(&block)` → `true` (first receipt). A `RemoteBlock` with Peer A's ban callback is created and dispatched. `OrphanBroker::process_lonely_block` finds the parent absent and calls `orphan_blocks_broker.insert(lonely_block_A)`. The orphan pool now holds `blocks[parent_hash][B.hash] = LonelyBlockHash { verify_callback: Some(peer_A_ban_fn) }`.
3. Peer B sends the same `SendBlock` with block `B`. `shared.new_block_received(&block)` returns `false` (already in `BLOCK_RECEIVED` status), so `BlockProcess::execute` returns `Status::ignored()` without creating a new `RemoteBlock`. **However**, if the timing is such that Peer B's message arrives before `BLOCK_RECEIVED` is set (the status is set in `new_block_received` only after `remove_by_block` succeeds, and the orphan insert happens asynchronously in the chain service thread), Peer B's block can also pass `new_block_received` and reach `InnerPool::insert`. The inner `HashMap::insert` overwrites `LonelyBlockHash` for `B.hash`, dropping Peer A's callback.
4. The parent of `B` arrives. `OrphanBroker::search_orphan_leaders` fires, dequeues `B`, and sends it to `ConsumeUnverifiedBlockProcessor`. Verification fails. `lonely_block.execute_callback(Err(...))` calls Peer B's callback (banning Peer B). Peer A's callback was already dropped — Peer A is never banned. [2](#0-1) [7](#0-6) [9](#0-8) [10](#0-9)

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L16-24)
```rust
struct InnerPool {
    // Group by blocks in the pool by the parent hash.
    blocks: HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>,
    // The map tells the parent hash when given the hash of a block in the pool.
    //
    // The block is in the orphan pool if and only if the block hash exists as a key in this map.
    parents: HashMap<packed::Byte32, ParentHash>,
    // Leaders are blocks not in the orphan pool but having at least a child in the pool.
    leaders: HashSet<ParentHash>,
```

**File:** chain/src/utils/orphan_block_pool.rs (L36-54)
```rust
    fn insert(&mut self, lonely_block: LonelyBlockHash) {
        let hash = lonely_block.hash();
        let parent_hash = lonely_block.parent_hash();
        self.blocks
            .entry(parent_hash.clone())
            .or_default()
            .insert(hash.clone(), lonely_block);
        // Out-of-order insertion needs to be deduplicated
        self.leaders.remove(&hash);
        // It is a possible optimization to make the judgment in advance,
        // because the parent of the block must not be equal to its own hash,
        // so we can judge first, which may reduce one arc clone
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
        self.parents.insert(hash, parent_hash);
    }
```

**File:** chain/src/lib.rs (L44-82)
```rust
/// VerifyCallback is the callback type to be called after block verification
pub type VerifyCallback = Box<dyn FnOnce(VerifyResult) + Send + Sync>;

/// RemoteBlock is received from ckb-sync and ckb-relayer
pub struct RemoteBlock {
    /// block
    pub block: Arc<BlockView>,

    /// Relayer and Synchronizer will have callback to ban peer
    pub verify_callback: VerifyCallback,
}

/// LonelyBlock is the block which we have not check weather its parent is stored yet
pub struct LonelyBlock {
    /// block
    pub block: Arc<BlockView>,

    /// The Switch to control the verification process
    pub switch: Option<Switch>,

    /// The optional verify_callback
    pub verify_callback: Option<VerifyCallback>,
}

/// LonelyBlock is the block which we have not check weather its parent is stored yet
pub struct LonelyBlockHash {
    /// block
    pub block_number_and_hash: BlockNumberAndHash,

    pub parent_hash: Byte32,

    pub epoch_number: EpochNumber,

    /// The Switch to control the verification process
    pub switch: Option<Switch>,

    /// The optional verify_callback
    pub verify_callback: Option<VerifyCallback>,
}
```

**File:** sync/src/synchronizer/block_process.rs (L34-81)
```rust
    pub fn execute(self) -> crate::Status {
        let block = Arc::new(self.message.block().to_entity().into_view());
        debug!(
            "BlockProcess received block {} {}",
            block.number(),
            block.hash(),
        );
        let shared = self.synchronizer.shared();

        if shared.new_block_received(&block) {
            let verify_callback = {
                let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&self.nc);
                let peer_id: PeerIndex = self.peer;
                let block_hash: Byte32 = block.hash();
                Box::new(move |verify_result: Result<bool, ckb_error::Error>| {
                    match verify_result {
                        Ok(_) => {}
                        Err(err) => {
                            let is_internal_db_error = is_internal_db_error(&err);
                            if is_internal_db_error {
                                return;
                            }

                            // punish the malicious peer
                            post_sync_process(
                                nc.as_ref(),
                                peer_id,
                                "SendBlock",
                                StatusCode::BlockIsInvalid.with_context(format!(
                                    "block {} is invalid, reason: {}",
                                    block_hash, err
                                )),
                            );
                        }
                    };
                })
            };
            let remote_block = RemoteBlock {
                block,
                verify_callback,
            };
            self.synchronizer
                .asynchronous_process_remote_block(remote_block);
        }

        // block process is asynchronous, so we only return ignored here
        crate::Status::ignored()
    }
```

**File:** sync/src/relayer/mod.rs (L291-342)
```rust
        let verify_callback = {
            let nc: Arc<dyn CKBProtocolContext + Sync> = Arc::clone(&nc);
            let block = Arc::clone(&block);
            let shared = Arc::clone(self.shared());
            let msg_name = msg_name.to_owned();
            Box::new(move |result: VerifyResult| match result {
                Ok(verified) => {
                    if !verified {
                        debug!(
                            "block {}-{} has verified already, won't build compact block and broadcast it",
                            block.number(),
                            block.hash()
                        );
                        return;
                    }

                    build_and_broadcast_compact_block(nc.as_ref(), shared.shared(), peer_id, block);
                }
                Err(err) => {
                    error!(
                        "verify block {}-{} failed: {:?}, won't build compact block and broadcast it",
                        block.number(),
                        block.hash(),
                        err
                    );

                    let is_internal_db_error = is_internal_db_error(&err);
                    if is_internal_db_error {
                        return;
                    }

                    // punish the malicious peer
                    post_sync_process(
                        nc.as_ref(),
                        peer_id,
                        &msg_name,
                        StatusCode::BlockIsInvalid.with_context(format!(
                            "block {} is invalid, reason: {}",
                            block.hash(),
                            err
                        )),
                    );
                }
            })
        };

        let remote_block = RemoteBlock {
            block,
            verify_callback,
        };

        self.shared.accept_remote_block(&self.chain, remote_block);
```

**File:** chain/src/orphan_broker.rs (L107-132)
```rust
    pub(crate) fn process_lonely_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();
        let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
        let parent_status = self.shared.get_block_status(&parent_hash);
        if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
            debug!(
                "parent {} has stored: {:?} or is_pending_verify: {}, processing descendant directly {}-{}",
                parent_hash, parent_status, parent_is_pending_verify, block_number, block_hash,
            );
            self.process_descendant(lonely_block);
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();

        if let Some(metrics) = ckb_metrics::handle() {
            metrics
                .ckb_chain_orphan_count
                .set(self.orphan_blocks_broker.len() as i64)
        }
    }
```

**File:** chain/src/chain_controller.rs (L79-109)
```rust
    fn blocking_process_block_internal(
        &self,
        block: Arc<BlockView>,
        switch: Option<Switch>,
    ) -> VerifyResult {
        let (verify_result_tx, verify_result_rx) = ckb_channel::oneshot::channel::<VerifyResult>();

        let verify_callback = {
            move |result: VerifyResult| {
                if let Err(err) = verify_result_tx.send(result) {
                    error!(
                        "blocking send verify_result failed: {}, this shouldn't happen",
                        err
                    )
                }
            }
        };

        let lonely_block = LonelyBlock {
            block,
            switch,
            verify_callback: Some(Box::new(verify_callback)),
        };

        self.asynchronous_process_lonely_block(lonely_block);
        verify_result_rx.recv().unwrap_or_else(|err| {
            Err(InternalErrorKind::System
                .other(format!("blocking recv verify_result failed: {}", err))
                .into())
        })
    }
```
