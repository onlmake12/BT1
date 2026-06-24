All code references in the report have been verified against the actual repository. Here is the audit result:

---

Audit Report

## Title
`BLOCK_INVALID` Status Not Checked in `HeaderAcceptor::accept`, Allowing Invalid Headers to Corrupt Sync State — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept` checks `status.contains(BlockStatus::HEADER_VALID)` to skip re-processing known headers, but `BLOCK_INVALID` (`1 << 12`) and `HEADER_VALID` (`1`) are orthogonal bits, so a header already marked `BLOCK_INVALID` passes this guard. The developer explicitly acknowledged this gap with a `// FIXME` comment at line 301. A header with a valid PoW/timestamp/version but an invalid body will pass all three sub-checks (`prev_block_check`, `non_contextual_check`, `version_check`) and reach `insert_valid_header`, corrupting the peer's `best_known_header` and potentially the global `shared_best_header` to an invalid chain tip.

## Finding Description

**Root cause — bit orthogonality:**
`HEADER_VALID = 1` (bit 0), `BLOCK_INVALID = 1 << 12 = 4096` (bit 12). `(4096 & 1) != 0` is `false`, so `status.contains(BlockStatus::HEADER_VALID)` is `false` when `status == BLOCK_INVALID`. [1](#0-0) 

**The unguarded path in `accept()`:**
The `// FIXME` at line 301 is a developer-acknowledged gap. When `status == BLOCK_INVALID`, the early-return branch at line 304 is not taken. [2](#0-1) 

Execution then falls through to:
1. `prev_block_check` (lines 244–253) — checks the **parent's** status, not H itself. Passes if the parent is valid. [3](#0-2) 
2. `non_contextual_check` (lines 255–284) — runs `HeaderVerifier::verify`, which only checks PoW, timestamp, and epoch. A header that previously passed these checks will pass again. [4](#0-3) 
3. `version_check` (lines 286–293) — checks `version == 0`. Passes trivially. [5](#0-4) 
4. `insert_valid_header` is called at line 356. [6](#0-5) 

**How a header gets `BLOCK_INVALID` with valid header fields:**
In `chain/src/verify.rs`, when full block verification fails (invalid transactions, script failures, etc.), the block is marked `BLOCK_INVALID`. The header itself already passed PoW/timestamp/version checks before body verification, so `non_contextual_check` will pass again. [7](#0-6) 

**What `insert_valid_header` does:**
It inserts the header into `header_map` (line 1129), calls `may_set_best_known_header` (line 1132) to update the peer's best known header to the invalid chain tip, and calls `may_set_shared_best_header` (line 1140) to potentially update the global shared best header. [8](#0-7) 

**Why the bug is repeatable:**
`insert_valid_header` does NOT call `insert_block_status`, so `block_status_map` retains `BLOCK_INVALID`. `get_block_status` checks `block_status_map` first and returns `BLOCK_INVALID` on every subsequent call, meaning every `SendHeaders` message containing H re-triggers the full fallthrough and another `insert_valid_header` call. [9](#0-8) 

**Contrast with the correct guard in `CompactBlockProcess`:**
The relay path explicitly checks `BLOCK_INVALID` and returns early. [10](#0-9) 

## Impact Explanation

**High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

- `header_map` is polluted with invalid headers, consuming memory and potentially serving as parent anchors for further invalid header chains.
- The peer's `best_known_header` is updated to an invalid chain tip, causing the block fetcher to schedule downloads of blocks building on an invalid chain, wasting bandwidth.
- If the invalid chain has higher total difficulty, `shared_best_header` is updated to the invalid tip, disrupting sync decisions for all peers on the node.
- Because the bug is repeatable on every `SendHeaders` message (the `block_status_map` entry is never cleared), the attacker can sustain the attack indefinitely after the one-time PoW cost, causing continuous sync disruption and bandwidth waste across the network.
- The actual chain state (UTXO set, tip) is not corrupted; this is a sync-layer DoS, not a consensus violation.

## Likelihood Explanation

The attack requires only an unprivileged P2P connection. The one-time cost is mining a single block with a valid header (valid PoW, timestamp, version) but an invalid body (e.g., a transaction with a failing script). On mainnet, this PoW cost is significant but finite and amortized across unlimited repeated exploitation. On testnet or during IBD on a low-difficulty chain, the cost is negligible. After the initial block is marked `BLOCK_INVALID`, the attacker sends `SendHeaders` messages containing that header hash at essentially zero marginal cost, repeatedly corrupting the sync state of any reachable node.

## Recommendation

Add an explicit `BLOCK_INVALID` early-return guard at the top of `accept()`, immediately after the status check, resolving the `// FIXME`:

```rust
// In HeaderAcceptor::accept(), after line 303:
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent));
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) { ... }
```

This mirrors the existing guard in `CompactBlockProcess::contextual_check` at line 259. [10](#0-9) 

## Proof of Concept

1. Pre-insert `BLOCK_INVALID` status for header hash H in `block_status_map` (simulating a prior failed full-block verification, as done in `chain/src/verify.rs` line 177).
2. Construct a `HeaderView` H with: a valid parent (not `BLOCK_INVALID`, present in `header_map` or store), valid PoW/timestamp, and `version == 0`.
3. Call `HeaderAcceptor::new(H, peer, verifier, active_chain).accept()`.
4. Assert the returned `ValidationResult.state == ValidationState::Invalid` — **this assertion will FAIL** (the bug: it returns `Valid`).
5. Assert `header_map` does NOT contain H — **this assertion will FAIL** (the bug: H is inserted).
6. Assert `peers.get_best_known_header(peer)` does NOT equal H's index — **this assertion will FAIL**.

The analogous correct behavior is demonstrated by `test_in_block_status_map` in `sync/src/relayer/tests/compact_block_process.rs`, which confirms `BlockIsInvalid` is returned for the relay path. The equivalent test for `HeadersProcess` would fail due to the missing guard.

### Citations

**File:** shared/src/block_status.rs (L9-17)
```rust
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```

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

**File:** sync/src/synchronizer/headers_process.rs (L255-284)
```rust
    pub fn non_contextual_check(&self, state: &mut ValidationResult) -> Result<(), bool> {
        self.verifier.verify(self.header).map_err(|error| {
            debug!(
                "HeadersProcess accepted {:?} error {:?}",
                self.header.number(),
                error
            );
            // UnknownParentError surfaces as BlockError(UnknownParent), not
            // HeaderError.  Missing parent is a recoverable ordering/context
            // issue, not proof that this header is invalid.
            if error
                .downcast_ref::<BlockError>()
                .is_some_and(|e| e.kind() == BlockErrorKind::UnknownParent)
            {
                state.temporary_invalid(Some(ValidationError::Verify(error)));
                false
            } else if let Some(header_error) = error.downcast_ref::<HeaderError>() {
                if header_error.is_too_new() {
                    state.temporary_invalid(Some(ValidationError::Verify(error)));
                    false
                } else {
                    state.invalid(Some(ValidationError::Verify(error)));
                    true
                }
            } else {
                state.invalid(Some(ValidationError::Verify(error)));
                true
            }
        })
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L286-293)
```rust
    pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.header.version() != 0 {
            state.invalid(Some(ValidationError::Version));
            Err(())
        } else {
            Ok(())
        }
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

**File:** chain/src/verify.rs (L175-178)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
```

**File:** sync/src/types/mod.rs (L1129-1140)
```rust
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

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
