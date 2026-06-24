Audit Report

## Title
Unbounded `block_status_map` Growth via Unremoved `BLOCK_INVALID` Entries — (`shared/src/shared.rs`)

## Summary

The `block_status_map` (`Arc<DashMap<Byte32, BlockStatus>>`) in `shared/src/shared.rs` has no capacity bound and no eviction policy. Every invalid block header received from a remote peer causes a permanent `BLOCK_INVALID` insertion via `insert_block_status()`. Because `remove_block_status()` is never called for `BLOCK_INVALID` entries on the header-validation path, and because no peer-banning occurs in `HeadersProcess`, an unprivileged remote peer can drive unbounded heap growth and crash the node via OOM.

## Finding Description

The map is declared with no capacity limit: [1](#0-0) 

`insert_block_status()` performs an unconditional insert with no size guard: [2](#0-1) 

`remove_block_status()` is only called on the **success path** in `chain/src/verify.rs` (line 143) and on the internal-DB-error path (line 180). For every block that fails contextual verification, `BLOCK_INVALID` is inserted and the entry is never removed: [3](#0-2) 

In `sync/src/synchronizer/headers_process.rs`, three separate failure branches each call `insert_block_status(..., BLOCK_INVALID)` with no corresponding removal: [4](#0-3) 

In `chain/src/chain_service.rs`, the non-contextual verification failure path also inserts `BLOCK_INVALID` permanently: [5](#0-4) 

Critically, a search for `ban_peer` in `headers_process.rs` returns no matches. When `HeadersProcess::execute()` encounters an invalid header, it returns `StatusCode::HeadersIsInvalid` but does **not** ban the peer: [6](#0-5) 

This means the same peer (or a reconnecting peer) can continue submitting unique invalid headers indefinitely, each one permanently occupying a slot in `block_status_map`.

## Impact Explanation

This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

`block_status_map` resides entirely in process heap memory. Each entry is a `Byte32` key (32 bytes) plus `BlockStatus` metadata plus `DashMap` internal overhead (~100–200 bytes per entry). Sending 10 million unique invalid header hashes consumes roughly 1–2 GB of heap. At sufficient scale the OS OOM killer terminates the node process, constituting a complete node crash. The attack requires no cryptographic capability and no special privilege — only the ability to open a sync protocol connection.

## Likelihood Explanation

Any unprivileged remote peer acting as a header relayer can trigger this. The attacker constructs `SendHeaders` messages containing headers with unique hashes that fail `prev_block_check`, `non_contextual_check`, or `version_check` (e.g., wrong version field, invalid PoW, or invalid parent hash). Each unique hash that is not already in the map triggers a permanent `BLOCK_INVALID` insertion. Because no banning occurs in `HeadersProcess`, the attacker is not disconnected-and-banned after a single invalid submission; even if the connection is dropped, reconnection is trivial. IP rotation on cloud infrastructure makes address-based banning ineffective. The attack is slow but requires no special resources and accumulates entries across sessions.

## Recommendation

1. **Cap the map size**: Enforce a maximum entry count (e.g., 1 million entries). When the cap is reached, evict the oldest or least-recently-used `BLOCK_INVALID` entries before inserting new ones.
2. **Periodic TTL-based cleanup**: Add a background task that removes `BLOCK_INVALID` entries older than a configurable TTL (e.g., 24 hours), since re-checking a known-invalid block against the database is cheap.
3. **Peer banning on invalid headers**: In `HeadersProcess::execute()`, call `ban_peer` when a header fails validation with `ValidationState::Invalid`, preventing the same peer from repeatedly injecting entries.
4. **Per-peer rate limiting**: Enforce a per-peer limit on the number of invalid blocks/headers accepted per time window before banning.

## Proof of Concept

1. Establish a sync protocol connection to a CKB node.
2. In a loop, construct `SendHeaders` messages each containing a single header with a unique hash that fails `non_contextual_check` (e.g., set an invalid version field or an invalid PoW value, varying the nonce to produce a unique hash each iteration).
3. Send each message. After each batch, observe `/proc/<pid>/status` (`VmRSS`) on the node host — resident memory grows monotonically.
4. Confirm via logging that `insert_block_status(..., BLOCK_INVALID)` is called for each header at `sync/src/synchronizer/headers_process.rs` lines 330, 341, or 352, and that no corresponding `remove_block_status` is ever called.
5. After sufficient iterations (scale depends on attacker bandwidth and node RAM), the node process is terminated by the OS OOM killer or becomes unresponsive due to memory pressure.

The permanent accumulation site: [1](#0-0) 

The unbounded insertion triggered per invalid header: [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** shared/src/shared.rs (L72-72)
```rust
    pub(crate) block_status_map: Arc<DashMap<Byte32, BlockStatus>>,
```

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
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

**File:** sync/src/synchronizer/headers_process.rs (L133-141)
```rust
        match result.state {
            ValidationState::Invalid => {
                debug!(
                    "HeadersProcess accept_first result is invalid, error = {:?}, first header = {:?}",
                    result.error, headers[0]
                );
                return StatusCode::HeadersIsInvalid
                    .with_context(format!("accept first header {:?}", headers[0]));
            }
```

**File:** sync/src/synchronizer/headers_process.rs (L324-354)
```rust
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
```

**File:** chain/src/chain_service.rs (L120-130)
```rust
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
```
