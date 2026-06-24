All code references have been verified. The vulnerability is real and all cited code paths are confirmed.

**Verification summary:**

1. `compact_to_target(0x20ffffff)`: `exponent=32`, overflow check is `exponent > 32` (strict), so `overflow=false`, target = `0xffffff << 232` — confirmed by test at `util/types/src/utilities/tests/difficulty.rs:25-35` and implementation at `util/types/src/utilities/difficulty.rs:75`.

2. `EaglesongBlake2bPowEngine::verify` guard `block_target.is_zero() || overflow` is false for this value — confirmed at `pow/src/eaglesong_blake2b.rs:22-28`. ~99.9999% of nonces pass.

3. `HeaderVerifier::verify` calls only `PowVerifier`, `NumberVerifier`, `EpochVerifier`, `TimestampVerifier` — confirmed at `verification/src/header_verifier.rs:32-50`. `EpochVerifier` only checks `is_well_formed()` and `is_successor_of()`, not `compact_target` — confirmed at `verification/src/header_verifier.rs:133-148`.

4. `compact_target` vs epoch check exists only in `contextual_block_verifier.rs:500-507`, never reached in sync path.

5. `SyncShared::get_header_fields` checks `header_map` first, then store — confirmed at `sync/src/types/mod.rs:1230-1253`. This means chained fake headers within a single batch work: after the first fake header is inserted into `header_map`, the second fake header's parent lookup finds it there.

6. `HeaderAcceptor::accept` calls `sync_shared.insert_valid_header` unconditionally on success — confirmed at `sync/src/synchronizer/headers_process.rs:356`.

---

Audit Report

## Title
Missing `compact_target` Epoch Validation in `HeaderVerifier` Allows Zero-Work Header Flooding — (`verification/src/header_verifier.rs`)

## Summary

`HeaderVerifier::verify` in the sync path validates PoW only against the header's own `compact_target`, but never checks that `compact_target` matches the epoch's consensus-enforced target. An unprivileged peer can craft headers with `compact_target=0x20ffffff`, making PoW trivially satisfiable by essentially any nonce (~99.9999% success rate). These headers pass all checks in `HeaderAcceptor::accept` and are inserted into the node's `header_map` via `insert_valid_header`. Up to 2000 such headers can be sent per `SendHeaders` message at zero real PoW cost, enabling memory pressure, bandwidth waste, and peer-state poisoning.

## Finding Description

**Root cause — `compact_to_target(0x20ffffff)` is non-zero and non-overflow:**
In `util/types/src/utilities/difficulty.rs:75`, the overflow guard is `!mantissa.is_zero() && (exponent > 32)`. For `compact=0x20ffffff`, `exponent=0x20=32`, so `32 > 32` is false and `overflow=false`. The resulting target is `0xffffff << 232` — a 256-bit value with the top 24 bits all set. This is confirmed by the test at `util/types/src/utilities/tests/difficulty.rs:25-35`.

**`EaglesongBlake2bPowEngine::verify` accepts this target:**
The guard at `pow/src/eaglesong_blake2b.rs:22-28` checks `block_target.is_zero() || overflow`. Both are false for this value. The subsequent check `output > block_target` passes for ~99.9999% of nonces since the top 24 bits of the target are all `0xff`.

**`HeaderVerifier::verify` never validates `compact_target` against the epoch:**
At `verification/src/header_verifier.rs:32-50`, the verifier calls `PowVerifier`, `NumberVerifier`, `EpochVerifier`, and `TimestampVerifier`. The `EpochVerifier` at lines 133-148 only checks `is_well_formed()` and `is_successor_of()` — it does not check the `compact_target` value against the epoch's expected target.

**The `compact_target` vs. epoch check is only in the contextual block verifier:**
`contextual_block_verifier.rs:500-507` performs `self.epoch.compact_target() != actual_compact_target`, but this code path is never reached during header-only sync.

**`SyncShared::get_header_fields` looks in `header_map` first:**
At `sync/src/types/mod.rs:1230-1253`, `get_header_fields` checks `header_map` before the persistent store. This means chained fake headers within a single `SendHeaders` batch work: after the first fake header is inserted into `header_map` via `insert_valid_header`, the second fake header's parent lookup succeeds, and so on for all 2000 headers in the batch.

**The sync path inserts fake headers unconditionally:**
`HeaderAcceptor::accept` at `sync/src/synchronizer/headers_process.rs:356` calls `sync_shared.insert_valid_header(self.peer, self.header)` after `non_contextual_check` passes. `insert_valid_header` at `sync/src/types/mod.rs:1094-1141` stores the header in `header_map`, updates `may_set_best_known_header` for the peer, and calls `may_set_shared_best_header`.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can send `SendHeaders` P2P messages (up to `MAX_HEADERS_LEN = 2000` headers per message) containing headers with `compact_target=0x20ffffff` and nonce=0. Each header requires zero real PoW. Concrete effects:

1. **Memory pressure on `header_map`**: Fake headers fill the configurable memory limit, evicting legitimate headers and degrading sync performance.
2. **Bandwidth waste**: The node issues `GetBlocks` requests for fake headers it believes are valid.
3. **CPU overhead**: Processing 2000 fake headers per message at negligible attacker cost.
4. **Peer best-header poisoning**: `may_set_best_known_header` is updated with the fake header index, distorting the node's view of peer chain tips and potentially causing incorrect sync decisions.

The fake headers cannot enter the canonical chain (contextual block verification would reject them via `EpochError::TargetMismatch`), but the header-chain pollution and resource exhaustion are real and repeatable.

## Likelihood Explanation

Any unprivileged peer connected via the Sync protocol can exploit this. The `SendHeaders` message is a standard sync protocol message processed by `HeadersProcess::execute`. No special privileges, keys, or majority hashpower are required. The cost per fake header is essentially zero — nonce=0 passes PoW with ~99.9999% probability. The attack is repeatable indefinitely as long as the peer connection is maintained.

## Recommendation

Add a `compact_target` check to the sync-path `EpochVerifier` in `verification/src/header_verifier.rs`. The verifier currently receives only `EpochNumberWithFraction` from the parent; to validate `compact_target`, it needs access to the parent's `EpochExt`. The `SyncShared` already has `get_epoch_ext` available at `sync/src/types/mod.rs:1175-1178`. Alternatively, add a standalone `CompactTargetVerifier` that retrieves the epoch's expected `compact_target` from the store and compares it against `header.compact_target()`, mirroring the check already present in `contextual_block_verifier.rs:500-507`.

## Proof of Concept

1. Connect to a CKB node as a peer via the Sync protocol.
2. Obtain the current tip header (parent) and its epoch info.
3. Construct a `HeaderView` with:
   - `parent_hash` = tip hash
   - `number` = tip number + 1
   - `epoch` = valid successor of tip epoch (same epoch number, index + 1)
   - `compact_target` = `0x20ffffff`
   - `timestamp` = tip timestamp + 1 (within allowed window)
   - `nonce` = 0 (passes PoW with ~99.9999% probability)
4. Chain 2000 such headers, each referencing the previous fake header as parent.
5. Send a `SendHeaders` P2P message containing all 2000 headers.
6. Assert: all headers pass `HeaderAcceptor::accept` and appear in `header_map`.
7. Repeat continuously to maintain memory pressure and bandwidth waste on the target node.