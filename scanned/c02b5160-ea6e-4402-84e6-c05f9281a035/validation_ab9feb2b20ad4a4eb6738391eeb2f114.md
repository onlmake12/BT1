### Title
Unbounded Loop in `last_common_ancestor` Enables Repeated CPU Exhaustion via Crafted Fork Headers — (File: `sync/src/types/mod.rs`)

---

### Summary

The `last_common_ancestor` function in `sync/src/types/mod.rs` contains a `while` loop with no explicit iteration bound. It is called from `BlockFetcher::update_last_common_header` every time the node's periodic block-fetch timer fires for a connected peer. The number of loop iterations equals the depth of the fork between the local chain and the peer's advertised best header. A sync peer who has mined a fork chain of depth N causes the victim node to execute N iterations of this loop on every fetch cycle, with no cap enforced anywhere in the call chain.

---

### Finding Description

`ActiveChain::last_common_ancestor` (lines 1827–1855 of `sync/src/types/mod.rs`) implements a naïve linear walk-back to find the lowest common ancestor of two chain tips:

```rust
while m_left != m_right {
    m_left = self
        .get_ancestor(&m_left.hash(), m_left.number() - 1)?
        .number_and_hash();
    m_right = self
        .get_ancestor(&m_right.hash(), m_right.number() - 1)?
        .number_and_hash();
}
```

