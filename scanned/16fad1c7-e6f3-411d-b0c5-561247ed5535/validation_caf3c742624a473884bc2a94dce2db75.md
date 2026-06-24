Audit Report

## Title
Missing `BLOCK_INVALID` Guard in `HeadersProcess::accept` Enables Repeated Re-processing of Invalid Blocks — (File: `sync/src/synchronizer/headers_process.rs`)

## Summary
`HeadersProcess::accept` checks only for `HEADER_VALID` when short-circuiting on already-seen headers, leaving no guard against `BLOCK_INVALID`. A malicious peer can repeatedly re-send a header for a block that was already fully verified and rejected, causing `insert_valid_header` to overwrite the `BLOCK_INVALID` status with `HEADER_VALID` and re-enter the download-and-verify cycle indefinitely. A secondary bug in `consume_unverified_blocks` leaves a stale `header_map` entry on verification failure, compounding the issue.

## Finding Description

**Root Cause 1 — Missing guard in `HeadersProcess::accept`**

The FIXME comment at lines 301–302 is a developer acknowledgment that the guard is absent:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    ...
    return result;
}
``` [1](#0-0) 

`BLOCK_INVALID = 1 << 12` and `HEADER_VALID = 1`. The bitflag `contains` check evaluates to `false` for `BLOCK_INVALID`, so the early return is never triggered for an already-rejected block. [2](#0-1) 

Execution falls through all header checks (`prev_block_check`, `non_contextual_check`, `version_check`) and reaches:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [3](#0-2) 

`insert_valid_header` inserts into `header_map` and updates `block_status_map` with `HEADER_VALID`. [4](#0-3) 

`insert_block_status` unconditionally overwrites any existing entry:

```rust
pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
    self.block_status_map.insert(block_hash, status);
}
``` [5](#0-4) 

This overwrites `BLOCK_INVALID` with `HEADER_VALID`, resetting the block into the sync pipeline.

**Root Cause 2 — Partial cleanup on verification failure**

In `consume_unverified_blocks`, the success branch calls both `remove_block_status` and `remove_header_view`, but the failure branch only writes `BLOCK_INVALID` and never calls `remove_header_view`: [6](#0-5) 

`get_block_status` checks `block_status_map` first; if not found, it falls back to `header_map` and returns `HEADER_VALID`. Once root cause 1 overwrites `BLOCK_INVALID` in `block_status_map`, the stale `header_map` entry ensures `HEADER_VALID` is returned on subsequent lookups. [7](#0-6) 

**Exploit flow:**
1. Attacker sends header H (syntactically valid header, semantically invalid body).
2. Node processes header → `insert_valid_header` → `HEADER_VALID` in `block_status_map`, entry in `header_map`.
3. Node downloads block B, runs CKB-VM full verification → fails → `BLOCK_INVALID` written to `block_status_map`; `header_map` entry is **not** removed.
4. Attacker re-sends header H.
5. `accept` reads `BLOCK_INVALID`; `contains(HEADER_VALID)` is false → no early return.
6. `insert_valid_header` overwrites `BLOCK_INVALID` with `HEADER_VALID`.
7. Block fetcher sees `HEADER_VALID` → re-requests block → node re-verifies → fails again.
8. Steps 4–7 repeat indefinitely.

## Impact Explanation
A single unprivileged peer can force a node into an unbounded loop of CKB-VM re-execution, repeated block downloads, and repeated DB writes/deletes. This exhausts CPU, bandwidth, and disk I/O, effectively stalling or crashing the targeted node. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attack requires only a standard P2P connection. Any peer that can relay headers and blocks can trigger it. No cryptographic material, majority hashpower, or operator access is needed. The FIXME comment confirms developer awareness of the missing guard. The loop is self-sustaining with no rate-limiting mechanism preventing re-submission of the same header.

## Recommendation

1. **Add the missing guard** in `HeadersProcess::accept` immediately after reading the block status:
   ```rust
   if status.contains(BlockStatus::BLOCK_INVALID) {
       return ValidationResult::with_error(ValidationError::InvalidBlock);
   }
   ``` [8](#0-7) 

2. **Call `remove_header_view`** in the failure branch of `consume_unverified_blocks`, mirroring the success branch:
   ```rust
   self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
   self.shared.remove_header_view(&block_hash); // add this
   ``` [9](#0-8) 

## Proof of Concept

```
Attacker peer                          CKB Node
     |                                     |
     |--- SendHeaders(H) ---------------->|  // H: valid header, invalid body
     |                                     |  // accept(): insert_valid_header → HEADER_VALID
     |<-- GetBlocks(H) -------------------|  // block fetcher requests block
     |--- SendBlock(B) ------------------>|  // CKB-VM runs, fails
     |                                     |  // BLOCK_INVALID set; header_map NOT cleared
     |--- SendHeaders(H) [repeat] ------->|  // FIXME guard absent → insert_valid_header
     |                                     |  // BLOCK_INVALID overwritten with HEADER_VALID
     |<-- GetBlocks(H) [repeat] ---------|  // node re-requests block
     |--- SendBlock(B) [repeat] -------->|  // node re-verifies, fails again
     |         ... loop forever ...       |
```

A unit test can confirm this by: (1) calling `accept` for a header, (2) writing `BLOCK_INVALID` to `block_status_map` for that header's hash, (3) calling `accept` again for the same header, and (4) asserting that `get_block_status` returns `HEADER_VALID` — demonstrating the overwrite.

### Citations

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

**File:** sync/src/types/mod.rs (L1089-1094)
```rust
    /// Sync a new valid header, try insert to sync state
    // Update the header_map
    // Update the block_status_map
    // Update the shared_best_header if need
    // Update the peer's best_known_header
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
```

**File:** shared/src/shared.rs (L425-445)
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
```

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
    }
```

**File:** chain/src/verify.rs (L140-181)
```rust
        match &verify_result {
            Ok(_) => {
                let log_now = std::time::Instant::now();
                self.shared.remove_block_status(&block_hash);
                let log_elapsed_remove_block_status = log_now.elapsed();
                self.shared.remove_header_view(&block_hash);
                debug!(
                    "block {} remove_block_status cost: {:?}, and header_view cost: {:?}",
                    block_hash,
                    log_elapsed_remove_block_status,
                    log_now.elapsed()
                );
            }
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
