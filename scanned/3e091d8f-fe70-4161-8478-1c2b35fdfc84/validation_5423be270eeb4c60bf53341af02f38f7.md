Audit Report

## Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept()` Enables Repeated Block Download of Known-Invalid Blocks — (`File: sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains a developer-acknowledged `FIXME` comment noting that a `BLOCK_INVALID` early-return is absent. Because `BLOCK_INVALID` (bit `1 << 12`) does not overlap with `HEADER_VALID` (bit `1`), a header for a block already marked `BLOCK_INVALID` bypasses the only early-return guard, passes all three header-level checks, and causes `insert_valid_header` to insert the header into the `header_map`. This causes the `BlockFetcher` to repeatedly schedule downloads of the same known-invalid block, wasting bandwidth and network resources.

## Finding Description

`BlockStatus::BLOCK_INVALID = 1 << 12 = 4096` and `BlockStatus::HEADER_VALID = 1` share no bits. [1](#0-0) 

In `HeaderAcceptor::accept()`, the only early-return checks `status.contains(BlockStatus::HEADER_VALID)`. When status is `BLOCK_INVALID`, this check evaluates to false and execution falls through. [2](#0-1) 

The three subsequent checks — `prev_block_check`, `non_contextual_check`, `version_check` — are header-level only. A block can be marked `BLOCK_INVALID` during full contextual verification (invalid transactions, script failure) while its header still passes all three. When all pass, `insert_valid_header` is called: [3](#0-2) 

`insert_valid_header` inserts the header into `header_map` and updates the peer's best-known header, but does **not** update `block_status_map`: [4](#0-3) 

`get_block_status` checks `block_status_map` first. Since `BLOCK_INVALID` remains in `block_status_map`, subsequent calls still return `BLOCK_INVALID`: [5](#0-4) 

The `BlockFetcher` has no `BLOCK_INVALID` guard. It only skips blocks with `BLOCK_STORED` or `BLOCK_RECEIVED` status. A block with `BLOCK_INVALID` status falls through to the inflight-insert branch and is scheduled for download: [6](#0-5) 

When the block arrives, `new_block_received` performs an equality check `!BlockStatus::HEADER_VALID.eq(&status)`. Since status is `BLOCK_INVALID`, it returns false and removes the inflight entry without processing the block: [7](#0-6) 

With the inflight entry removed, the next `BlockFetcher` cycle re-adds the same block to inflight, creating an indefinite loop of GetBlocks requests and block downloads for a known-invalid block.

The `BLOCK_INVALID` guard is correctly implemented in `CompactBlockProcess` (`contextual_check`) and `asynchronous_process_remote_block`, confirming this is an isolated omission in `HeaderAcceptor::accept()`: [8](#0-7) [9](#0-8) 

## Impact Explanation

An attacker can cause a target node to repeatedly issue `GetBlocks` requests and download the same known-invalid block indefinitely, wasting bandwidth and network resources. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**, as the root cause is the missing guard in the sync state management that allows the `header_map` and `block_status_map` to fall out of sync, driving the block fetcher into a perpetual re-download loop. The node does not crash and consensus is not affected; the harm is resource exhaustion.

## Likelihood Explanation

The attacker must possess a block whose header passes `HeaderVerifier` (valid PoW, valid structure) but whose body fails full contextual verification. This requires either: (a) mining such a block (significant cost), or (b) knowing the hash of a naturally-occurring rejected block (rare but free). Once the hash is known, any unprivileged peer can send a `SendHeaders` message containing that header. No keys, privileges, or majority hashpower are required beyond the initial block. The attack is repeatable indefinitely per connection.

## Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `accept()`, immediately before the `HEADER_VALID` check, resolving the existing `FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent));
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic ...
}
```

This mirrors the guard already present in `CompactBlockProcess::contextual_check` and `asynchronous_process_remote_block`. [10](#0-9) 

## Proof of Concept

1. Build a block B whose header passes `HeaderVerifier` (valid PoW, correct parent, version 0) but whose body contains an invalid transaction (e.g., double-spend or script failure).
2. Submit B to the target node via `SendBlock`. Full verification in `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` fails; `block_status_map[B.hash] = BLOCK_INVALID`. [11](#0-10) 
3. Send a `SendHeaders` message containing B's header to the target node.
4. `HeaderAcceptor::accept()` reads status `BLOCK_INVALID`, skips the `HEADER_VALID` branch, passes all three header-level checks, and calls `insert_valid_header`. B's header is now in `header_map`; `block_status_map` still holds `BLOCK_INVALID`.
5. `BlockFetcher` traverses from the peer's updated best-known header, encounters B with status `BLOCK_INVALID` (not `BLOCK_STORED`/`BLOCK_RECEIVED`), and adds B to inflight.
6. Node sends `GetBlocks` for B; peer responds with B.
7. `new_block_received` returns false (`BLOCK_INVALID ≠ HEADER_VALID`); inflight entry removed.
8. Steps 5–7 repeat on every `BlockFetcher` cycle without any further action from the attacker.

To reproduce in a test: set `block_status_map[hash] = BLOCK_INVALID`, call `insert_valid_header` for the same hash, then run `BlockFetcher::fetch` and assert the block hash appears in the returned fetch list.

### Citations

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
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

**File:** sync/src/synchronizer/headers_process.rs (L324-357)
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

        sync_shared.insert_valid_header(self.peer, self.header);
        result
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

**File:** sync/src/types/mod.rs (L1209-1227)
```rust
        let status = self.active_chain().get_block_status(&block.hash());
        debug!(
            "new_block_received {}-{}, status: {:?}",
            block.number(),
            block.hash(),
            status
        );
        if !BlockStatus::HEADER_VALID.eq(&status) {
            return false;
        }

        if let dashmap::mapref::entry::Entry::Vacant(status) =
            self.shared().block_status_map().entry(block.hash())
        {
            status.insert(BlockStatus::BLOCK_RECEIVED);
            return true;
        }
        false
    }
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

**File:** sync/src/synchronizer/block_fetcher.rs (L257-284)
```rust
                if status.contains(BlockStatus::BLOCK_STORED) {
                    if status.contains(BlockStatus::BLOCK_VALID) {
                        // If the block is stored, its ancestor must on store
                        // So we can skip the search of this space directly
                        self.sync_shared
                            .state()
                            .peers()
                            .set_last_common_header(self.peer, header.number_and_hash());
                    }

                    end = window_end(header.number(), BLOCK_DOWNLOAD_WINDOW, best_known.number());
                    break;
                } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
                    // Do not download repeatedly
                } else if (matches!(self.ibd, IBDState::In)
                    || state.compare_with_pending_compact(&hash, now))
                    && state
                        .write_inflight_blocks()
                        .insert(self.peer, (header.number(), hash).into())
                {
                    debug!(
                        "block: {}-{} added to inflight, block_status: {:?}",
                        header.number(),
                        header.hash(),
                        status
                    );
                    fetch.push(header)
                }
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/synchronizer/mod.rs (L472-485)
```rust
        let status = self.shared.active_chain().get_block_status(&block_hash);
        // NOTE: Filtering `BLOCK_STORED` but not `BLOCK_RECEIVED`, is for avoiding
        // stopping synchronization even when orphan_pool maintains dirty items by bugs.
        if status.contains(BlockStatus::BLOCK_STORED) {
            error!("Block {} already stored", block_hash);
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
        } else {
            debug!(
                "Synchronizer process_new_block unexpected status {:?} {}",
                status, block_hash,
            );
            // TODO which error should we return?
        }
```

**File:** chain/src/verify.rs (L175-178)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
```
