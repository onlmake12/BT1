Audit Report

## Title
Missing `BLOCK_INVALID` Status Check in `BlockFetcher::fetch()` Causes Repeated Re-Download of Invalid Blocks — (File: `sync/src/synchronizer/block_fetcher.rs`)

## Summary

The inner loop of `BlockFetcher::fetch()` checks only `BLOCK_STORED` and `BLOCK_RECEIVED` before queuing a block for download. Because `BLOCK_INVALID` (`1 << 12`) shares no bits with either guard, a block whose in-memory status is `BLOCK_INVALID` falls through to the inflight-insert branch and is re-queued on every fetch cycle. An attacker who mines one block with a valid header but invalid body can cause a victim node to repeatedly re-download and re-verify that block for the duration of the session, wasting bandwidth and CPU.

## Finding Description

In `sync/src/synchronizer/block_fetcher.rs` lines 257–284, the status-check cascade is:

```rust
if status.contains(BlockStatus::BLOCK_STORED) {
    ...
    break;
} else if status.contains(BlockStatus::BLOCK_RECEIVED) {
    // Do not download repeatedly
} else if ... && state.write_inflight_blocks().insert(...) {
    fetch.push(header)   // ← reached when status == BLOCK_INVALID
}
```

`BlockStatus` is defined in `shared/src/block_status.rs`:

```
BLOCK_RECEIVED = 0b011
BLOCK_STORED   = 0b111
BLOCK_INVALID  = 1 << 12   // completely disjoint
```

`BLOCK_INVALID` satisfies neither `contains(BLOCK_STORED)` nor `contains(BLOCK_RECEIVED)`, so it always reaches the `fetch.push` branch.

The status is set by `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` in `chain/src/verify.rs` lines 173–177: on contextual verification failure, `delete_unverified_block` removes the block body from the DB, then `insert_block_status(block_hash, BlockStatus::BLOCK_INVALID)` writes the status into the in-memory `block_status_map`. On the next `BlockFetcher` poll, `get_block_status` (`shared/src/shared.rs` lines 425–444) returns `BLOCK_INVALID` from the map. The fetcher ignores this and re-queues the block.

The contrast with other handlers is explicit: `compact_block_process.rs` line 259 returns early on `BLOCK_INVALID`, and `headers_process.rs` line 301 carries a `FIXME` acknowledging the same gap.

**Important scope correction vs. the submitted claim**: the claim states the block body persists in the DB with `block_ext.verified = Some(false)`. This is incorrect. `delete_unverified_block` is called before `insert_block_status`, so the block is removed from the DB. After a node restart the in-memory map is cleared and `get_block_status` returns `BLOCK_UNKNOWN`, resetting the attack. The vulnerability is therefore session-scoped, not persistent across restarts.

## Impact Explanation

The concrete impact is repeated bandwidth consumption (re-downloading the invalid block body) and CPU consumption (re-running CKB-VM script verification) on the victim node, bounded to the current session. This does not crash the node, does not cause consensus deviation, and does not affect the wider network unless many nodes are targeted simultaneously. The closest allowed impact is **Low (501–2000 points): Any other important performance improvements for CKB**, as the bug represents a suboptimal resource-management decision that a fix would directly improve.

## Likelihood Explanation

The entry path is reachable by any unprivileged P2P peer. The one-time cost is mining a single block with valid PoW but an invalid body (e.g., a lock script that always aborts). After the first delivery the attacker peer is banned, so continuous re-download requires the attacker to reconnect under different peer identities (Sybil) and re-advertise the same chain tip. Each reconnection is cheap; the PoW cost is paid once. The attack resets on victim node restart. The combination of PoW requirement plus Sybil requirement makes sustained exploitation moderately costly for the attacker.

## Recommendation

Add an explicit `BLOCK_INVALID` guard in the `BlockFetcher::fetch` inner loop, analogous to the existing `BLOCK_STORED` and `BLOCK_RECEIVED` guards:

```rust
if status.contains(BlockStatus::BLOCK_STORED) {
    ...
    break;
} else if status.contains(BlockStatus::BLOCK_RECEIVED) {
    // Do not download repeatedly
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    // Do not re-download persistently invalid blocks; skip entire window
    break;
} else if ... {
    fetch.push(header)
}
```

This mirrors the guard already present in `compact_block_process.rs` line 259 and resolves the `FIXME` noted in `headers_process.rs` line 301.

## Proof of Concept

1. Attacker mines block `B` at height `N`: valid PoW, valid header, invalid body (lock script exits with code 1).
2. Attacker connects to victim as a sync peer, sends `SendHeaders` for a chain whose tip descends from `B`.
3. Victim's `HeaderAcceptor::accept()` validates the header; `best_known_header` is updated to the attacker's tip.
4. `BlockFetcher::fetch()` traverses from `best_known` to `last_common`, finds `B` with status `BLOCK_UNKNOWN`, inserts it into `InflightBlocks`, sends `GetBlocks`.
5. Attacker responds with `B`. `ConsumeUnverifiedBlockProcessor` runs CKB-VM, fails → `delete_unverified_block(B)` then `insert_block_status(B, BLOCK_INVALID)`. Attacker peer is banned.
6. After `BLOCK_DOWNLOAD_TIMEOUT` (30 s), `InflightBlocks::prune` removes the timed-out entry for `B`.
7. On the next `BlockFetcher` poll for any peer that still advertises the same chain tip: `get_block_status(B)` → `BLOCK_INVALID` (from in-memory map); `BLOCK_STORED` check → false; `BLOCK_RECEIVED` check → false; block is re-inserted into `InflightBlocks` and re-requested.
8. If the attacker reconnects under a new peer identity and responds, `B` is re-downloaded and re-verified, failing again. Steps 6–8 repeat every ~30 s for the session lifetime. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** chain/src/verify.rs (L153-181)
```rust
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

**File:** shared/src/shared.rs (L425-444)
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
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L301-303)
```rust
        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
```

**File:** util/constant/src/sync.rs (L48-48)
```rust
pub const BLOCK_DOWNLOAD_TIMEOUT: u64 = 30 * 1000; // 30s
```
