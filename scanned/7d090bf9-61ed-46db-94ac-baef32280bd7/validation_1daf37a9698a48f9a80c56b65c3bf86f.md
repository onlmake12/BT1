### Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept()` Causes Inconsistent Sync Validation Behavior - (`File: sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the synchronizer's header-processing pipeline omits an early-return guard for the `BLOCK_INVALID` block status. The code contains an explicit `FIXME` comment acknowledging this gap. As a result, a header whose block was previously determined to be invalid can be re-accepted as `HEADER_VALID` through the `SendHeaders` P2P message path, while the same block is correctly rejected through the `CompactBlock` path. This is a direct analog to the Cairo ERC20Handler bug: one code path uses a hard-stop (assert/revert) or, in this case, silently skips a required guard, while a parallel code path handles the same condition correctly.

---

### Finding Description

In `HeaderAcceptor::accept()`, the function reads the current block status and branches on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best known header and return
    return result;
}
// Falls through to prev_block_check, non_contextual_check, version_check
// and ultimately insert_valid_header if all pass
```

`BLOCK_INVALID` is defined as `1 << 12`, while `HEADER_VALID` is `1`. They are completely disjoint bit flags:

```rust
const HEADER_VALID  =     1;
const BLOCK_INVALID =     1 << 12;
```

A header with `BLOCK_INVALID` status does **not** satisfy `status.contains(BlockStatus::HEADER_VALID)`, so execution falls through to the three subsequent checks:

1. `prev_block_check` — only checks whether the **parent** is `BLOCK_INVALID`, not the header itself.
2. `non_contextual_check` — runs `HeaderVerifier`. A header that was marked invalid due to block-body failure (e.g., script execution) will still pass header-level structural checks.
3. `version_check` — checks version field only.

If all three pass, `sync_shared.insert_valid_header(self.peer, self.header)` is called, inserting the previously-invalid header into the `header_map` and updating the peer's and shared best-known header.

In contrast, `CompactBlockProcess::execute()` correctly rejects `BLOCK_INVALID` blocks before any further processing, as confirmed by the test:

```rust
// BLOCK_INVALID in block_status_map
relayer.shared.shared()
    .insert_block_status(block.header().hash(), BlockStatus::BLOCK_INVALID);
assert_eq!(
    rt.block_on(compact_block_process.execute()),
    StatusCode::BlockIsInvalid.into(),
);
```

---

### Impact Explanation

When a block fails full contextual verification (e.g., script execution in `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks`), its hash is written to `block_status_map` as `BLOCK_INVALID`. However, a remote peer can immediately re-send the same header via a `SendHeaders` message. Because `HeaderAcceptor::accept()` skips the `BLOCK_INVALID` guard, the header passes all three lightweight checks and is re-inserted into the `header_map` as `HEADER_VALID` via `insert_valid_header`. This:

1. **Corrupts sync state**: The peer's best-known header and the shared best header can be advanced to point to a block that the local node has already determined is invalid.
2. **Triggers redundant block downloads**: The synchronizer may re-request the full block body for a hash it already rejected, wasting bandwidth and CPU.
3. **Inconsistent validation across message types**: The same block is rejected via `CompactBlock` but accepted via `SendHeaders`, creating an exploitable asymmetry.
4. **Potential fork-choice interference**: If the invalid chain has higher total difficulty, updating `shared_best_header` to it can mislead the synchronizer's chain-selection logic.

---

### Likelihood Explanation

The attack requires only an unprivileged P2P peer. Any connected peer can:
1. Craft or relay a block with a valid header but invalid body (e.g., a script that fails).
2. Wait for the node to mark it `BLOCK_INVALID` after full verification.
3. Re-send the header via `SendHeaders`.

No special privileges, keys, or majority hashpower are required. The `SendHeaders` message is a standard sync protocol message accepted from all peers.

---

### Recommendation

Add an explicit early-return for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, resolving the acknowledged FIXME:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

// Early return for already-invalid blocks
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
}
```

This mirrors the behavior already implemented in `CompactBlockProcess`.

---

### Proof of Concept

**Root cause — missing guard (acknowledged by FIXME):** [1](#0-0) 

**`BLOCK_INVALID` is a disjoint bit from `HEADER_VALID` — the `contains` check never fires for invalid blocks:** [2](#0-1) 

**`prev_block_check` only checks the parent, not the header itself:** [3](#0-2) 

**If all lightweight checks pass, `insert_valid_header` overwrites the invalid state:** [4](#0-3) 

**`insert_valid_header` inserts into `header_map` and updates best-known headers:** [5](#0-4) 

**`BLOCK_INVALID` is correctly set after full block verification failure:** [6](#0-5) 

**Contrast: `CompactBlockProcess` correctly rejects `BLOCK_INVALID` (test confirms `StatusCode::BlockIsInvalid`):** [7](#0-6)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L244-253)
```rust
    pub fn prev_block_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.active_chain.contains_block_status(
            &self.header.data().raw().parent_hash(),
            BlockStatus::BLOCK_INVALID,
        ) {
            state.invalid(Some(ValidationError::InvalidParent));
            return Err(());
        }
        Ok(())
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

**File:** sync/src/synchronizer/headers_process.rs (L356-357)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
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

**File:** sync/src/types/mod.rs (L1094-1141)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
    }
```

**File:** chain/src/verify.rs (L175-178)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
```

**File:** sync/src/relayer/tests/compact_block_process.rs (L60-71)
```rust
    // BLOCK_INVALID in block_status_map
    {
        relayer
            .shared
            .shared()
            .insert_block_status(block.header().hash(), BlockStatus::BLOCK_INVALID);
    }

    assert_eq!(
        rt.block_on(compact_block_process.execute()),
        StatusCode::BlockIsInvalid.into(),
    );
```
