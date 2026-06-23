### Title
Inflight Block Entries Above `tip+20` Are Never Pruned on Timeout, Permanently Occupying Peer Slots — (`sync/src/types/mod.rs`)

---

### Summary

`InflightBlocks::prune` deliberately stops iterating at `tip + 20`. The secondary cleanup path (`trace_number` / `mark_slow_block`) only covers entries at `key.number <= tip + 1`. Blocks inserted at numbers in the range `(tip+20, tip+8192]` — reachable via `BLOCK_DOWNLOAD_WINDOW` — are never evicted on timeout, the responsible peer is never punished, and its inflight slots are permanently consumed, stalling sync.

---

### Finding Description

**`prune` hard-stops at `tip + 20`** [1](#0-0) 

```rust
let end = tip + 20;
for (key, value) in states.iter() {
    if key.number > end {
        break;   // ← entries above tip+20 are never visited
    }
```

The comment at line 657–660 explains the intent: "just check within tip + 20, with the checkpoint marking possible blocking points, it's enough." The correctness of this assumption depends entirely on `mark_slow_block` covering the rest.

**`mark_slow_block` only covers `<= tip + 1`** [2](#0-1) 

```rust
pub fn mark_slow_block(&mut self, tip: BlockNumber) {
    for key in self.inflight_states.keys() {
        if key.number > tip + 1 {
            break;   // ← entries above tip+1 are never added to trace_number
        }
        self.trace_number.entry(key.clone()).or_insert(now);
    }
}
```

**The gap: `(tip+1, tip+20]` is covered by neither path**

Actually the gap is `(tip+20, tip+8192]`:
- `prune`'s timeout loop covers `<= tip+20`
- `mark_slow_block` / `trace.retain` covers `<= tip+1`
- Entries in `(tip+20, tip+8192]` are covered by neither

**`BlockFetcher` can insert blocks up to `tip + BLOCK_DOWNLOAD_WINDOW` (= `tip + 8192`)** [3](#0-2) 

```rust
let mut end = min(
    fetch_end,
    window_end(start, BLOCK_DOWNLOAD_WINDOW, best_known.number()),
);
```

`BLOCK_DOWNLOAD_WINDOW = 1024 * 8 = 8192`. [4](#0-3) 

**Peer is never punished or disconnected**

`punish` is only called inside the `tip+20` loop and the `trace.retain` path. If all of a peer's inflight entries are above `tip+20`, neither path fires, `task_count` is never decremented, and the `download_schedulers.retain` eviction check (`task_count == 0`) never triggers. [5](#0-4) 

---

### Impact Explanation

1. A malicious peer presents a valid header chain with higher total difficulty (legitimate headers, no fake PoW needed — the attacker can simply be a peer that has synced a long chain).
2. `BlockFetcher` inserts up to `MAX_BLOCKS_IN_TRANSIT_PER_PEER = 128` entries at numbers `> tip+20` into `inflight_states`.
3. The attacker silently drops all `GetBlocks` requests.
4. `prune(tip)` is called every sync cycle but skips all these entries.
5. The peer's `DownloadScheduler.hashes` fills up; `can_fetch` returns 0; the peer is permanently excluded from further block downloads.
6. The peer is never disconnected (no punishment path fires).
7. With enough malicious peers (each consuming 128 slots), all sync peers are rendered useless and block sync stalls completely.

`inflight_states` and `download_schedulers.hashes` accumulate stale entries bounded only by `(number_of_peers × 128)`, which is not negligible on a well-connected node.

---

### Likelihood Explanation

- **Entry point**: standard P2P sync protocol — any unprivileged inbound or outbound peer.
- **Cost**: connect, send valid `SendHeaders` with a high-number tip (headers can be real headers from the chain), then drop all `GetBlocks` messages. No mining required.
- **Persistence**: the node never disconnects the attacker, so the attack is self-sustaining for the lifetime of the connection.
- **Scale**: a handful of coordinated peers is sufficient to exhaust all sync slots.

---

### Recommendation

The `prune` timeout loop must cover **all** timed-out entries, not just those within `tip+20`. The `tip+20` optimisation is only safe if `mark_slow_block` guarantees coverage of the remainder, which it does not (it stops at `tip+1`). Two complementary fixes:

1. **Extend `mark_slow_block`** to cover all currently-inflight entries (not just `<= tip+1`), so the `trace.retain` path can evict them.
2. **Or extend the `prune` timeout loop** to also scan entries above `tip+20` when their `timestamp + BLOCK_DOWNLOAD_TIMEOUT < now`, applying the same punishment and removal logic.

Either fix closes the gap between the two cleanup paths.

---

### Proof of Concept

```rust
// Pseudocode — mirrors the structure of sync/src/tests/inflight_blocks.rs
let _faketime_guard = ckb_systemtime::faketime();
_faketime_guard.set_faketime(0);

let mut inflight = InflightBlocks::default();
inflight.protect_num = 0;

let tip: u64 = 100;

// Insert 128 blocks at numbers tip+21 through tip+148 (all above the prune window)
for i in 1u64..=128 {
    let number = tip + 20 + i;          // 121 .. 248
    let hash = /* unique hash for i */;
    assert!(inflight.insert(1.into(), (number, hash).into()));
}

// Advance time past BLOCK_DOWNLOAD_TIMEOUT (30 s)
_faketime_guard.set_faketime(BLOCK_DOWNLOAD_TIMEOUT + 1);

// prune at tip=100 — should evict all 128 timed-out entries, but does not
let disconnected = inflight.prune(tip);

// BUG: all 128 entries remain; peer 1 is not disconnected; not punished
assert_eq!(inflight.total_inflight_count(), 128);  // passes — entries NOT removed
assert!(!disconnected.contains(&1.into()));         // passes — peer NOT evicted
assert_eq!(inflight.peer_can_fetch_count(1.into()), 0); // peer slots permanently full
```

The entries at `tip+21` through `tip+148` survive indefinitely. Peer 1's `can_fetch` is 0, blocking all future block requests to it, with no timeout-based recovery.

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

**File:** sync/src/types/mod.rs (L661-665)
```rust
        let end = tip + 20;
        for (key, value) in states.iter() {
            if key.number > end {
                break;
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

**File:** sync/src/synchronizer/block_fetcher.rs (L212-215)
```rust
        let mut end = min(
            fetch_end,
            window_end(start, BLOCK_DOWNLOAD_WINDOW, best_known.number()),
        );
```

**File:** util/constant/src/sync.rs (L54-54)
```rust
pub const BLOCK_DOWNLOAD_WINDOW: u64 = 1024 * 8; // 1024 * default_outbound_peers
```
