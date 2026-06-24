Audit Report

## Title
`HeaderAcceptor::accept()` Bypasses `BLOCK_INVALID` Guard, Inserting Known-Invalid Headers as Valid - (File: `sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains an acknowledged `FIXME` where a header whose block was previously marked `BLOCK_INVALID` is not rejected early. Because `BLOCK_INVALID` (`1 << 12 = 4096`) does not contain the `HEADER_VALID` (`1`) bit, the early-return guard is bypassed. The function then re-runs only lightweight non-contextual checks, which pass for blocks invalidated for contextual reasons, and proceeds to call `insert_valid_header`, inserting the invalid block's header into the `header_map` and corrupting the peer's `best_known_header` and potentially the global `shared_best_header`.

## Finding Description
In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` at line 301 contains an explicit `FIXME` comment acknowledging the missing guard:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... }
```

`BlockStatus::BLOCK_INVALID = 1 << 12 = 4096`. `BlockStatus::HEADER_VALID = 1`. The bitwise check `4096 & 1 == 0` means `status.contains(HEADER_VALID)` is `false` for `BLOCK_INVALID` blocks, so the early-return is skipped entirely.

The code then falls through to three checks:
1. `prev_block_check` — only checks whether the *parent* is `BLOCK_INVALID`
2. `non_contextual_check` — runs `HeaderVerifier` (PoW nonce, timestamp, epoch, version)
3. `version_check` — checks `header.version() == 0`

A block marked `BLOCK_INVALID` due to contextual body failures (invalid transactions, wrong cellbase reward, invalid DAO header, etc.) has a valid header that passes all three checks. The function then reaches line 356:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
```

`insert_valid_header` (lines 1094–1141 of `sync/src/types/mod.rs`) inserts the header into `header_map`, calls `may_set_best_known_header` to update the peer's chain tip, and calls `may_set_shared_best_header` to potentially update the global best header — all for a block the node already knows is invalid.

By contrast, `CompactBlockProcess` at line 259 of `sync/src/relayer/compact_block_process.rs` correctly guards:
```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
```

The `SendHeaders` path has no equivalent guard.

## Impact Explanation
The concrete impact is **CKB network congestion with attacker-controlled cost**. Once `best_known_header` is set to a known-invalid block, `BlockFetcher` (lines 159–169 of `sync/src/synchronizer/block_fetcher.rs`) uses it to determine which blocks to download. The fetcher loop (lines 247–280) does not check `BLOCK_INVALID` status when iterating candidates, so it issues `GetBlocks` requests for blocks on a chain the node already knows is invalid, wasting bandwidth and CPU. If `shared_best_header` is also corrupted (when the invalid block has higher total difficulty), the node's IBD state machine and sync scheduling are disrupted, potentially stalling legitimate sync. This maps to the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
The attacker must first cause a block to be marked `BLOCK_INVALID` for contextual reasons — this requires mining a block with valid PoW but an invalid body (e.g., wrong cellbase reward). On mainnet this is expensive (requires real hash power), making the precondition non-trivial. However, the cost is one-time: once such a block exists, the attacker can repeatedly send its header via `SendHeaders` to any number of nodes at negligible cost, amplifying the impact. The FIXME comment in the source code confirms the developers are aware of the gap. The entry path (`SendHeaders` P2P message → `HeadersProcess::execute()` → `HeaderAcceptor::accept()`) requires no privilege.

## Recommendation
Add an explicit `BLOCK_INVALID` guard at the top of `HeaderAcceptor::accept()`, before the `HEADER_VALID` check, mirroring what `CompactBlockProcess` already does:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // existing path
}
```

## Proof of Concept
1. Connect a malicious peer to a CKB node.
2. Mine a block whose header is valid (correct PoW, timestamp, epoch) but whose body is contextually invalid (e.g., cellbase output exceeds the allowed reward). Relay it to the target node. The node processes it, fails contextual verification in `chain/src/verify.rs`, and sets `block_status_map[block_hash] = BLOCK_INVALID`.
3. Send a `SendHeaders` P2P message containing that same block's header.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()`.
5. `get_block_status` returns `BLOCK_INVALID` (= `4096`). `status.contains(HEADER_VALID)` = `(4096 & 1) != 0` = `false`. The early-return is skipped.
6. `prev_block_check` passes (the parent is valid). `non_contextual_check` passes (the header's PoW/timestamp/epoch are valid). `version_check` passes.
7. `insert_valid_header` is called: the invalid block's header is inserted into `header_map`, and `best_known_header` for the peer is updated to the invalid block.
8. Observe via `get_peers` RPC that `best_known_header_hash` now points to the known-invalid block. `BlockFetcher` subsequently issues `GetBlocks` requests for blocks on the invalid chain.