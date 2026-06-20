### Title
`shared_best_header` Anchor Updated with Header from Previously-Invalid Block — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` contains an acknowledged `FIXME`: when a block's status is `BLOCK_INVALID`, the function does not return early. It falls through all header-level checks and, if those pass, calls `insert_valid_header`, which updates `shared_best_header` — the node's global sync anchor. This is the direct CKB analog to the oracle report: a "last good" fallback value is updated during the exact condition (known block invalidity) that should prevent the update.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` begins with an explicit developer acknowledgment of the bug:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ...
    state.peers().may_set_best_known_header(self.peer, header_index);
    return result;
}
``` [1](#0-0) 

`BLOCK_INVALID` is a separate bit flag (`1 << 12`) that does not overlap with `HEADER_VALID` (`1`): [2](#0-1) 

So when `get_block_status` returns `BLOCK_INVALID`, the `status.contains(BlockStatus::HEADER_VALID)` guard is false, and the code falls through to `prev_block_check`, `non_contextual_check`, and `version_check`. A block that failed **full** verification (e.g., script execution failure) but has a structurally valid header passes all three of these checks. The function then reaches:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [3](#0-2) 

`insert_valid_header` unconditionally inserts the header into `header_map`, updates the peer's `best_known_header`, and calls `may_set_shared_best_header`: [4](#0-3) 

`shared_best_header` is the node-wide "best known header" anchor. It drives sync decisions, IBD state, peer eviction timeouts, and block download targeting — the direct analog to `lastGoodPrice`.

---

### Impact Explanation

An unprivileged remote peer can send a `SendHeaders` P2P message containing the header of a block that the local node has already marked `BLOCK_INVALID` (e.g., a block that failed script verification). Because the FIXME guard is absent, `insert_valid_header` is called, poisoning `shared_best_header` with a header from a known-invalid block. Downstream effects:

- **Sync anchor corruption**: `shared_best_header` is used by `BlockFetchCMD::can_start()` and the eviction logic to determine which chain to follow and which peers to keep.
- **Wasted download work**: The block fetcher will attempt to download blocks on a chain rooted at an invalid block, all of which will fail verification.
- **Peer eviction interference**: The `chain_sync` timeout logic in `eviction()` compares `best_known_header` against `shared_best_header`; a poisoned anchor skews these comparisons, potentially protecting malicious peers from eviction or evicting honest ones. [5](#0-4) 

---

### Likelihood Explanation

- **Entry path**: Any connected P2P peer can send a `SendHeaders` message. No authentication or privilege is required.
- **Precondition**: The target block must already be in `block_status_map` as `BLOCK_INVALID`. This is achievable: an attacker first submits a block with a valid header but invalid script (causing `BLOCK_INVALID` to be set), then re-sends the header via `SendHeaders`.
- **Persistence**: `block_status_map` is in-memory and survives for the node's session. The poisoned `shared_best_header` persists until a legitimate higher-difficulty header overwrites it via `may_set_shared_best_header`.
- **Developer acknowledgment**: The `FIXME` comment confirms this is a known, unresolved gap. [6](#0-5) 

---

### Recommendation

In `HeaderAcceptor::accept()`, add an explicit early-return guard for `BLOCK_INVALID` before the existing `HEADER_VALID` check:

```rust
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None); // or a dedicated ValidationError variant
    return result;
}
```

This mirrors the fix applied in `OracleAdapterV5`: do not update the trusted anchor (`shared_best_header` / `lastGoodPrice`) when the triggering condition is itself flagged as invalid/suspicious.

---

### Proof of Concept

1. Attacker connects to a CKB node as a P2P peer.
2. Attacker relays a block `B` with a valid header (passes `HeaderVerifier`) but an invalid script in its transactions. The node processes `B`, script verification fails, and `block_status_map[B.hash] = BLOCK_INVALID` is set. [7](#0-6) 

3. Attacker sends a `SendHeaders` message containing `B`'s header.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for `B`'s header.
5. `get_block_status(B.hash)` returns `BLOCK_INVALID`. The `HEADER_VALID` guard is false; no early return.
6. `prev_block_check` passes (B's parent is valid). `non_contextual_check` passes (header is structurally sound). `version_check` passes.
7. `insert_valid_header(peer, B.header)` is called → `shared_best_header` is updated to point to `B`'s header.
8. The node's sync anchor is now set to a header from a known-invalid block, degrading sync safety for the remainder of the session. [8](#0-7)

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

**File:** sync/src/synchronizer/mod.rs (L583-614)
```rust
                let best_known_header = state.best_known_header.as_ref();
                let (tip_header, local_total_difficulty) = {
                    (
                        active_chain.tip_header().to_owned(),
                        active_chain.total_difficulty().to_owned(),
                    )
                };
                if best_known_header
                    .map(|header_index| header_index.total_difficulty().clone())
                    .unwrap_or_default()
                    >= local_total_difficulty
                {
                    if state.chain_sync.timeout != 0 {
                        state.chain_sync.timeout = 0;
                        state.chain_sync.work_header = None;
                        state.chain_sync.total_difficulty = None;
                        state.chain_sync.sent_getheaders = false;
                    }
                } else if state.chain_sync.timeout == 0
                    || (best_known_header.is_some()
                        && best_known_header
                            .map(|header_index| header_index.total_difficulty().clone())
                            >= state.chain_sync.total_difficulty)
                {
                    // Our best block known by this peer is behind our tip, and we're either noticing
                    // that for the first time, OR this peer was able to catch up to some earlier point
                    // where we checked against our tip.
                    // Either way, set a new timeout based on current tip.
                    state.chain_sync.timeout = now + CHAIN_SYNC_TIMEOUT;
                    state.chain_sync.work_header = Some(tip_header);
                    state.chain_sync.total_difficulty = Some(local_total_difficulty);
                    state.chain_sync.sent_getheaders = false;
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
