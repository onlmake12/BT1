### Title
`BLOCK_INVALID` Status Ignored in `HeaderAcceptor::accept()`, Allowing Invalid Block to Corrupt Sync State — (`File: sync/src/synchronizer/headers_process.rs`)

### Summary

`HeaderAcceptor::accept()` contains a developer-acknowledged FIXME: it only guards against re-processing a header whose status is `HEADER_VALID`, but never guards against `BLOCK_INVALID`. When a block has been permanently marked `BLOCK_INVALID` by the chain verifier, any unprivileged P2P peer can re-submit that header via a `SendHeaders` message. Because the invalid-status guard is missing, the function falls through all sub-checks and calls `insert_valid_header`, which inserts the header into the `header_map`, updates the peer's `best_known_header`, and can overwrite the global `shared_best_header` with a block the node already knows is invalid.

---

### Finding Description

`HeaderAcceptor::accept()` in `sync/src/synchronizer/headers_process.rs` reads the current `BlockStatus` for the incoming header and then immediately checks only for `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best known and return
    return result;
}
``` [1](#0-0) 

`BlockStatus` is a bitflag type. `BLOCK_INVALID = 1 << 12` and `HEADER_VALID = 1` share no bits, so `status.contains(BlockStatus::HEADER_VALID)` is `false` when the status is `BLOCK_INVALID`, and the early-return is never taken. [2](#0-1) 

The function then runs `prev_block_check`, `non_contextual_check` (header-only PoW/timestamp/epoch checks), and `version_check`. For a block that was marked `BLOCK_INVALID` by the full chain verifier (i.e., the header itself was valid but the block body failed script execution or contextual verification), all three sub-checks pass. The function then calls `insert_valid_header`: [3](#0-2) 

`insert_valid_header` inserts the header into `header_map`, sets the peer's `best_known_header` to this invalid block, and calls `may_set_shared_best_header`, which unconditionally overwrites the global `shared_best_header` if the invalid block's total difficulty is higher than the current best: [4](#0-3) 

`may_set_shared_best_header` replaces the global `shared_best_header` with no validity check: [5](#0-4) 

The `BLOCK_INVALID` status in `block_status_map` is not cleared — `get_block_status` checks `block_status_map` first — but the `header_map` entry and the peer/global best-header pointers are now corrupted. [6](#0-5) 

---

### Impact Explanation

**Sync state machine corruption.** `shared_best_header` is the authoritative source for:

1. `min_chain_work_ready()` — used to gate IBD block download. If an attacker-controlled invalid block has artificially high difficulty, the node prematurely believes it has reached minimum chain work and begins downloading blocks from a chain it cannot validate. [7](#0-6) 

2. `BlockFetchCMD::process_fetch_cmd()` — the `CanStart::MinWorkNotReach` and `CanStart::AssumeValidNotFound` branches both read `shared_best_header_ref()` to log progress and decide whether to start fetching. A corrupted value causes incorrect IBD gating decisions. [8](#0-7) 

3. `BlockFetcher::fetch()` — uses the peer's `best_known_header` (set by `insert_valid_header`) to compute which block hashes to request via `GetBlocks`. The fetcher will issue `GetBlocks` requests for blocks on a chain the node already knows is invalid, wasting bandwidth and triggering redundant processing. [9](#0-8) 

---

### Likelihood Explanation

**Low.** The precondition is that a block must have been previously marked `BLOCK_INVALID` by the full chain verifier (`consume_unverified_blocks` in `chain/src/verify.rs`) — meaning the block passed header verification but failed body/script verification. A malicious miner can craft such a block (valid PoW header, invalid body). Once such a block exists on the network, any peer can replay its header via `SendHeaders` to trigger the bug. No privileged access is required; the `SendHeaders` message is a standard unprivileged P2P sync message. [10](#0-9) 

---

### Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `accept()`, directly resolving the FIXME:

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

This mirrors the pattern already used in `compact_block_process.rs`'s `contextual_check`, which correctly returns `BlockIsInvalid` when `status.contains(BlockStatus::BLOCK_INVALID)`: [11](#0-10) 

---

### Proof of Concept

1. Attacker mines block **B** with a valid PoW header but an invalid body (e.g., a script that always fails). The node receives **B** via compact block relay, passes header verification, stores it, runs full verification, fails, and sets `block_status_map[B.hash] = BLOCK_INVALID`. [10](#0-9) 

2. Attacker (or any peer) sends a `SendHeaders` P2P message containing **B**'s header to the victim node.

3. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for **B**'s header. [12](#0-11) 

4. `status = BLOCK_INVALID`. The guard `status.contains(HEADER_VALID)` is `false`. No early return.

5. `prev_block_check` passes (B's parent is valid). `non_contextual_check` passes (B's header is valid PoW). `version_check` passes.

6. `insert_valid_header` is called. `shared_best_header` is updated to **B** if **B**'s total difficulty exceeds the current best. [13](#0-12) 

7. The node's sync state machine now believes the best known chain ends at an invalid block, corrupting IBD gating and block-fetch decisions.

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L295-322)
```rust
    pub fn accept(&self) -> ValidationResult {
        let mut result = ValidationResult::default();
        let sync_shared = self.active_chain.sync_shared();
        let state = self.active_chain.state();
        let shared = sync_shared.shared();

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

**File:** sync/src/types/mod.rs (L1348-1352)
```rust
    pub fn min_chain_work_ready(&self) -> bool {
        self.shared_best_header
            .read()
            .is_better_than(&self.min_chain_work)
    }
```

**File:** sync/src/types/mod.rs (L1398-1408)
```rust
    pub fn may_set_shared_best_header(&self, header: HeaderIndexView) {
        let mut shared_best_header = self.shared_best_header.write();
        if !header.is_better_than(shared_best_header.total_difficulty()) {
            return;
        }

        if let Some(metrics) = ckb_metrics::handle() {
            metrics.ckb_shared_best_number.set(header.number() as i64);
        }
        *shared_best_header = header;
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

**File:** sync/src/synchronizer/mod.rs (L124-165)
```rust
            CanStart::MinWorkNotReach => {
                let best_known = self.sync_shared.state().shared_best_header_ref();
                let number = best_known.number();
                if number != self.number && (number - self.number).is_multiple_of(10000) {
                    self.number = number;
                    info!(
                        "The current best known header number: {}, total difficulty: {:#x}. \
                                 Block download minimum requirements: header number: 500_000, total difficulty: {:#x}.",
                        number,
                        best_known.total_difficulty(),
                        self.sync_shared.state().min_chain_work()
                    );
                }
            }
            CanStart::AssumeValidNotFound => {
                let state = self.sync_shared.state();
                let shared = self.sync_shared.shared();
                let best_known = state.shared_best_header_ref();
                let number = best_known.number();
                let assume_valid_target: Byte32 = shared
                    .assume_valid_targets()
                    .as_ref()
                    .and_then(|targets| targets.first())
                    .map(Pack::pack)
                    .expect("assume valid target must exist");

                if number != self.number && (number - self.number).is_multiple_of(10000) {
                    self.number = number;
                    let remaining_headers_sync_log = self.reaming_headers_sync_log();

                    info!(
                        "best known header {}-{}, \
                                 CKB is syncing to latest Header to find the assume valid target: {}. \
                                 Please wait. {}",
                        number,
                        best_known.hash(),
                        assume_valid_target,
                        remaining_headers_sync_log
                    );
                }
            }
        }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L159-183)
```rust
        let best_known = match self.peer_best_known_header() {
            Some(t) => t,
            None => {
                debug!(
                    "Peer {} doesn't have best known header; ignore it",
                    self.peer
                );
                return None;
            }
        };
        if !best_known.is_better_than(self.active_chain.total_difficulty()) {
            // Advancing this peer's last_common_header is unnecessary for block-sync mechanism.
            // However, RPC `get_peers`, returns peers information which includes
            // last_common_header, is expected to provide a more realistic picture. Hence here we
            // specially advance this peer's last_common_header at the case of both us on the same
            // active chain.
            if self.active_chain.is_main_chain(&best_known.hash()) {
                self.sync_shared
                    .state()
                    .peers()
                    .set_last_common_header(self.peer, best_known.number_and_hash());
            }

            return None;
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

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
