All cited code is confirmed in the repository. Here is the validation result:

---

Audit Report

## Title
`BLOCK_INVALID` Header Re-accepted as `HEADER_VALID` After Node Restart Due to Missing Early-Return Guard — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` creates a `ValidationResult` whose default state is `Valid` (confirmed at lines 361–364), then reads the block status but — as explicitly acknowledged by a `FIXME` comment at lines 301–302 — performs no early return when `status == BLOCK_INVALID`. A structurally valid header for a previously-rejected block passes all three remaining checks and causes `insert_valid_header` to write the header into `header_map`. Because `get_block_status` checks `header_map` before the DB (lines 425–444 of `shared/src/shared.rs`), subsequent status lookups return `HEADER_VALID` (bit 0), permanently shadowing the DB-persisted `BLOCK_INVALID` (bit 12, a disjoint flag) for the session.

## Finding Description
**Root cause — default equals success and missing guard:**
`ValidationState` uses `Valid` as `#[default]` (lines 362–364 of `headers_process.rs`). `ValidationResult::default()` therefore starts as `Valid` with no validation performed. The FIXME at lines 301–302 explicitly acknowledges the missing early-return for `BLOCK_INVALID`. Because no guard exists, execution falls through to `prev_block_check`, `non_contextual_check`, and `version_check`. A structurally valid header for a previously-rejected block passes all three. `insert_valid_header` is called at line 356, writing the header into `header_map` (line 1129 of `sync/src/types/mod.rs`) without touching `block_status_map` or the DB.

**Status shadowing:**
`get_block_status` (lines 425–444 of `shared/src/shared.rs`) resolves in order: `block_status_map` → `header_map` → DB. `HEADER_VALID = 1` and `BLOCK_INVALID = 1 << 12` are disjoint bitflags (confirmed in `shared/src/block_status.rs` lines 11 and 16). Once `header_map` contains the entry, `get_block_status` returns `HEADER_VALID`, which does not contain `BLOCK_INVALID`. The DB entry (`verified = Some(false)`) is never consulted again for the session.

**Downstream bypass:**
- `contextual_check` in `compact_block_process.rs` (line 259) rejects only if `status.contains(BlockStatus::BLOCK_INVALID)`. After re-insertion, status is `HEADER_VALID`, so this guard is bypassed and the compact block is processed.
- `asynchronous_process_remote_block` (line 477 of `synchronizer/mod.rs`) calls `accept_remote_block` when `status.contains(BlockStatus::HEADER_VALID)`, re-queuing the block for chain processing.
- `insert_valid_header` also calls `may_set_shared_best_header` (line 1140), potentially updating the node's `shared_best_header` to a chain containing a known-invalid block.

## Impact Explanation
An unprivileged peer can repeatedly trigger re-processing of known-invalid blocks after any node restart, with negligible attacker cost (a single `SendHeaders` P2P message per block). Each trigger causes the node to re-request and re-process the full block or compact block, wasting CPU, memory, and bandwidth. At scale — targeting multiple nodes simultaneously with multiple known-invalid block headers — this constitutes a low-cost mechanism for CKB network congestion. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
Medium-High. The precondition (a block in the DB with `verified = Some(false)`) arises naturally during normal operation whenever a block fails full validation after being stored. Node restarts are routine. Any peer that observed the original rejected block can replay its header. The `FIXME` comment in production code confirms the developers are aware the path is reachable. The attack is repeatable after every restart with no additional setup.

## Recommendation
1. Add an `Uninitialized` sentinel as `#[default]` to `ValidationState` so a freshly created `ValidationResult` is never silently treated as success.
2. Fix the FIXME in `HeaderAcceptor::accept()` by adding an early return for `BLOCK_INVALID` immediately after the status lookup:
```rust
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}
```
3. Assert in callers that a returned `ValidationResult` is never `Uninitialized`.

## Proof of Concept
1. Run a CKB node and let it process a block that fails full validation (e.g., invalid script). The block is stored in the DB with `block_ext.verified = Some(false)`.
2. Restart the node. Both `block_status_map` and `header_map` are cleared.
3. Connect as an unprivileged peer and send a `SendHeaders` P2P message containing the header of the rejected block.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()`. `get_block_status` returns `BLOCK_INVALID` (from DB). The FIXME path is taken: no early return. `prev_block_check`, `non_contextual_check`, and `version_check` all pass for a structurally valid header. `insert_valid_header` is called.
5. Call `get_block_status` for the block hash. Observe it now returns `HEADER_VALID` instead of `BLOCK_INVALID` — the DB-backed status is shadowed.
6. Send a compact block for the same block. `contextual_check` no longer rejects it (the `BLOCK_INVALID` guard at line 259 of `compact_block_process.rs` is bypassed). The node re-processes the block, wasting resources. Repeat after each restart.