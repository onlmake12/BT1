### Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept()` Allows State Machine Re-entry and Cache Bypass — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` contains an acknowledged `FIXME` comment noting that it does not return early when a block's status is `BLOCK_INVALID`. Because the only early-exit guard checks for `BLOCK_INVALID`'s absence of the `HEADER_VALID` bit (which is a disjoint bit), a header previously condemned as `BLOCK_INVALID` is silently re-processed through all non-contextual checks. If those checks pass, `insert_valid_header` overwrites the `BLOCK_INVALID` entry with `HEADER_VALID`, causing the node to re-request and re-verify a block it already determined to be invalid. This is the CKB analog of the external report's state machine inconsistency: a status transition that should be terminal is instead reversible, allowing a second call to the same logic to produce a different outcome.

---

### Finding Description

`HeaderAcceptor::accept()` begins with a status check:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ...
    return result;
}
```

`BlockStatus` is defined as a bitflag set:

```
HEADER_VALID  = 1
BLOCK_INVALID = 1 << 12  (= 4096)
```

`BLOCK_INVALID` does **not** contain the `HEADER_VALID` bit, so `status.contains(BlockStatus::HEADER_VALID)` evaluates to `false` for a `BLOCK_INVALID` block. The function falls through to `prev_block_check`, `non_contextual_check`, and `version_check`. If all three pass, `sync_shared.insert_valid_header(self.peer, self.header)` is called, which calls `insert_block_status` and overwrites `BLOCK_INVALID` with `HEADER_VALID` in the shared `DashMap`.

The `get_block_status` / `insert_block_status` pair is a plain read-then-overwrite on a `DashMap` with no atomic compare-and-swap:

```rust
pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
    match self.block_status_map().get(block_hash) {
        Some(status_ref) => *status_ref.value(),
        // ...
    }
}

pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
    self.block_status_map.insert(block_hash, status);
}
```

The state machine transition that should be one-way (`BLOCK_INVALID` is terminal) is therefore reversible: `BLOCK_INVALID → HEADER_VALID`.

The concrete path that sets `BLOCK_INVALID` in `chain_service.rs::asynchronous_process_block` is:

```rust
if let Err(err) = result {
    self.shared
        .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
    lonely_block.execute_callback(Err(err));
    return;
}
```

And in `verify.rs::consume_unverified_blocks`:

```rust
Err(err) => {
    // ...
    self.shared
        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
}
```

After either of these paths fires, a peer can re-send the same header. `HeaderAcceptor::accept()` will re-run all checks. If the header is structurally valid (passes `HeaderVerifier`) but the block body fails full contextual verification, the header will be re-accepted as `HEADER_VALID`, the node will re-request the block body, and the cycle repeats.

---

### Impact Explanation

1. **BLOCK_INVALID cache bypass / repeated re-verification**: The `BLOCK_INVALID` status is the node's mechanism to avoid re-processing blocks already determined to be invalid. The missing guard allows a malicious peer to repeatedly cause the node to re-download and re-verify the same invalid block body, consuming bandwidth and CPU proportional to the number of re-sends.

2. **Peer ban evasion**: When the node re-requests a block body (because the header was re-accepted), the subsequent block failure is attributed to the node's own re-request, not to the peer's unsolicited send. The peer avoids the ban that would normally follow sending an invalid block.

3. **Sync disruption**: In IBD, the node fetches blocks from a single selected peer. If that peer exploits this to keep the node cycling on invalid blocks, it can stall synchronization indefinitely.

---

### Likelihood Explanation

The attack requires a peer to possess a header that:
- Is structurally valid (passes `HeaderVerifier` / `non_contextual_check`)
- Has a valid parent (passes `prev_block_check`)
- Has version 0 (passes `version_check`)
- But whose full block body fails contextual verification (e.g., invalid transactions, bad merkle root, script failure)

This is a realistic scenario: a miner can construct a block with a valid header and PoW but with an invalid transaction set. The header passes all three checks in `accept()`, but the block body fails `ConsumeUnverifiedBlockProcessor::verify_block`. The peer can then repeatedly re-send the header to keep the node cycling.

The FIXME comment in the source code confirms the developers are aware of the missing guard and have not yet resolved it.

---

### Recommendation

Add an explicit early-return guard for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, before the `HEADER_VALID` check:

```rust
pub fn accept(&self) -> ValidationResult {
    let mut result = ValidationResult::default();
    let sync_shared = self.active_chain.sync_shared();
    let state = self.active_chain.state();
    let shared = sync_shared.shared();

    let status = self.active_chain.get_block_status(&self.header.hash());

+   // If the block was previously determined to be invalid, reject immediately.
+   if status.contains(BlockStatus::BLOCK_INVALID) {
+       result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
+       return result;
+   }

    if status.contains(BlockStatus::HEADER_VALID) {
        // ...
        return result;
    }
    // ...
}
```

This mirrors the fix pattern in the external report: add the missing guard condition so that a terminal state (`BLOCK_INVALID` / `bothOraclesUntrusted`) cannot be silently re-entered and overwritten.

---

### Proof of Concept

**Step 1**: Attacker constructs block `B` with a valid header (valid PoW, valid parent, version 0) but an invalid body (e.g., a transaction that fails script execution).

**Step 2**: Attacker sends `B`'s header via `SendHeaders`. `HeaderAcceptor::accept()` runs; all three checks pass; `insert_valid_header` marks the header `HEADER_VALID`. Node requests block body.

**Step 3**: Attacker sends block body. `asynchronous_process_block` → `non_contextual_verify` passes → block is stored → `ConsumeUnverifiedBlockProcessor::verify_block` fails → `insert_block_status(block_hash, BLOCK_INVALID)`.

**Step 4**: Attacker re-sends the same header via `SendHeaders`. `HeaderAcceptor::accept()` is called again. `status = BLOCK_INVALID`. The `HEADER_VALID` guard does not fire (`BLOCK_INVALID` does not contain `HEADER_VALID`). All three checks pass again. `insert_valid_header` overwrites `BLOCK_INVALID` with `HEADER_VALID`. Node re-requests block body.

**Step 5**: Repeat from Step 3 indefinitely. Each cycle costs the node one full block download and one full block verification.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L295-322)
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
```

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

**File:** shared/src/shared.rs (L425-457)
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
    }

    pub fn contains_block_status<T: ChainStore>(
        &self,
        block_hash: &Byte32,
        status: BlockStatus,
    ) -> bool {
        self.get_block_status(block_hash).contains(status)
    }

    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
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
