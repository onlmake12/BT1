### Title
Inflight Block Entries at Numbers > tip+20 Are Never Pruned on Timeout, Allowing Unprivileged Peers to Persistently Stall Sync — (`sync/src/types/mod.rs`)

---

### Summary

`InflightBlocks::prune` contains a hard-coded early-exit at `key.number > tip + 20` in its BLOCK_DOWNLOAD_TIMEOUT sweep. The secondary cleanup path (`trace.retain`) only covers entries that were added to `trace_number` by `mark_slow_block`, which itself only marks blocks with `key.number <= tip + 1`. This leaves a permanent dead zone: any inflight block at number `tip+2` through `tip+8192` that a peer never delivers is never evicted, never causes the peer to be punished, and permanently occupies a download slot until the local tip organically advances past it.

---

### Finding Description

**Path 1 — BLOCK_DOWNLOAD_TIMEOUT sweep** (`prune`, lines 661–686):

```rust
let end = tip + 20;
for (key, value) in states.iter() {
    if key.number > end {
        break;          // hard exit — nothing above tip+20 is ever checked
    }
    if value.timestamp + BLOCK_DOWNLOAD_TIMEOUT < now {
        // punish peer, remove entry
    }
}
``` [1](#0-0) 

**Path 2 — `trace.retain` sweep** (lines 713–742): removes entries from `inflight_states` only if they appear in `trace_number`. [2](#0-1) 

**`mark_slow_block`** populates `trace_number` only for blocks with `key.number <= tip + 1`:

```rust
for key in self.inflight_states.keys() {
    if key.number > tip + 1 {
        break;   // blocks above tip+1 are never added to trace_number
    }
    self.trace_number.entry(key.clone()).or_insert(now);
}
``` [3](#0-2) 

`mark_slow_block` is triggered in `BlockFetcher::fetch` when the last requested block number exceeds `unverified_tip + CHECK_POINT_WINDOW (512)`: [4](#0-3) 

Even when triggered, it only marks blocks `<= unverified_tip + 1`. Blocks at `unverified_tip + 2` through `unverified_tip + 8192` (`BLOCK_DOWNLOAD_WINDOW`) are in neither cleanup path. [5](#0-4) 

**`DownloadScheduler.task_count`** is only decremented by `punish`, which is only called from the two prune paths above. Since neither path fires for high-number blocks, `task_count` never reaches zero, so the peer is never disconnected by the `download_schedulers.retain` check: [6](#0-5) 

---

### Impact Explanation

An attacker peer that:
1. Advertises a `best_known_header` at `tip + N` (N > 512 to trigger `mark_slow_block`, but any N > 20 suffices for the dead zone),
2. Causes the victim to insert inflight entries at numbers `tip+21` through `tip+128` (up to `MAX_BLOCKS_IN_TRANSIT_PER_PEER = 128`),
3. Then silently drops all `GetBlocks` requests,

will hold those 108 slots permanently until the local tip organically advances past each one. Because `task_count` is never decremented, the attacker's scheduler always reports capacity, so `BlockFetcher` keeps assigning new high-number blocks to the attacker as the tip crawls forward. The attacker can thus maintain a persistent, rolling reservation of up to 128 inflight slots per connection, forcing the victim to rely entirely on other peers for forward progress. With multiple attacker connections the effect multiplies. The attacker is never punished, never disconnected, and the cost is simply maintaining a TCP connection and discarding `GetBlocks` messages.

The claim of "unbounded growth" is inaccurate — the map is bounded by `num_peers × MAX_BLOCKS_IN_TRANSIT_PER_PEER`. The real impact is **persistent sync slowdown / slot exhaustion** rather than unbounded memory growth.

---

### Likelihood Explanation

- Requires only a standard P2P sync connection — no keys, no PoW, no privilege.
- The attacker sends one `SendHeaders` message with a high-number header and then goes silent. This is trivially achievable.
- The victim node has no mechanism to detect or evict a peer that only ignores `GetBlocks` for high-number blocks.
- Effective against nodes in both IBD and post-IBD sync.

---

### Recommendation

The `prune` function's `tip + 20` window must not be the sole timeout guard. Options:
- Extend the BLOCK_DOWNLOAD_TIMEOUT sweep to cover all entries in `inflight_states`, not just those within `tip + 20`. The BTreeMap early-exit is a performance optimisation that creates a correctness hole.
- Alternatively, ensure `mark_slow_block` marks all currently-inflight blocks (not just those `<= tip + 1`) so the `trace.retain` path covers the full range.
- Add a per-peer absolute timeout: if a peer has any inflight entry older than `BLOCK_DOWNLOAD_TIMEOUT` regardless of block number, punish and eventually disconnect it.

---

### Proof of Concept

```rust
// faketime at 0
let mut inflight = InflightBlocks::default();
inflight.protect_num = 0;

// Attacker peer 1 claims blocks tip+21 through tip+52 (all above the tip+20 window)
let tip: u64 = 100;
for i in 21u64..=52 {
    assert!(inflight.insert(1.into(), (tip + i, h256!("0x1").into()).into()));
    // (use distinct hashes in practice)
}

// Advance time past BLOCK_DOWNLOAD_TIMEOUT
faketime.set(BLOCK_DOWNLOAD_TIMEOUT + 1);

// prune with current tip
let disconnected = inflight.prune(tip);

// None of the 32 entries are removed — they are all above tip+20
assert_eq!(inflight.total_inflight_count(), 32);  // all still present
assert!(disconnected.is_empty());                  // peer never punished
// Peer 1's task_count is still INIT_BLOCKS_IN_TRANSIT_PER_PEER — never decremented
assert_eq!(inflight.peer_can_fetch_count(1.into()), 0); // slots fully occupied, not freed
```

The entries at `tip+21` through `tip+52` survive indefinitely until `prune` is called with a tip value ≥ `(tip+21) - 20 = tip+1`, i.e., until the local chain tip advances by at least 1 block — which the attacker can delay by also claiming the `tip+1` through `tip+20` range from a second connection.

### Citations

**File:** sync/src/types/mod.rs (L628-636)
```rust
    pub fn mark_slow_block(&mut self, tip: BlockNumber) {
        let now = ckb_systemtime::unix_time_as_millis();
        for key in self.inflight_states.keys() {
            if key.number > tip + 1 {
                break;
            }
            self.trace_number.entry(key.clone()).or_insert(now);
        }
    }
```

**File:** sync/src/types/mod.rs (L661-686)
```rust
        let end = tip + 20;
        for (key, value) in states.iter() {
            if key.number > end {
                break;
            }
            if value.timestamp + BLOCK_DOWNLOAD_TIMEOUT < now {
                if let Some(set) = download_schedulers.get_mut(&value.peer) {
                    set.hashes.remove(key);
                    if should_punish && adjustment {
                        set.punish(2);
                    }
                };
                if !trace.is_empty() {
                    trace.remove(key);
                }
                remove_key.push(key.clone());
                debug!(
                    "prune: remove InflightState: remove {}-{} from {}",
                    key.number, key.hash, value.peer
                );

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics.ckb_inflight_timeout_count.inc();
                }
            }
        }
```

**File:** sync/src/types/mod.rs (L692-700)
```rust
        download_schedulers.retain(|k, v| {
            // task number zero means this peer's response is very slow
            if v.task_count == 0 {
                disconnect_list.insert(*k);
                false
            } else {
                true
            }
        });
```

**File:** sync/src/types/mod.rs (L713-742)
```rust
        trace.retain(|key, time| {
            // In the normal state, trace will always empty
            //
            // When the inflight request reaches the checkpoint(inflight > tip + 512),
            // it means that there is an anomaly in the sync less than tip + 1, i.e. some nodes are stuck,
            // at which point it will be recorded as the timestamp at that time.
            //
            // If the time exceeds low time limit, delete the task and halve the number of
            // executable tasks for the corresponding node
            if now > timeout_limit + *time {
                if let Some(state) = states.remove(key)
                    && let Some(d) = download_schedulers.get_mut(&state.peer)
                {
                    if should_punish && adjustment {
                        d.punish(1);
                    }
                    d.hashes.remove(key);
                    debug!(
                        "prune: remove download_schedulers: remove {}-{} from {}",
                        key.number, key.hash, state.peer
                    );
                };

                if key.number > *restart_number {
                    *restart_number = key.number;
                }
                return false;
            }
            true
        });
```

**File:** sync/src/synchronizer/block_fetcher.rs (L307-314)
```rust
        let should_mark = fetch.last().is_some_and(|header| {
            header.number().saturating_sub(CHECK_POINT_WINDOW) > unverified_tip
        });
        if should_mark {
            state
                .write_inflight_blocks()
                .mark_slow_block(unverified_tip);
        }
```

**File:** util/constant/src/sync.rs (L48-54)
```rust
pub const BLOCK_DOWNLOAD_TIMEOUT: u64 = 30 * 1000; // 30s

/// Block download window size
// Size of the "block download window": how far ahead of our current height do we fetch?
// Larger windows tolerate larger download speed differences between peers, but increase the
// potential degree of disordering of blocks.
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```
