Audit Report

## Title
`BLOCK_INVALID` Status Not Guarded in `HeaderAcceptor::accept()`, Corrupting Sync State — (`File: sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains a developer-acknowledged FIXME: it checks only for `HEADER_VALID` status but never for `BLOCK_INVALID`. Because `BLOCK_INVALID = 1 << 12` and `HEADER_VALID = 1` share no bits, a header whose block was previously rejected by the chain verifier bypasses the early-return guard, passes all three sub-checks (whose scope is header-only), and reaches `insert_valid_header`. This inserts the header into `header_map`, sets the peer's `best_known_header`, and can overwrite the global `shared_best_header` with a block the node already knows is permanently invalid, corrupting IBD gating and block-fetch decisions.

## Finding Description
In `sync/src/synchronizer/headers_process.rs` lines 301–322, `accept()` reads the block status and immediately checks only `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
```

`get_block_status` in `shared/src/shared.rs` (lines 425–444) checks `block_status_map` first; when the chain verifier has called `insert_block_status(hash, BLOCK_INVALID)`, it returns `BLOCK_INVALID` directly. Since `BLOCK_INVALID (1 << 12)` and `HEADER_VALID (1)` share no bits (`shared/src/block_status.rs` lines 11–16), `status.contains(HEADER_VALID)` is `false` and the early-return is never taken.

The function then runs:
- `prev_block_check` — checks the *parent's* status, not the block itself; passes for any block whose parent is valid.
- `non_contextual_check` — runs `HeaderVerifier::verify()`, which is header-only (PoW, timestamp, epoch); passes for a block with a valid header but invalid body.
- `version_check` — passes for any version-0 header.

All three pass, so `sync_shared.insert_valid_header(self.peer, self.header)` is called at line 356. `insert_valid_header` (`sync/src/types/mod.rs` lines 1094–1141) inserts the header into `header_map`, sets the peer's `best_known_header`, and calls `may_set_shared_best_header`, which unconditionally overwrites `shared_best_header` if the invalid block's total difficulty is higher (lines 1398–1408).

The `block_status_map` entry for `BLOCK_INVALID` is not cleared, so subsequent `get_block_status` calls still return `BLOCK_INVALID`, but the damage to `header_map`, peer state, and `shared_best_header` is already done.

The contrast with `compact_block_process.rs` (lines 259–261) is direct: that path explicitly returns `BlockIsInvalid` when `status.contains(BlockStatus::BLOCK_INVALID)`, while `headers_process.rs` has only the FIXME comment.

## Impact Explanation
`shared_best_header` is the authoritative source for `min_chain_work_ready()` (`sync/src/types/mod.rs` lines 1348–1352), which gates IBD block download. If an attacker-controlled invalid block carries artificially high total difficulty, the node prematurely believes it has reached minimum chain work. `BlockFetchCMD` reads `shared_best_header_ref()` to make IBD gating decisions (`sync/src/synchronizer/mod.rs` lines 124–164), and `BlockFetcher::fetch()` uses the peer's `best_known_header` to issue `GetBlocks` requests (`sync/src/synchronizer/block_fetcher.rs` lines 159–183), causing the node to request blocks on a chain it already knows is invalid.

This constitutes suboptimal/incorrect implementation of the CKB sync state storage mechanism: the node's authoritative sync pointer (`shared_best_header`) and per-peer tracking are corrupted by a block the node has already permanently rejected, disrupting IBD gating and block-fetch decisions.

**Allowed impact: Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism.**

## Likelihood Explanation
**Low.** The precondition requires a miner to produce a block with a valid PoW header but an invalid body (e.g., a script that always fails). This has a real cost proportional to the network's mining difficulty. However, once such a block exists on the network, any unprivileged P2P peer can replay its header via a standard `SendHeaders` message at negligible cost, and the attack is repeatable. No privileged access is required beyond the initial block creation.

## Recommendation
Add an explicit `BLOCK_INVALID` guard at the top of `accept()`, directly resolving the FIXME, mirroring the pattern already used in `compact_block_process.rs`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing early-return path
}
```

## Proof of Concept
1. Attacker mines block **B** with valid PoW but an invalid body (e.g., always-failing script). The victim node receives **B** via compact block relay, passes header verification, stores it, runs full body verification, fails, and the chain verifier calls `insert_block_status(B.hash, BLOCK_INVALID)` (`chain/src/verify.rs` lines 175–177).
2. Attacker (or any peer) sends a `SendHeaders` P2P message containing **B**'s header to the victim node.
3. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for **B**'s header.
4. `get_block_status` returns `BLOCK_INVALID`. The guard `status.contains(HEADER_VALID)` is `false`. No early return.
5. `prev_block_check` passes (B's parent is valid). `non_contextual_check` passes (B's header has valid PoW). `version_check` passes.
6. `insert_valid_header` is called. `shared_best_header` is updated to **B** if **B**'s total difficulty exceeds the current best.
7. `min_chain_work_ready()` may now return `true` prematurely; `BlockFetcher` issues `GetBlocks` for blocks on **B**'s invalid chain.