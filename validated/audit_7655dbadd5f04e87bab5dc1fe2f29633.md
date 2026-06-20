### Title
`BLOCK_INVALID` Status Overwritten to `HEADER_VALID` via Peer-Sent `SendHeaders` — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the sync subsystem is missing a guard for the `BLOCK_INVALID` block status. Any P2P peer can send a `SendHeaders` message containing a header that was previously marked `BLOCK_INVALID` (e.g., after failed contextual block verification). Because the function only short-circuits on `HEADER_VALID` and not on `BLOCK_INVALID`, the header passes all three lightweight checks and `insert_valid_header` is called, silently overwriting the `BLOCK_INVALID` entry in `block_status_map` with `HEADER_VALID`. The developers themselves flagged this with a `FIXME` comment at the exact location.

---

### Finding Description

In `HeaderAcceptor::accept()`, the status of the incoming header is read from `block_status_map`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best-known and return
    return result;
}
``` [1](#0-0) 

The guard only returns early when the status already contains `HEADER_VALID`. `BLOCK_INVALID` does **not** contain `HEADER_VALID` (they are disjoint bit-flag values): [2](#0-1) 

So when `status == BLOCK_INVALID`, execution falls through to the three lightweight checks:

1. `prev_block_check` — only rejects if the **parent** is `BLOCK_INVALID`
2. `non_contextual_check` — only rejects on structural/PoW header invalidity
3. `version_check` — only rejects on non-zero version [3](#0-2) 

A block can be marked `BLOCK_INVALID` by the chain verifier after **contextual** verification fails (e.g., invalid transactions, script execution failure, capacity overflow): [4](#0-3) 

Such a block has a structurally valid header (valid PoW, valid parent hash, version 0). When a peer re-sends that header via `SendHeaders`, all three lightweight checks pass and `insert_valid_header` is called: [5](#0-4) 

`insert_valid_header` is documented to update `block_status_map`, `header_map`, `shared_best_header`, and the peer's `best_known_header`: [6](#0-5) 

`insert_block_status` performs an unconditional overwrite with no existence check: [7](#0-6) 

---

### Impact Explanation

After the overwrite, the block's status transitions from `BLOCK_INVALID` → `HEADER_VALID`. Downstream sync logic uses `block_status_map` to gate block download and processing decisions:

- `asynchronous_process_remote_block` only processes blocks with `HEADER_VALID` status; a re-validated header re-enables download of the previously-rejected block.
- `block_fetcher` skips blocks with `BLOCK_STORED` or `BLOCK_RECEIVED` but will re-queue a block whose status has been reset to `HEADER_VALID`.
- If the invalid block's chain has higher total difficulty, `shared_best_header` may be updated, causing the node to treat an invalid chain as the best known chain. [8](#0-7) 

The practical result is: the node repeatedly downloads, re-submits, and re-fails contextual verification of the same invalid block in a loop, wasting CPU and bandwidth, and potentially stalling or disrupting the sync state machine.

---

### Likelihood Explanation

Any unauthenticated inbound or outbound P2P peer can send a `SendHeaders` message at any time. The `HeadersProcess::execute()` handler is invoked for every such message with no rate-limiting specific to already-invalid blocks. The attacker only needs to know (or guess) the hash of a block that was previously rejected by the target node — which is trivially discoverable by observing the network or by deliberately crafting and submitting an invalid block first. [9](#0-8) 

---

### Recommendation

Add an explicit early-return guard for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, immediately after reading the status and before the `HEADER_VALID` check. The existing `FIXME` comment marks the exact location:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
// Add this guard:
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing early-return logic
}
``` [1](#0-0) 

---

### Proof of Concept

1. Connect a peer to a CKB node.
2. Submit a block whose header is structurally valid (valid PoW, valid parent, version 0) but whose transactions fail script verification. The node marks it `BLOCK_INVALID` via `insert_block_status(..., BlockStatus::BLOCK_INVALID)` in `chain/src/verify.rs:177`.
3. From the same peer, send a `SendHeaders` P2P message containing that block's header.
4. `HeadersProcess::execute()` → `HeaderAcceptor::accept()` is called. `status == BLOCK_INVALID`, which does not contain `HEADER_VALID`, so the early-return is skipped.
5. `prev_block_check` passes (parent is valid), `non_contextual_check` passes (header PoW is valid), `version_check` passes (version == 0).
6. `sync_shared.insert_valid_header(peer, header)` is called, overwriting `BLOCK_INVALID` with `HEADER_VALID` in `block_status_map`.
7. The block is now eligible for re-download and re-processing. The cycle repeats indefinitely. [10](#0-9) [11](#0-10)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L94-130)
```rust
    pub fn execute(self) -> Status {
        debug!("HeadersProcess begins");
        let shared: &SyncShared = self.synchronizer.shared();
        let consensus = shared.consensus();
        let headers = self
            .message
            .headers()
            .to_entity()
            .into_iter()
            .map(packed::Header::into_view)
            .collect::<Vec<_>>();

        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }

        if headers.is_empty() {
            // Empty means that the other peer's tip may be consistent with our own best known,
            // but empty cannot 100% confirm this, so it does not set the other peer's best header
            // to the shared best known.
            // This action means that if the newly connected node has not been sync with headers,
            // it cannot be used as a synchronization node.
            debug!("HeadersProcess is_empty (synchronized)");
            if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
                self.synchronizer
                    .shared()
                    .state()
                    .tip_synced(state.value_mut());
            }
            return Status::ok();
        }

        if !self.is_continuous(&headers) {
            warn!("HeadersProcess is not continuous");
            return StatusCode::HeadersIsInvalid.with_context("not continuous");
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L295-358)
```rust
    pub fn accept(&self) -> ValidationResult {
        let mut result = ValidationResult::default();
        let sync_shared = self.active_chain.sync_shared();
        let state = self.active_chain.state();
        let shared = sync_shared.shared();

        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
            let header_index = sync_shared
                .get_header_index_view(
                    &self.header.hash(),
                    status.contains(BlockStatus::BLOCK_STORED),
                )
                .unwrap_or_else(|| {
                    panic!(
                        "header {}-{} with HEADER_VALID should exist",
                        self.header.number(),
                        self.header.hash()
                    )
                })
                .as_header_index();
            state
                .peers()
                .may_set_best_known_header(self.peer, header_index);
            return result;
        }

        if self.prev_block_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-parent header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        sync_shared.insert_valid_header(self.peer, self.header);
        result
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

**File:** chain/src/verify.rs (L153-181)
```rust
            Err(err) => {
                error!("verify block {} failed: {}", block_hash, err);

                let tip = self
                    .shared
                    .store()
                    .get_tip_header()
                    .expect("tip_header must exist");
                let tip_ext = self
                    .shared
                    .store()
                    .get_block_ext(&tip.hash())
                    .expect("tip header's ext must exist");

                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));

                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```

**File:** sync/src/types/mod.rs (L1089-1094)
```rust
    /// Sync a new valid header, try insert to sync state
    // Update the header_map
    // Update the block_status_map
    // Update the shared_best_header if need
    // Update the peer's best_known_header
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
```

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
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
