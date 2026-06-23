### Title
Unsolicited `SendBlock` Message Accepted Without Sender Verification, Consuming Inflight Slot and Potentially Stalling Sync — (File: `sync/src/synchronizer/block_process.rs`)

---

### Summary

The `SendBlock` P2P message handler in CKB's synchronizer accepts a full block from **any** connected peer without verifying that the sender is the peer that was originally assigned to fetch that block. The inflight tracking entry is removed by block hash alone, not by `(peer, block_hash)`. A malicious connected peer can race to send a `SendBlock` for any block currently in-flight, consuming the inflight slot and causing the node to process block data from the attacker instead of the assigned peer.

---

### Finding Description

When the CKB synchronizer needs a block, it assigns the download to a specific peer via `InflightBlocks::insert`, which records `(block_hash → assigned_peer)` in `inflight_states`. [1](#0-0) 

When a `SendBlock` message arrives, `BlockProcess::execute` calls `shared.new_block_received(&block)`: [2](#0-1) 

`new_block_received` calls `remove_by_block((block.number(), block.hash()).into())`, which removes the inflight entry **keyed only by block hash**, with no check that the sending peer matches the originally assigned peer: [3](#0-2) 

`remove_by_block` itself updates the download scheduler for `state.peer` (the originally assigned peer), not the actual sender: [4](#0-3) 

After `new_block_received` returns `true`, the block status is set to `BLOCK_RECEIVED` and the block body from the attacker is submitted for asynchronous verification. The `BlockFetcher` will not re-request a block already marked `BLOCK_RECEIVED`: [5](#0-4) 

By contrast, the `BlockTransactions` relay handler **does** correctly gate processing on the sending peer being present in the `peers_map` for that compact block: [6](#0-5) 

The `SendBlock` handler has no equivalent check.

---

### Impact Explanation

A malicious connected peer that observes (or infers) which block hashes are currently in-flight can send a `SendBlock` message for any of those hashes before the legitimate assigned peer responds. This:

1. **Consumes the inflight slot** — `remove_by_block` removes the entry; the assigned peer's slot is gone.
2. **Substitutes attacker-controlled block body** — the node processes the block from the attacker's data. The block hash must match a previously header-validated hash (so the attacker cannot fabricate a new chain tip), but the transaction list can be wrong (not matching `transactions_root`).
3. **Triggers verification failure** — the block fails verification (transactions don't match `transactions_root`). The attacker's peer is banned, but the block is now marked `BLOCK_RECEIVED` (and subsequently `BLOCK_INVALID`).
4. **Prevents re-download** — the `BlockFetcher` skips blocks already marked `BLOCK_RECEIVED`, and a block marked `BLOCK_INVALID` will not be re-accepted. The node stalls syncing that specific block until a recovery path (e.g., timeout-based prune cycle) triggers a re-request — if one exists for the `BLOCK_INVALID` state.

Additionally, the download scheduler timing credit is applied to the originally assigned peer (peer A), not the attacker (peer B), corrupting peer performance statistics.

The scope is a targeted sync-stall attack against individual nodes, not a global network shutdown, but it is reachable by any unprivileged connected peer.

---

### Likelihood Explanation

- Any peer that completes the CKB P2P handshake can send `SendBlock` messages.
- The set of in-flight block hashes is trivially inferable: the attacker observes `GetBlocks` messages sent by the victim node, or simply knows the current chain tip and sync frontier from the public blockchain.
- No special privilege, key material, or majority hashpower is required.
- The race window is the round-trip time between the victim sending `GetBlocks` and the legitimate peer responding — easily won by a local or low-latency attacker.

---

### Recommendation

In `new_block_received` (or in `BlockProcess::execute` before calling it), verify that the sending peer matches the peer recorded in `inflight_states` for the given block hash. Specifically:

1. Expose a `inflight_state_by_block` lookup before removing the entry.
2. Compare `state.peer` with the `peer_index` of the incoming `SendBlock` message.
3. If they do not match, either ignore the message or apply a mild penalty (not a ban, since unsolicited blocks can arrive legitimately during reorgs), but do **not** consume the inflight slot.

This mirrors the existing correct pattern in `BlockTransactionsProcess`, which gates processing on `peers_map.entry(self.peer)`. [7](#0-6) 

---

### Proof of Concept

1. Connect a malicious peer M to a victim CKB node V.
2. Observe V sending a `GetBlocks` message containing hash `H` (block at the sync frontier).
3. Before the legitimate peer responds, M sends a `SendBlock` message containing a block with hash `H` but with a transaction list that does not match the `transactions_root` in the header.
4. V's `BlockProcess::execute` calls `new_block_received`, which returns `true` (hash `H` is in inflight), removes the inflight slot, and marks the block `BLOCK_RECEIVED`.
5. Asynchronous verification fails (transactions_root mismatch). M is banned.
6. V's `BlockFetcher` skips hash `H` on the next cycle because it is already `BLOCK_RECEIVED`. V stalls at block `H`.

### Citations

**File:** sync/src/types/mod.rs (L624-626)
```rust
    pub fn inflight_state_by_block(&self, block: &BlockNumberAndHash) -> Option<&InflightState> {
        self.inflight_states.get(block)
    }
```

**File:** sync/src/types/mod.rs (L748-764)
```rust
    pub fn insert(&mut self, peer: PeerIndex, block: BlockNumberAndHash) -> bool {
        let state = self.inflight_states.entry(block.clone());
        match state {
            Entry::Occupied(_entry) => return false,
            Entry::Vacant(entry) => entry.insert(InflightState::new(peer)),
        };

        if self.restart_number >= block.number {
            // All new requests smaller than restart_number mean that they are cleaned up and
            // cannot be immediately marked as cleaned up again.
            self.trace_number
                .insert(block.clone(), unix_time_as_millis());
        }

        let download_scheduler = self.download_schedulers.entry(peer).or_default();
        download_scheduler.hashes.insert(block)
    }
```

**File:** sync/src/types/mod.rs (L785-819)
```rust
    pub fn remove_by_block(&mut self, block: BlockNumberAndHash) -> bool {
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let download_schedulers = &mut self.download_schedulers;
        let trace = &mut self.trace_number;
        let time_analyzer = &mut self.time_analyzer;
        let adjustment = self.adjustment;
        self.inflight_states
            .remove(&block)
            .map(|state| {
                let elapsed = unix_time_as_millis().saturating_sub(state.timestamp);
                if let Some(set) = download_schedulers.get_mut(&state.peer) {
                    set.hashes.remove(&block);
                    if adjustment {
                        match time_analyzer.push_time(elapsed) {
                            TimeQuantile::MinToFast => set.increase(2),
                            TimeQuantile::FastToNormal => set.increase(1),
                            TimeQuantile::NormalToUpper => {
                                if should_punish {
                                    set.decrease(1)
                                }
                            }
                            TimeQuantile::UpperToMax => {
                                if should_punish {
                                    set.decrease(2)
                                }
                            }
                        }
                    }
                    if !trace.is_empty() {
                        trace.remove(&block);
                    }
                };
            })
            .is_some()
    }
```

**File:** sync/src/types/mod.rs (L1200-1227)
```rust
    pub fn new_block_received(&self, block: &core::BlockView) -> bool {
        if !self
            .state()
            .write_inflight_blocks()
            .remove_by_block((block.number(), block.hash()).into())
        {
            return false;
        }

        let status = self.active_chain().get_block_status(&block.hash());
        debug!(
            "new_block_received {}-{}, status: {:?}",
            block.number(),
            block.hash(),
            status
        );
        if !BlockStatus::HEADER_VALID.eq(&status) {
            return false;
        }

        if let dashmap::mapref::entry::Entry::Vacant(status) =
            self.shared().block_status_map().entry(block.hash())
        {
            status.insert(BlockStatus::BLOCK_RECEIVED);
            return true;
        }
        false
    }
```

**File:** sync/src/synchronizer/block_process.rs (L34-77)
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
```

**File:** sync/src/synchronizer/block_fetcher.rs (L269-271)
```rust
                } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
                    // Do not download repeatedly
                } else if (matches!(self.ibd, IBDState::In)
```

**File:** sync/src/relayer/block_transactions_process.rs (L65-73)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
            let (compact_block, peers_map, _) = pending.get_mut();
            if let Entry::Occupied(mut value) = peers_map.entry(self.peer) {
                let (expected_transaction_indexes, expected_uncle_indexes) = value.get_mut();
```
