Audit Report

## Title
Unbounded Linear Walk in `last_common_ancestor` Enables Repeated CPU Overhead via PoW-Backed Fork Headers — (File: `sync/src/types/mod.rs`)

## Summary
`ActiveChain::last_common_ancestor` contains a `while` loop that walks back one block at a time with no iteration cap. An attacker who pre-mines a valid fork of depth D and repeatedly connects/disconnects forces the victim to execute O(D) `get_ancestor` calls per connection event. The stored `last_common_header` is fully cleared on every disconnect, so the full walk recurs on every new connection with no bound enforced anywhere in the call chain.

## Finding Description
The loop at lines 1846–1853 of `sync/src/types/mod.rs` decrements both chain tips by exactly one per iteration until they converge:

```rust
while m_left != m_right {
    m_left = self.get_ancestor(&m_left.hash(), m_left.number() - 1)?.number_and_hash();
    m_right = self.get_ancestor(&m_right.hash(), m_right.number() - 1)?.number_and_hash();
}
```

`ActiveChain::get_ancestor` (lines 1721–1772) uses a skip-list internally via `HeaderIndexView::get_ancestor`, but because each call requests exactly `number - 1` (one step back), the skip-list fast-path (`fast_scanner_fn`) only fires when the current block is on the main chain. Fork headers are not on the main chain, so the fast-path returns `None` and each call degrades to a single parent-pointer follow — O(1) per call, O(D) total for the loop. [1](#0-0) [2](#0-1) 

`update_last_common_header` in `sync/src/synchronizer/block_fetcher.rs` (lines 61–104) calls `last_common_ancestor` unconditionally on every invocation. When `get_last_common_header` returns `None` (new peer), it guesses `min(tip_number, best_known_number)` from the main chain, then passes that guess and the fork tip to `last_common_ancestor`, triggering the full O(D) walk. [3](#0-2) 

The critical enabler is `Peers::disconnected` at line 901–923 of `sync/src/types/mod.rs`, which calls `self.state.remove(&peer)`, completely removing the `PeerState` including `last_common_header`. On reconnection, `sync_connected` inserts a fresh `PeerState` with `last_common_header: None`, so the full walk recurs on the next timer tick. [4](#0-3) [5](#0-4) 

`HeadersProcess::execute` accepts up to `MAX_HEADERS_LEN = 2000` headers per message and automatically requests more when the batch is full (line 183–186), allowing an attacker to build an arbitrarily deep fork across multiple message batches with no per-peer total cap. [6](#0-5) [7](#0-6) 

Valid headers do not trigger any ban or rate limit — bans are only issued for malformed messages (`BAD_MESSAGE_BAN_TIME`) or missing common ancestors (`SYNC_USELESS_BAN_TIME`). A peer submitting a valid deep fork is never penalized. [8](#0-7) 

## Impact Explanation
Each reconnection by an attacker holding a pre-mined fork of depth D causes O(D) in-memory `HeaderMap` lookups in the fetch worker thread. With K simultaneous attacker-controlled peers each cycling through connect → send headers → disconnect → reconnect, the aggregate CPU load scales as O(K × D) per connection wave. Individual iterations are fast (single hash-map lookups), but a fork of depth D = 10,000 combined with rapid reconnection cycles and multiple peers can produce sustained elevated CPU usage on the victim node. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**, as the practical impact is CPU overhead and performance degradation rather than a node crash. [2](#0-1) 

## Likelihood Explanation
The attacker must mine D valid PoW-verified headers — a real, non-trivial one-time cost. However, once mined, the same headers can be replayed across unlimited reconnection cycles with only network overhead. There is no reconnection rate limit, no ban triggered by valid header submission, and no iteration guard in `last_common_ancestor`. The attack is self-sustaining after the initial PoW investment and requires no further mining. A moderately resourced attacker can sustain this against a target node indefinitely. [9](#0-8) 

## Recommendation
Add an explicit iteration cap to `last_common_ancestor`. If the common ancestor is not found within a configurable maximum number of steps (e.g., `BLOCK_DOWNLOAD_WINDOW = 8192`), return `None` and treat the peer as unusable for block fetching. Additionally, replace the O(N) linear walk with a binary-search approach using the existing skip-list `get_ancestor` path, which already supports O(log N) ancestor lookup and would reduce the per-connection cost from O(D) to O(log²D). [10](#0-9) 

## Proof of Concept
1. Attacker pre-mines a private fork of depth D (e.g., D = 10,000) starting from a recent main-chain block at height F. All headers pass PoW verification.
2. Attacker connects to the victim as a sync peer and sends the D fork headers across multiple `SendHeaders` batches (each up to 2,000 headers). `HeadersProcess::execute` accepts each batch and updates `best_known_header` to the fork tip at height F+D.
3. Victim's `NOT_IBD_BLOCK_FETCH_TOKEN` timer fires. `find_blocks_to_fetch` dispatches `BlockFetcher::fetch` for the attacker peer. `update_last_common_header` finds no stored `last_common_header`, guesses `min(tip, F+D)` from the main chain, and calls `last_common_ancestor`, which runs D iterations.
4. Attacker disconnects. `Peers::disconnected` calls `self.state.remove(&peer)`, erasing `last_common_header`.
5. Attacker immediately reconnects and re-sends the same D headers. The stored `last_common_header` for the new peer index is absent, so the full D-iteration walk recurs on the next timer tick.
6. Attacker repeats step 4–5 continuously. With K simultaneous attacker peers each cycling through this pattern, the victim's fetch worker thread sustains O(K × D) work per connection wave with no bound enforced anywhere in the call chain. [4](#0-3) [11](#0-10)

### Citations

**File:** sync/src/types/mod.rs (L280-288)
```rust
    pub fn new(peer_flags: PeerFlags) -> PeerState {
        PeerState {
            headers_sync_controller: None,
            peer_flags,
            chain_sync: ChainSyncState::default(),
            best_known_header: None,
            last_common_header: None,
            unknown_header_list: Vec::new(),
        }
```

**File:** sync/src/types/mod.rs (L901-902)
```rust
    pub fn disconnected(&self, peer: PeerIndex) {
        if let Some(peer_state) = self.state.remove(&peer).map(|(_, peer_state)| peer_state) {
```

**File:** sync/src/types/mod.rs (L1759-1766)
```rust
        let fast_scanner_fn = |number: BlockNumber, current: BlockNumberAndHash| {
            // shortcut to return an ancestor block
            if current.number <= tip_number && block_is_on_chain_fn(&current.hash) {
                self.get_block_hash(number)
                    .and_then(|hash| self.sync_shared.get_header_index_view(&hash, true))
            } else {
                None
            }
```

**File:** sync/src/types/mod.rs (L1846-1853)
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

**File:** sync/src/synchronizer/block_fetcher.rs (L67-96)
```rust
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
```

**File:** sync/src/synchronizer/headers_process.rs (L106-109)
```rust
        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L183-186)
```rust
        if headers.len() == MAX_HEADERS_LEN {
            let start = headers.last().expect("empty checked").into();
            self.active_chain
                .send_getheaders_to_peer(self.nc, self.peer, start);
```

**File:** util/constant/src/sync.rs (L8-8)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```

**File:** util/constant/src/sync.rs (L59-65)
```rust
/// Default ban time for message
// ban time
// 5 minutes
pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);
/// Default ban time for sync useless
// 10 minutes, peer have no common ancestor block
pub const SYNC_USELESS_BAN_TIME: Duration = Duration::from_secs(10 * 60);
```
