Audit Report

## Title
Stale `header_map` Entries on Block Verification Failure — (`chain/src/verify.rs`, `chain/src/orphan_broker.rs`)

## Summary
In `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks`, the success path removes a block from both `block_status_map` and `header_map`, but the failure path only updates `block_status_map` and never calls `remove_header_view`. The same omission exists in `process_invalid_block` in `orphan_broker.rs`. This causes `header_map` entries to accumulate permanently for every block that passes header validation but fails contextual verification, while `clean_expired_orphans` in the same file correctly performs all three cleanup operations.

## Finding Description
**Root cause — `chain/src/verify.rs` L141–190:**

The success branch calls both `remove_block_status` and `remove_header_view`:
```rust
Ok(_) => {
    self.shared.remove_block_status(&block_hash);
    self.shared.remove_header_view(&block_hash);   // ← present
}
```
The failure branch calls `delete_unverified_block` and updates `block_status_map`, but never calls `remove_header_view`:
```rust
Err(err) => {
    self.delete_unverified_block(&block);
    // remove_header_view is absent
    if !is_internal_db_error(err) {
        self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
    } else {
        self.shared.remove_block_status(&block_hash);
    }
}
```

**Secondary instance — `chain/src/orphan_broker.rs` L88–105:**

`process_invalid_block` calls `delete_block` and `insert_block_status(BLOCK_INVALID)` but omits `remove_header_view`, while `clean_expired_orphans` (L134–156) in the same file correctly calls all three: `delete_block`, `remove_header_view`, and `remove_block_status`.

**State inconsistency consequence — `shared/src/shared.rs` L425–445:**

`get_block_status` checks `block_status_map` first, then falls through to `header_map`. As long as `BLOCK_INVALID` remains in `block_status_map`, the stale `header_map` entry is masked. However, `insert_peer_unknown_header_list` (`sync/src/types/mod.rs` L1181–1196) queries `header_map` **directly**, bypassing `block_status_map`. A stale entry for an invalid block can therefore cause the node to record a peer's best-known header as an invalid block's `HeaderIndexView`, skewing download scheduling.

**Confirmed asymmetry:** `grep` across the entire repo shows `remove_header_view` is called in only three places: `chain/src/verify.rs` (success path only), `chain/src/orphan_broker.rs` (`clean_expired_orphans` only), and `sync/src/relayer/mod.rs`. The failure paths in both `verify.rs` and `orphan_broker.rs` are confirmed omissions.

## Impact Explanation
Stale `HeaderIndexView` entries (containing hash, number, epoch, timestamp, parent hash, total difficulty, and skip-list pointers) accumulate in `header_map` without bound for every block that passes header validation but fails contextual verification. This constitutes a **suboptimal implementation of CKB state storage mechanism** (Medium, 2001–10000 points): the two in-memory maps (`header_map` and `block_status_map`) are left in an inconsistent state, and `insert_peer_unknown_header_list` can act on stale `header_map` data to incorrectly set peer best-known headers, degrading sync scheduling. The "latent status confusion" impact (re-processing a known-invalid block) is not concretely supported because no eviction mechanism for `block_status_map` was found in the codebase.

## Likelihood Explanation
Triggering the bug requires a block whose header passes non-contextual validation (valid PoW, valid structure) but whose full block fails contextual verification. Realistic triggers include: a miner producing a valid-PoW block with invalid transactions; a block relayer forwarding a block from a fork with different contextual rules; or a compact block reconstructed with mismatched transactions. Each such event leaves one permanent stale entry. The condition is reachable by any peer without special privileges.

## Recommendation
In the failure branch of `consume_unverified_blocks` (`chain/src/verify.rs`), add `remove_header_view` symmetrically with the success branch:
```rust
Err(err) => {
    self.delete_unverified_block(&block);
    self.shared.remove_header_view(&block_hash); // add this
    if !is_internal_db_error(err) {
        self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
    } else {
        self.shared.remove_block_status(&block_hash);
    }
}
```
Apply the same fix to `process_invalid_block` in `chain/src/orphan_broker.rs`: add `self.shared.remove_header_view(block_hash)` after `self.delete_block(&lonely_block)`, mirroring what `clean_expired_orphans` already does correctly.

## Proof of Concept
1. Connect a peer to a CKB node and send a sequence of headers that pass non-contextual validation (valid PoW, valid parent chain). Each header is inserted into `header_map` via `insert_valid_header`.
2. Send the corresponding full blocks. Each block passes `non_contextual_verify` and is stored to DB.
3. During `consume_unverified_blocks`, contextual verification fails (e.g., a transaction spends a non-existent cell).
4. The failure path executes: `delete_unverified_block` removes the block from DB, `insert_block_status(BLOCK_INVALID)` marks it invalid — `remove_header_view` is never called.
5. After step 4, assert that `shared.header_map().contains_key(&block_hash)` returns `true` while `shared.block_status_map().get(&block_hash)` returns `BLOCK_INVALID`. The two maps are inconsistent.
6. Repeat steps 1–4 with distinct valid-PoW headers; observe `header_map` growing monotonically with no corresponding cleanup.