### Title
Outbound Peer Eviction Timer Indefinitely Defeatable via Periodic `SendHeaders` Relay — (`File: sync/src/synchronizer/mod.rs`)

---

### Summary

The `eviction()` function in CKB's synchronizer enforces `CHAIN_SYNC_TIMEOUT` (12 minutes) to disconnect outbound peers whose best-known chain work is behind the local tip. However, the eviction timer (`chain_sync.timeout`) is unconditionally reset whenever a peer's `best_known_header.total_difficulty()` reaches the stored `chain_sync.total_difficulty` snapshot. A malicious outbound peer can exploit this by periodically relaying a small batch of valid, already-mined main-chain headers — requiring zero PoW — to keep resetting the timer and avoid eviction indefinitely.

---

### Finding Description

The `eviction()` function in `sync/src/synchronizer/mod.rs` implements the following logic for each outbound peer:

```
if best_known_header.total_difficulty() >= local_total_difficulty {
    // peer is caught up → clear timeout
} else if timeout == 0
       || best_known_header.total_difficulty() >= chain_sync.total_difficulty {
    // reset: timeout = now + CHAIN_SYNC_TIMEOUT (12 min)
    //        chain_sync.total_difficulty = local_total_difficulty   ← snapshot updated
} else if timeout > 0 && now > timeout {
    // evict
}
``` [1](#0-0) 

The second branch resets the 12-minute eviction clock whenever the peer's `best_known_header.total_difficulty()` is ≥ the **previously snapshotted** `chain_sync.total_difficulty`. After the reset, the snapshot is advanced to the **current** local tip difficulty.

`best_known_header` is updated via `may_set_best_known_header()`, which is called from `insert_valid_header()` every time the peer sends a `SendHeaders` P2P message that passes `HeaderVerifier`. [2](#0-1) [3](#0-2) 

The attack loop:

1. Victim's local tip difficulty = **D₁**. Peer's `best_known` = **D₀ < D₁**.
2. `eviction()` sets `timeout = now + 12 min`, `chain_sync.total_difficulty = D₁`.
3. Before the 12 minutes expire, the malicious peer sends a `SendHeaders` message containing valid main-chain headers whose cumulative difficulty reaches **D₁** (but not the current tip).
4. `HeadersProcess::execute()` validates and inserts them; `may_set_best_known_header()` advances `best_known` to **D₁**.
5. Next `eviction()` call: `best_known (D₁) >= chain_sync.total_difficulty (D₁)` → **timer resets** to `now + 12 min`, snapshot advances to **D₂**.
6. Peer sends headers up to **D₂**. Repeat indefinitely. [4](#0-3) 

The headers used in each step are already-mined, publicly available main-chain headers. The attacker performs **no PoW**; it simply replays blocks it obtained from any honest node.

The `CHAIN_SYNC_TIMEOUT` constant is 12 minutes and `EVICTION_HEADERS_RESPONSE_TIME` is 2 minutes. [5](#0-4) 

The `ChainSyncState` struct that holds the mutable timer and snapshot: [6](#0-5) 

---

### Impact Explanation

A malicious peer that the victim has connected to outbound can hold that connection slot open indefinitely without ever syncing to the victim's tip. CKB nodes maintain a small fixed number of outbound connections. If an attacker controls several such peers (e.g., via Sybil nodes seeded into the peer-discovery layer), it can saturate all outbound slots, isolating the victim from honest peers. This is a prerequisite step for an eclipse attack: once isolated, the victim can be fed a stale or adversarial chain view, enabling double-spend facilitation or mining-reward manipulation against that node.

---

### Likelihood Explanation

The attack is cheap: no mining is required. The attacker only needs to:
- Be reachable as an outbound peer (achievable via DNS seed poisoning, peer-store poisoning, or simply advertising a public IP),
- Relay a batch of ≤ 2,000 valid headers every ~12 minutes.

All required headers are freely available from any honest node. The attack is fully automatable and can be sustained indefinitely at negligible cost.

---

### Recommendation

Decouple the eviction-timer reset from the peer's announced `best_known_header`. Specifically:

1. **Do not reset `chain_sync.timeout` mid-interval** when the peer's difficulty catches up to the old snapshot. Only clear the timeout when `best_known_header.total_difficulty() >= local_total_difficulty` (i.e., the peer is genuinely caught up to the current tip). Intermediate progress should not restart the full 12-minute window.
2. Alternatively, track a **monotonically advancing** high-water mark for the snapshot: once `chain_sync.total_difficulty` is set to `D`, only advance it when the peer's `best_known` exceeds the **current** local tip, not the old snapshot.
3. Consider adding a **global wall-clock deadline** per peer connection (e.g., first-seen time + N × `CHAIN_SYNC_TIMEOUT`) beyond which no further resets are permitted, analogous to the recommendation in the source report to omit the `lastUpdateTime` condition for undercollateralized positions.

---

### Proof of Concept

```
Victim local tip: block 1000, total_difficulty = D1000

t=0:   eviction() runs
         best_known(peer) = D500 < D1000
         → timeout = t+12min, chain_sync.total_difficulty = D1000

t=10min: attacker sends SendHeaders([block_501 … block_1000])
           HeadersProcess validates each header (PoW already done by honest miners)
           may_set_best_known_header(peer, D1000)

t=11min: eviction() runs
           best_known(peer) = D1000 >= chain_sync.total_difficulty(D1000)  ← RESET
           → timeout = t+12min, chain_sync.total_difficulty = D1010  (tip grew)

t=21min: attacker sends SendHeaders([block_1001 … block_1010])
           may_set_best_known_header(peer, D1010)

t=22min: eviction() runs → RESET again
           ...

[loop forever; peer never evicted; connection slot permanently occupied]
```

### Citations

**File:** sync/src/synchronizer/mod.rs (L590-614)
```rust
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

**File:** sync/src/types/mod.rs (L70-77)
```rust
#[derive(Clone, Debug, Default)]
pub struct ChainSyncState {
    pub timeout: u64,
    pub work_header: Option<core::HeaderView>,
    pub total_difficulty: Option<U256>,
    pub sent_getheaders: bool,
    headers_sync_state: HeadersSyncState,
}
```

**File:** sync/src/types/mod.rs (L873-883)
```rust
    pub fn may_set_best_known_header(&self, peer: PeerIndex, header_index: HeaderIndex) {
        if let Some(mut peer_state) = self.state.get_mut(&peer) {
            if let Some(ref known) = peer_state.best_known_header {
                if header_index.is_better_chain(known) {
                    peer_state.best_known_header = Some(header_index);
                }
            } else {
                peer_state.best_known_header = Some(header_index);
            }
        }
    }
```

**File:** sync/src/types/mod.rs (L1094-1132)
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
```

**File:** sync/src/synchronizer/headers_process.rs (L94-180)
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

        let result = self.accept_first(&headers[0]);
        match result.state {
            ValidationState::Invalid => {
                debug!(
                    "HeadersProcess accept_first result is invalid, error = {:?}, first header = {:?}",
                    result.error, headers[0]
                );
                return StatusCode::HeadersIsInvalid
                    .with_context(format!("accept first header {:?}", headers[0]));
            }
            ValidationState::TemporaryInvalid => {
                debug!(
                    "HeadersProcess accept_first result is temporary invalid, first header = {:?}",
                    headers[0]
                );
                return Status::ok();
            }
            ValidationState::Valid => {
                // Valid, do nothing
            }
        };

        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
            match result.state {
                ValidationState::Invalid => {
                    debug!(
                        "HeadersProcess accept result is invalid, error = {:?}, header = {:?}",
                        result.error, headers,
                    );
                    return StatusCode::HeadersIsInvalid
                        .with_context(format!("accept header {header:?}"));
                }
                ValidationState::TemporaryInvalid => {
                    debug!(
                        "HeadersProcess accept result is temporarily invalid, header = {:?}",
                        header
                    );
                    return Status::ok();
                }
                ValidationState::Valid => {
                    // Valid, do nothing
                }
            };
        }

```

**File:** util/constant/src/sync.rs (L38-42)
```rust
pub const CHAIN_SYNC_TIMEOUT: u64 = 12 * 60 * 1000; // 12 minutes
/// Suspend sync time
pub const SUSPEND_SYNC_TIME: u64 = 5 * 60 * 1000; // 5 minutes
/// Eviction response time
pub const EVICTION_HEADERS_RESPONSE_TIME: u64 = 120 * 1000; // 2 minutes
```
