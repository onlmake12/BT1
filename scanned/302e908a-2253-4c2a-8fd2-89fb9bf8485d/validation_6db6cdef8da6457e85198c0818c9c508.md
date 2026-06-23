### Title
`BLOCK_INVALID` Status Can Be Overwritten to `HEADER_VALID` via Peer-Sent `SendHeaders` — (`File: sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the header sync pipeline does not guard against a block already marked `BLOCK_INVALID`. A malicious peer can send a `SendHeaders` P2P message containing a previously-rejected block header, causing the node to overwrite the terminal `BLOCK_INVALID` status with `HEADER_VALID` and re-enter the block into the sync pipeline. The code itself contains a `FIXME` comment acknowledging this gap.

---

### Finding Description

`BlockStatus` is defined as a bitflag set in `shared/src/block_status.rs`:

```
UNKNOWN        = 0
HEADER_VALID   = 1
BLOCK_RECEIVED = 1 | (HEADER_VALID << 1)
BLOCK_STORED   = 1 | (BLOCK_RECEIVED << 1)
BLOCK_VALID    = 1 | (BLOCK_STORED << 1)
BLOCK_INVALID  = 1 << 12
```

`BLOCK_INVALID` is a disjoint terminal state — it shares no bits with the valid-chain states. [1](#0-0) 

`insert_block_status` performs an unconditional map insert with no guard against overwriting an existing `BLOCK_INVALID` entry:

```rust
pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
    self.block_status_map.insert(block_hash, status);
}
``` [2](#0-1) 

In `HeaderAcceptor::accept()`, the early-return guard only checks for `HEADER_VALID`, not for `BLOCK_INVALID`. The developer left an explicit `FIXME` comment acknowledging the missing guard:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best-known and return
    return result;
}
``` [3](#0-2) 

Because `BLOCK_INVALID` (`1 << 12`) does not contain the `HEADER_VALID` bit (`1`), the `status.contains(BlockStatus::HEADER_VALID)` check evaluates to `false` for an invalid block. Execution falls through to `prev_block_check`, `non_contextual_check`, and `version_check`. If the header passes those checks (e.g., the block was rejected for a contextual reason such as wrong epoch or invalid parent, but the header itself is structurally sound), `insert_valid_header` is called at line 356, which writes `HEADER_VALID` into `block_status_map`, overwriting the terminal `BLOCK_INVALID` state. [4](#0-3) 

`BLOCK_INVALID` is set as a terminal state by multiple subsystems:
- `chain/src/chain_service.rs` — non-contextual verification failure [5](#0-4) 
- `chain/src/verify.rs` — contextual verification failure [6](#0-5) 
- `sync/src/relayer/compact_block_process.rs` — invalid compact block header [7](#0-6) 
- `chain/src/orphan_broker.rs` — descendant of an invalid block [8](#0-7) 

All of these expect `BLOCK_INVALID` to be permanent and irreversible. The header sync path violates this invariant.

---

### Impact Explanation

A malicious peer can repeatedly send `SendHeaders` messages containing a block header that was previously rejected and marked `BLOCK_INVALID`. Each time, the node:
1. Overwrites `BLOCK_INVALID` with `HEADER_VALID` in `block_status_map`.
2. Updates the peer's best-known header, causing the synchronizer to schedule download of the block.
3. Re-submits the block through `asynchronous_process_block`, which re-runs non-contextual verification, re-marks it `BLOCK_INVALID`, and the cycle repeats.

This creates a resource-exhaustion loop: repeated CPU cycles for verification, repeated DB writes to `block_status_map`, and repeated network requests for a block the node has already definitively rejected. The `BLOCK_INVALID` terminal state — which is the node's primary mechanism for permanently blacklisting invalid blocks and their descendants — is rendered non-terminal.

---

### Likelihood Explanation

The attack requires only a standard P2P `SendHeaders` message, which any connected peer can send without authentication. No special privilege, key, or majority hashpower is needed. The attacker only needs to know the hash of a block the target node has previously rejected (observable from public chain data or by probing). The FIXME comment confirms the developers are aware of the gap but have not resolved it.

---

### Recommendation

In `HeaderAcceptor::accept()`, add an explicit early-return guard for `BLOCK_INVALID` before any other check:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status == BlockStatus::BLOCK_INVALID {
    // Terminal state: do not allow re-validation via header sync
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ...existing logic...
}
```

Additionally, `insert_block_status` should guard against downgrading a `BLOCK_INVALID` entry to any non-invalid status:

```rust
pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
    self.block_status_map.entry(block_hash).and_modify(|existing| {
        if *existing != BlockStatus::BLOCK_INVALID {
            *existing = status;
        }
    }).or_insert(status);
}
```

---

### Proof of Concept

1. Connect to a CKB node as a peer.
2. Send a `SendBlock` message containing a block with a structurally valid header but an invalid contextual property (e.g., wrong epoch, invalid parent hash pointing to a known block). The node marks the block `BLOCK_INVALID` via `compact_block_process.rs` or `chain_service.rs`.
3. Immediately send a `SendHeaders` message containing the same block header.
4. `HeaderAcceptor::accept()` is invoked. The `BLOCK_INVALID` status does not contain `HEADER_VALID`, so the early-return guard is skipped (line 304). The header passes `non_contextual_check` (since the header bytes are structurally valid). `insert_valid_header` is called, overwriting `BLOCK_INVALID` with `HEADER_VALID` in `block_status_map`.
5. The synchronizer schedules re-download of the block. The block is re-submitted, fails verification again, and is re-marked `BLOCK_INVALID`.
6. Repeat step 3 indefinitely to sustain the resource-exhaustion loop. [9](#0-8) [2](#0-1) [1](#0-0)

### Citations

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

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
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

**File:** chain/src/chain_service.rs (L121-130)
```rust
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
```

**File:** chain/src/verify.rs (L175-181)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```

**File:** sync/src/relayer/compact_block_process.rs (L325-338)
```rust
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

**File:** chain/src/orphan_broker.rs (L88-104)
```rust
    fn process_invalid_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();

        self.delete_block(&lonely_block);

        self.shared
            .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);

        let err: VerifyResult = Err(InternalErrorKind::Other
            .other(format!(
                "parent {} is invalid, so block {}-{} is invalid too",
                parent_hash, block_number, block_hash
            ))
            .into());
        lonely_block.execute_callback(err);
```
