### Title
`HeaderAcceptor` Accepts `BLOCK_INVALID` Headers as `HEADER_VALID`, Bypassing Invalidity Status - (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the sync module contains a developer-acknowledged `FIXME` comment noting that when a header's status is `BLOCK_INVALID`, the function should return early but does not. As a result, a header previously marked invalid can be re-ingested via a `SendHeaders` P2P message and accepted as `HEADER_VALID`, updating the peer's best-known header to an invalid chain and potentially corrupting the node's sync state.

---

### Finding Description

CKB tracks block/header validity through `BlockStatus` flags stored in an in-memory `block_status_map` and the persistent `block_ext.verified` field in the database. The relevant statuses are:

- `BLOCK_INVALID` — set when a block fails non-contextual or contextual verification
- `HEADER_VALID` — set when a header passes all header-level checks [1](#0-0) 

When a peer sends a `SendHeaders` message, `HeaderAcceptor::accept()` is called for each header. The function reads the current status of the header: [2](#0-1) 

The critical flaw is on lines 301–302: there is an explicit `FIXME` comment acknowledging that when `status == BLOCK_INVALID`, the code **should** return early but does not. The only early-return guard is `status.contains(BlockStatus::HEADER_VALID)`. Since `BLOCK_INVALID` (`1 << 12`) does not contain the `HEADER_VALID` bit (`1`), a `BLOCK_INVALID` header falls through all three subsequent checks:

1. `prev_block_check` — only rejects if the **parent** is `BLOCK_INVALID`, not the header itself
2. `non_contextual_check` — runs `HeaderVerifier::verify()` (PoW, number, epoch, timestamp); a header with a valid PoW but an invalid block body passes this
3. `version_check` — checks `version == 0` [3](#0-2) 

If all three pass, `sync_shared.insert_valid_header(self.peer, self.header)` is called at line 356, which:
- Inserts the header into the `header_map` as `HEADER_VALID`
- Updates the peer's best-known header to this previously-invalid chain [4](#0-3) 

The `get_block_status` function checks `block_status_map` first, then `header_map`, then the DB: [5](#0-4) 

Once `insert_valid_header` writes to `header_map`, if the `block_status_map` entry for this hash is later removed (e.g., via `remove_block_status` on a successful verification path, or after a node restart clears the in-memory map), `get_block_status` will return `HEADER_VALID` for a header that was previously determined to be part of an invalid block. This is the direct analog to the external report: an entity marked as resolving incorrectly is re-ingested by a verifier that skips the invalidity flag check.

---

### Impact Explanation

An attacker can cause a CKB node to:

1. **Corrupt peer best-known-header state**: The node's record of a peer's best chain tip is set to an invalid chain, skewing sync decisions and fork-choice heuristics.
2. **Trigger redundant block downloads**: The node may issue `GetBlocks` requests for blocks on the invalid chain, wasting bandwidth and CPU.
3. **Erase the `BLOCK_INVALID` guard**: After `insert_valid_header` writes to `header_map`, if the `block_status_map` entry is evicted or cleared (restart), subsequent `get_block_status` calls return `HEADER_VALID` instead of `BLOCK_INVALID`, allowing the invalid header to propagate further through the sync pipeline without triggering the invalidity guard in `ConsumeUnverifiedBlockProcessor::verify_block`. [6](#0-5) 

---

### Likelihood Explanation

The attack is reachable by any unprivileged P2P peer:

1. Peer sends a block with a valid header (valid PoW, correct number/epoch/timestamp) but an invalid body (e.g., a transaction with no inputs, an empty cellbase, or a malformed uncle). This is the same technique used in the existing integration test `ChainContainsInvalidBlock`.
2. The node marks the block `BLOCK_INVALID` via `asynchronous_process_block` → `non_contextual_verify` failure.
3. The same peer (or any peer) sends the same header again via `SendHeaders`.
4. `HeaderAcceptor::accept()` is called; the `BLOCK_INVALID` check is absent; the header passes all three sub-checks and is inserted as `HEADER_VALID`. [7](#0-6) 

The `FIXME` comment in the production source code confirms the developers are aware of this gap.

---

### Recommendation

In `HeaderAcceptor::accept()`, add an explicit early-return guard for `BLOCK_INVALID` immediately after reading the status, before the `HEADER_VALID` check:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

// Reject headers that are already known to be invalid.
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
}
```

This mirrors the guard already present in `contextual_check` for compact blocks: [8](#0-7) 

and in `OrphanBroker::process_lonely_block`: [9](#0-8) 

---

### Proof of Concept

1. Connect a malicious peer to a CKB node.
2. Construct a block `B` at height `N` with a valid header (valid PoW, correct number/epoch/timestamp relative to the tip) but an invalid body (e.g., a transaction with zero inputs: `TransactionBuilder::default().build()` — the same pattern used in `test/src/specs/sync/invalid_block.rs`). [10](#0-9) 

3. Send block `B` to the node via `SendBlock`. The node runs `non_contextual_verify`, which fails; the node sets `block_status_map[B.hash] = BLOCK_INVALID`.
4. Immediately send the header of `B` again via `SendHeaders`.
5. `HeaderAcceptor::accept()` is called. `get_block_status(B.hash)` returns `BLOCK_INVALID`. The `HEADER_VALID` guard does not fire. `prev_block_check`, `non_contextual_check` (header-only PoW/number/epoch/timestamp), and `version_check` all pass.
6. `insert_valid_header` is called: `header_map[B.hash] = HEADER_VALID`; the peer's best-known header is updated to `B`.
7. The node now believes the peer's best chain tip is the invalid block `B` and may issue `GetBlocks` for it, wasting resources and corrupting sync state.

### Citations

**File:** shared/src/block_status.rs (L1-17)
```rust
//! Provide BlockStatus
#![allow(missing_docs)]
#![allow(clippy::bad_bit_mask)]

use bitflags::bitflags;
bitflags! {
    #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L301-322)
```rust
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
```

**File:** sync/src/synchronizer/headers_process.rs (L324-357)
```rust
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
```

**File:** shared/src/shared.rs (L425-444)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
```

**File:** chain/src/verify.rs (L243-252)
```rust
        {
            let parent_status = self.shared.get_block_status(&parent_hash);
            if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
                return Err(InternalErrorKind::Other
                    .other(format!(
                        "block: {}'s parent: {} previously verified failed",
                        block_hash, parent_hash
                    ))
                    .into());
            }
```

**File:** chain/src/chain_service.rs (L117-131)
```rust
        if lonely_block.switch().is_none()
            || matches!(lonely_block.switch(), Some(switch) if !switch.disable_non_contextual())
        {
            let result = self.non_contextual_verify(lonely_block.block());
            if let Err(err) = result {
                error!(
                    "block {}-{} verify failed: {:?}",
                    block_number, block_hash, err
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
                return;
            }
        }
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** chain/src/orphan_broker.rs (L119-120)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
```

**File:** test/src/specs/sync/invalid_block.rs (L35-41)
```rust
        let invalid_block = bad_node
            .new_block_builder(None, None, None)
            .transaction(TransactionBuilder::default().build())
            .build();
        let invalid_number = invalid_block.header().number();
        let invalid_hash = bad_node.process_block_without_verify(&invalid_block, false);
        bad_node.mine(3);
```