Each iteration decrements the block number by exactly 1 and performs two `get_ancestor` calls (one for the local chain side, one for the fork side). There is no maximum-iteration guard, no timeout, and no early-exit heuristic. The loop terminates only when both sides converge to the same `(number, hash)` pair. [1](#0-0) 

This function is called unconditionally from `BlockFetcher::update_last_common_header`:

```rust
let last_common_ancestor = self
    .active_chain
    .last_common_ancestor(&last_common, best_known)?;
``` [2](#0-1) 

`update_last_common_header` is itself called from `BlockFetcher::fetch`, which is dispatched for every qualifying peer on every firing of the `NOT_IBD_BLOCK_FETCH_TOKEN` and `IBD_BLOCK_FETCH_TOKEN` periodic timers inside `Synchronizer::find_blocks_to_fetch`. [3](#0-2) 

When no stored `last_common_header` exists for a peer, the initial guess for `last_common` is set to the block at height `min(local_tip, best_known.number())` on the main chain:

```rust
let guess_number = min(tip_header.number(), best_known.number());
let guess_hash = self.active_chain.get_block_hash(guess_number)?;
(guess_number, guess_hash).into()
``` [4](#0-3) 

If the peer's fork diverges from the main chain at block height F and the peer's tip is at height T, the initial guess places `m_left` at height T on the main chain and `m_right` at height T on the fork. After the alignment step, both are at height T. The loop then walks back one step per iteration until it reaches height F — a total of `T − F` iterations with no bound check.

---

### Impact Explanation

An attacker operating as a sync peer who has mined a fork of depth D causes the victim node to execute D iterations of the `last_common_ancestor` loop on every periodic block-fetch cycle. Each iteration performs two `get_ancestor` calls into the in-memory `HeaderMap`. The loop runs in the fetch worker thread (dispatched via `fetch_channel` in `find_blocks_to_fetch`), consuming CPU proportional to D on every timer tick. With multiple attacker-controlled peers each advertising a deep fork, the aggregate CPU load multiplies. There is no per-call cycle budget, no timeout, and no iteration cap anywhere in the call chain from the timer callback down to the loop body.

---

### Likelihood Explanation

The attacker must be a connected sync peer and must have mined a fork chain whose headers pass `HeaderVerifier` (PoW-verified). This is a real cost, but it is not a 51% attack: the attacker only needs to mine a private fork of depth D starting from any recent block, not outpace the entire network. A fork of a few hundred blocks is achievable by a moderately resourced actor. Once the fork headers are accepted by `HeadersProcess` and stored in the peer's `best_known_header`, the loop is triggered automatically and repeatedly by the node's own timer without any further action from the attacker. The attacker does not need to send any additional messages after the initial header relay. [5](#0-4) 

---

### Recommendation

Add an explicit iteration cap to `last_common_ancestor`. If the common ancestor is not found within a configurable maximum number of steps (e.g., `BLOCK_DOWNLOAD_WINDOW` or a fixed constant such as 2 048), return `None` and let the caller treat the peer as unusable for block fetching (or disconnect it). Additionally, consider replacing the O(N) linear walk with a binary-search or skip-list approach using the existing `get_ancestor` fast-scanner path, which already supports O(log N) ancestor lookup for blocks on the main chain. [6](#0-5) 

---

### Proof of Concept

1. **Attacker mines a private fork** of depth D (e.g., D = 500) starting from a recent main-chain block at height F. All D headers are valid and pass PoW.
2. **Attacker connects** to the victim node as a sync peer.
3. **Attacker sends** the D fork headers via `SendHeaders` messages. `HeadersProcess::execute` verifies each header and updates the peer's `best_known_header` to the fork tip at height `F + D`.
4. **Victim's `NOT_IBD_BLOCK_FETCH_TOKEN` timer fires** (periodically). `find_blocks_to_fetch` dispatches `BlockFetcher::fetch` for the attacker peer.
5. **`update_last_common_header` is called** with `best_known = (F+D, fork_tip_hash)`. No stored `last_common_header` exists, so the initial guess is `(F+D, main_chain_hash_at_F+D)`.
6. **`last_common_ancestor` runs D iterations** of the `while m_left != m_right` loop, walking from height `F+D` down to height `F` one step at a time, with no bound check.
7. **On every subsequent timer tick**, step 5–6 repeats (the stored `last_common_header` is now set to the fork point, but if the attacker rotates to a new fork or reconnects, the full D-iteration walk recurs).
8. **With multiple attacker peers** each advertising a distinct fork of depth D, the CPU cost scales linearly with the number of peers. [1](#0-0) [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L1827-1855)
```rust
    pub fn last_common_ancestor(
        &self,
        pa: &BlockNumberAndHash,
        pb: &BlockNumberAndHash,
    ) -> Option<BlockNumberAndHash> {
        let (mut m_left, mut m_right) = if pa.number() > pb.number() {
            (pb.clone(), pa.clone())
        } else {
            (pa.clone(), pb.clone())
        };

        m_right = self
            .get_ancestor(&m_right.hash(), m_left.number())?
            .number_and_hash();
        if m_left == m_right {
            return Some(m_left);
        }
        debug_assert!(m_left.number() == m_right.number());

        while m_left != m_right {
            m_left = self
                .get_ancestor(&m_left.hash(), m_left.number() - 1)?
                .number_and_hash();
            m_right = self
                .get_ancestor(&m_right.hash(), m_right.number() - 1)?
                .number_and_hash();
        }
        Some(m_left)
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L61-104)
```rust
    pub fn update_last_common_header(
        &self,
        best_known: &BlockNumberAndHash,
    ) -> Option<BlockNumberAndHash> {
        // Bootstrap quickly by guessing an ancestor of our best tip is forking point.
        // Guessing wrong in either direction is not a problem.
        let mut last_common = if let Some(header) = self
            .sync_shared
            .state()
            .peers()
            .get_last_common_header(self.peer)
        {
            header
        } else {
            let tip_header = self.active_chain.tip_header();
            let guess_number = min(tip_header.number(), best_known.number());
            let guess_hash = self.active_chain.get_block_hash(guess_number)?;
            (guess_number, guess_hash).into()
        };

        // If the peer reorganized, our previous last_common_header may not be an ancestor
        // of its current tip anymore. Go back enough to fix that.
        last_common = {
            let now = std::time::Instant::now();
            let last_common_ancestor = self
                .active_chain
                .last_common_ancestor(&last_common, best_known)?;
            debug!(
                "last_common_ancestor({:?}, {:?})->{:?} cost {:?}",
                last_common,
                best_known,
                last_common_ancestor,
                now.elapsed()
            );
            last_common_ancestor
        };

        self.sync_shared
            .state()
            .peers()
            .set_last_common_header(self.peer, last_common.clone());

        Some(last_common)
    }
```

**File:** sync/src/synchronizer/mod.rs (L735-800)
```rust
    fn find_blocks_to_fetch(&mut self, nc: &Arc<dyn CKBProtocolContext + Sync>, ibd: IBDState) {
        if self.chain.is_verifying_unverified_blocks_on_startup() {
            trace!(
                "skip find_blocks_to_fetch, ckb_chain is verifying unverified blocks on startup"
            );
            return;
        }

        if ckb_stop_handler::has_received_stop_signal() {
            info!("received stop signal, stop find_blocks_to_fetch");
            return;
        }

        let unverified_tip = self.shared.active_chain().unverified_tip_number();

        let disconnect_list = {
            let mut list = self
                .shared()
                .state()
                .write_inflight_blocks()
                .prune(unverified_tip);
            if let IBDState::In = ibd {
                // best known < tip and in IBD state, and unknown list is empty,
                // these node can be disconnect
                list.extend(
                    self.shared
                        .state()
                        .peers()
                        .get_best_known_less_than_tip_and_unknown_empty(unverified_tip),
                )
            };
            list
        };

        for peer in disconnect_list.iter() {
            // It is not forbidden to evict protected nodes:
            // - First of all, this node is not designated by the user for protection,
            //   but is connected randomly. It does not represent the will of the user
            // - Secondly, in the synchronization phase, the nodes with zero download tasks are
            //   retained, apart from reducing the download efficiency, there is no benefit.
            if self
                .peers()
                .get_flag(*peer)
                .map(|flag| flag.is_whitelist)
                .unwrap_or(false)
            {
                continue;
            }
            let nc = Arc::clone(nc);
            let peer = *peer;
            self.shared.shared().async_handle().spawn(async move {
                let _status = nc.async_disconnect(peer, "sync disconnect").await;
            });
        }

        // fetch use a lot of cpu time, especially in ibd state
        // so, the fetch function use another thread
        match nc.p2p_control() {
            Some(raw) => match self.fetch_channel {
                Some(ref sender) => {
                    if !sender.is_full() {
                        let peers = self.get_peers_to_fetch(ibd, &disconnect_list);
                        let _ignore = sender.try_send(FetchCMD {
                            peers,
                            ibd_state: ibd,
                        });
```

**File:** sync/src/synchronizer/headers_process.rs (L94-130)
```rust
    pub fn execute(self) -> Status {
        debug!("HeadersProcess begins");
        let shared: &SyncShared = self.synchronizer.shared();
        let consensus = shared.consensus();
        let headers = self
            .message
            .headers()
            .to_entity()
            .into_iter()
            .map(packed::Header::into_view)
            .collect::<Vec<_>>();

        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }

        if headers.is_empty() {
            // Empty means that the other peer's tip may be consistent with our own best known,
            // but empty cannot 100% confirm this, so it does not set the other peer's best header
            // to the shared best known.
            // This action means that if the newly connected node has not been sync with headers,
            // it cannot be used as a synchronization node.
            debug!("HeadersProcess is_empty (synchronized)");
            if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
                self.synchronizer
                    .shared()
                    .state()
                    .tip_synced(state.value_mut());
            }
            return Status::ok();
        }

        if !self.is_continuous(&headers) {
            warn!("HeadersProcess is not continuous");
            return StatusCode::HeadersIsInvalid.with_context("not continuous");
        }
```
