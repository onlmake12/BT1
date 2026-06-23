Now I have all the code needed to fully trace the vulnerability. Let me analyze the exact logic.

### Title
IBD Header Sync Slot Held Indefinitely via `is_close_to_the_end` Flag Manipulation — (`sync/src/types/mod.rs`)

---

### Summary

An unprivileged peer can send a batch of exactly `MAX_HEADERS_LEN` (2000) valid headers whose last header's timestamp is within `HEADERS_DOWNLOAD_INSPECT_WINDOW` (2 minutes) of the current wall clock. This causes `HeadersSyncController::is_timeout` to set `is_close_to_the_end = true` and return `Some(false)` on every subsequent call — permanently — because the escape condition requires the local tip to be ~22,222 days behind the current time. The peer is never evicted, and since IBD enforces a single-peer header-sync slot, the victim node's IBD stalls indefinitely.

---

### Finding Description

**Constants** (`util/constant/src/sync.rs`): [1](#0-0) 

- `HEADERS_DOWNLOAD_INSPECT_WINDOW = 120,000 ms` (2 minutes)
- `HEADERS_DOWNLOAD_HEADERS_PER_SECOND = 1600`
- `POW_INTERVAL = 10`

**`is_timeout` logic** (`sync/src/types/mod.rs`): [2](#0-1) 

When `is_close_to_the_end = false` and `now - now_tip_ts < inspect_window`, the flag is set to `true` and `Some(false)` is returned. Once `true`, the only escape is:

```
expected_in_base_time = 1600 * 120_000 * 10 / 1000 = 1_920_000_000 ms ≈ 22,222 days
``` [3](#0-2) 

`expected_before_finished > 1_920_000_000 ms` means the local tip must be more than 22,222 days behind the current wall clock — practically impossible. So `Some(false)` is returned on every subsequent call, forever.

**`eviction()` uses the LOCAL node's `better_tip_header.timestamp()` as `now_tip_ts`**: [4](#0-3) 

`better_tip_header` is either the active chain tip or `shared_best_header`, whichever has more total difficulty: [5](#0-4) 

In IBD, `shared_best_header` has more difficulty than the active chain tip, so `better_tip_ts = shared_best_header.timestamp()`. When the attacker's valid headers are accepted, `shared_best_header` is updated to the attacker's last header timestamp (close to now).

**`headers_process.rs` does NOT call `tip_synced` when exactly `MAX_HEADERS_LEN` headers are sent**: [6](#0-5) 

When the attacker sends exactly 2000 headers, `send_getheaders_to_peer` is called (requesting more), `tip_synced` is NOT called, and the peer stays in `Started` state with its `headers_sync_controller` intact. The IBD disconnect check also does not fire (it only applies when `headers.len() != MAX_HEADERS_LEN`).

**IBD enforces a single header-sync peer**: [7](#0-6) 

`n_sync_started()` is checked atomically; if `> 0` in IBD, no other peer can start syncing. The attacker's peer holds this slot indefinitely.

---

### Impact Explanation

The victim node's IBD header sync is stalled. The attacker's peer holds the single IBD sync slot and is never evicted. No other peer can take over. The node cannot make progress downloading headers or blocks until the attacker's TCP connection drops for external reasons or the operator manually intervenes (e.g., restarting the node or banning the peer's IP). This matches the stated scope: **Low (501–2000) — IBD header sync stalled**.

---

### Likelihood Explanation

The attacker requires no special privileges and no mining capability. They only need:
1. A connection to the victim node during IBD.
2. A set of valid chain headers (from the real CKB chain) where the last header's timestamp is within 2 minutes of the current wall clock — trivially obtained by any synced CKB node.
3. To send exactly 2000 of those headers and then stop responding.

This is a low-effort, low-cost attack executable by any peer on the network.

---

### Recommendation

The `is_close_to_the_end` branch must not suppress eviction indefinitely. Two complementary fixes:

1. **Add a wall-clock deadline**: When `is_close_to_the_end = true`, track a `close_to_end_since_ts` timestamp. If the peer has not advanced `now_tip_ts` within one additional `inspect_window` of real time, declare a timeout (`Some(true)`).

2. **Decouple `now_tip_ts` from peer-supplied data**: The `better_tip_header` used in `eviction()` should reflect only the locally verified active chain tip, not `shared_best_header` (which is updated by unverified peer-supplied headers). This prevents a peer from manipulating the timeout logic by sending headers that advance `shared_best_header` without advancing the verified chain.

---

### Proof of Concept

```
// Setup: node in IBD, attacker peer selected for header sync
// Attacker sends exactly 2000 valid real-chain headers,
// last header timestamp = now - 60_000 ms (1 minute ago)

// After headers_process.execute():
//   shared_best_header.timestamp() ≈ now - 60_000
//   peer stays in Started state (tip_synced NOT called)
//   n_sync_started() == 1

// Attacker stops responding. eviction() fires every second:
//   better_tip_ts = shared_best_header.timestamp() ≈ now - 60_000
//   expected_before_finished = now - better_tip_ts ≈ 60_000 < 120_000
//   → is_close_to_the_end = true, returns Some(false)

// As time passes:
//   expected_before_finished grows, but escape requires > 1_920_000_000 ms
//   → Some(false) returned on every call, forever
//   → peer never evicted, IBD stalled indefinitely
```

### Citations

**File:** util/constant/src/sync.rs (L21-32)
```rust
pub const HEADERS_DOWNLOAD_INSPECT_WINDOW: u64 = 2 * 60 * 1000;
/// Global Average Speed
//      Expect 300 KiB/second
//          = 1600 headers/second (300*1024/192)
//          = 96000 headers/minute (1600*60)
//          = 11.11 days-in-blockchain/minute-in-reality (96000*10/60/60/24)
//      => Sync 1 year headers in blockchain will be in 32.85 minutes (365/11.11) in reality
pub const HEADERS_DOWNLOAD_HEADERS_PER_SECOND: u64 = 1600;
/// Acceptable Lowest Instantaneous Speed: 75.0 KiB/second (300/4)
pub const HEADERS_DOWNLOAD_TOLERABLE_BIAS_FOR_SINGLE_SAMPLE: u64 = 4;
/// Pow interval
pub const POW_INTERVAL: u64 = 10;
```

**File:** sync/src/types/mod.rs (L185-216)
```rust
    pub(crate) fn is_timeout(&mut self, now_tip_ts: u64, now: u64) -> Option<bool> {
        let inspect_window = HEADERS_DOWNLOAD_INSPECT_WINDOW;
        let expected_headers_per_sec = HEADERS_DOWNLOAD_HEADERS_PER_SECOND;
        let tolerable_bias = HEADERS_DOWNLOAD_TOLERABLE_BIAS_FOR_SINGLE_SAMPLE;

        let expected_before_finished = now.saturating_sub(now_tip_ts);

        trace!("headers-sync: better tip ts {}; now {}", now_tip_ts, now);

        if self.is_close_to_the_end {
            let expected_in_base_time =
                expected_headers_per_sec * inspect_window * POW_INTERVAL / 1000;
            if expected_before_finished > expected_in_base_time {
                self.started_ts = now;
                self.started_tip_ts = now_tip_ts;
                self.last_updated_ts = now;
                self.last_updated_tip_ts = now_tip_ts;
                self.is_close_to_the_end = false;
                // if the node is behind the estimated tip header too much, sync again;
                trace!(
                    "headers-sync: send GetHeaders again since we are significantly behind the tip"
                );
                None
            } else {
                // ignore timeout because the tip already almost reach the real time;
                // we can sync to the estimated tip in 1 inspect window by the slowest speed that we can accept.
                Some(false)
            }
        } else if expected_before_finished < inspect_window {
            self.is_close_to_the_end = true;
            trace!("headers-sync: ignore timeout because the tip almost reaches the real time");
            Some(false)
```

**File:** sync/src/synchronizer/mod.rs (L451-466)
```rust
    fn better_tip_header(&self) -> HeaderIndexView {
        let (header, total_difficulty) = {
            let active_chain = self.shared.active_chain();
            (
                active_chain.tip_header(),
                active_chain.total_difficulty().to_owned(),
            )
        };
        let best_known = self.shared.state().shared_best_header();
        // is_better_chain
        if total_difficulty > *best_known.total_difficulty() {
            (header, total_difficulty).into()
        } else {
            best_known
        }
    }
```

**File:** sync/src/synchronizer/mod.rs (L557-570)
```rust
            if let Some(ref mut controller) = state.headers_sync_controller {
                let better_tip_ts = better_tip_header.timestamp();
                if let Some(is_timeout) = controller.is_timeout(better_tip_ts, now) {
                    if is_timeout {
                        eviction.push(*peer);
                        continue;
                    }
                } else {
                    active_chain.send_getheaders_to_peer(
                        nc,
                        *peer,
                        better_tip_header.number_and_hash(),
                    );
                }
```

**File:** sync/src/synchronizer/mod.rs (L672-681)
```rust
            if self
                .shared()
                .state()
                .n_sync_started()
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |x| {
                    if ibd && x != 0 { None } else { Some(x + 1) }
                })
                .is_err()
            {
                break;
```

**File:** sync/src/synchronizer/headers_process.rs (L183-216)
```rust
        if headers.len() == MAX_HEADERS_LEN {
            let start = headers.last().expect("empty checked").into();
            self.active_chain
                .send_getheaders_to_peer(self.nc, self.peer, start);
        } else if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
            self.synchronizer
                .shared()
                .state()
                .tip_synced(state.value_mut());
        }

        // If we're in IBD, we want outbound peers that will serve us a useful
        // chain. Disconnect peers that are on chains with insufficient work.
        let peer_flags = self
            .synchronizer
            .peers()
            .get_flag(self.peer)
            .unwrap_or_default();
        if self.active_chain.is_initial_block_download()
            && headers.len() != MAX_HEADERS_LEN
            && (!peer_flags.is_protect && !peer_flags.is_whitelist && peer_flags.is_outbound)
        {
            debug!("Disconnect an unprotected outbound peer ({})", self.peer);
            let nc = Arc::clone(self.nc);
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move {
                    let _ignore = nc
                        .async_disconnect(self.peer, "useless outbound peer in IBD")
                        .await;
                });
        }
```
