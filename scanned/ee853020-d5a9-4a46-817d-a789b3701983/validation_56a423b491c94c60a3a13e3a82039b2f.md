Audit Report

## Title
Remote Peer Can Inflate `task_count` to 128 and Lock Block Slots in `InflightBlocks` for Up to 30 Seconds — (`sync/src/types/mod.rs`)

## Summary

An unprivileged remote sync peer can inflate its `DownloadScheduler::task_count` to `MAX_BLOCKS_IN_TRANSIT_PER_PEER=128` by responding to block requests faster than `fast_time` (default 1000 ms, not 750 ms as stated in the report — a minor inaccuracy that does not affect exploitability), then stop responding while staying connected. This locks up to 128 `BlockNumberAndHash` slots in `InflightBlocks::inflight_states`, preventing any other peer from being asked for those same blocks for up to 30 seconds per the `BLOCK_DOWNLOAD_TIMEOUT`. The core code behavior is confirmed by direct inspection.

## Finding Description

**Inflation path — verified:**

`remove_by_block` is called each time a block is received. At lines 797–800 of `sync/src/types/mod.rs`, when `time_analyzer.push_time(elapsed)` returns `TimeQuantile::MinToFast` (elapsed ≤ `fast_time`, default 1000 ms), `set.increase(2)` is called unconditionally — no `should_punish` guard:

```rust
TimeQuantile::MinToFast => set.increase(2),
TimeQuantile::FastToNormal => set.increase(1),
TimeQuantile::NormalToUpper => {
    if should_punish { set.decrease(1) }
}
```

`increase(2)` adds 2 to `task_count` up to `MAX_BLOCKS_IN_TRANSIT_PER_PEER=128`. Starting from `INIT_BLOCKS_IN_TRANSIT_PER_PEER=32`, exactly 48 fast-responded blocks are needed: (128 − 32) / 2 = 48.

**Slot locking — verified:**

`InflightBlocks::insert` at lines 748–753 uses a global `BTreeMap<BlockNumberAndHash, InflightState>`. If the key is already present, it returns `false` immediately:

```rust
Entry::Occupied(_entry) => return false,
```

`BlockFetcher::fetch` at lines 271–275 only adds a block to the fetch list if `insert` returns `true`. A `false` return silently skips the block — no fallback to another peer.

**Timeout — verified:**

`prune` at line 666 only evicts entries where `value.timestamp + BLOCK_DOWNLOAD_TIMEOUT < now`. `BLOCK_DOWNLOAD_TIMEOUT = 30 * 1000` ms (30 seconds). Additionally, `prune` only iterates up to `tip + 20` (line 661–664), meaning blocks locked beyond that range are not pruned until the tip advances — which it cannot do if those blocks are locked. This makes the effective stall potentially longer than 30 seconds for blocks above `tip + 20`.

**`can_fetch` exhaustion — verified:**

`can_fetch` at lines 504–506 returns `task_count.saturating_sub(hashes.len())`. With `task_count=128` and 128 hashes inflight, `can_fetch=0`. `BlockFetcher::fetch` at line 219–221 caps `n_fetch` to `peer_can_fetch_count`, so no further requests are issued to this peer either.

**Existing mitigations reviewed and found insufficient:**

- `should_punish` guards only the decrease/punish branches, not the increase branch.
- `remove_by_peer` clears inflight blocks on disconnect, but the attacker need not disconnect — staying connected while not responding is sufficient to hold the slots.
- The `protect_num` guard (lines 649, 786) suppresses punishment when fewer than `MAX_OUTBOUND_PEERS_TO_PROTECT_FROM_DISCONNECT=4` peers are connected, making the attack easier in low-peer-count scenarios.

## Impact Explanation

Block synchronization for up to 128 specific block hashes stalls for at least 30 seconds (and potentially longer for blocks above `tip + 20`). Because the attacker must serve valid blocks on the same chain during the inflation phase, the locked hashes are exactly the ones honest peers would be asked for. Multiple coordinated malicious peers can cover disjoint block ranges, amplifying the stall across the victim's entire sync window. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** — High severity (10001–15000 points).

## Likelihood Explanation

The attacker requires only a standard P2P sync connection and the ability to respond to 48 block requests within 1000 ms each (the actual default `fast_time`, not 750 ms as stated in the report). This is trivially achievable on any low-latency or local network. No PoW, key material, or privileged access is required. The attack is repeatable: after the 30-second timeout expires, the attacker can reconnect and repeat. The `time_analyzer` is shared across all peers, so a single fast attacker can also shift the `fast_time` baseline downward over time, making the threshold easier to beat for subsequent rounds.

## Recommendation

1. Add a `should_punish`-style guard or a per-checkpoint cap on `task_count` increases, preventing a single peer from reaching `MAX_BLOCKS_IN_TRANSIT_PER_PEER` without sustained good behavior across multiple checkpoint windows.
2. Allow re-requesting a block from a different peer after a shorter unresponsiveness interval (e.g., `low_time`) rather than waiting the full `BLOCK_DOWNLOAD_TIMEOUT=30s`.
3. Make `time_analyzer` per-peer rather than global to prevent one fast peer from skewing quantile thresholds for all peers.
4. Extend `prune` to cover the full inflight range (not just `tip + 20`) so that timed-out entries beyond that range are also evicted.

## Proof of Concept

1. Connect a malicious peer to the victim node and advertise a `best_known_header` ahead of the victim's tip.
2. Respond to every `GetBlocks` request within <1000 ms with valid blocks. After 48 responses, `task_count` reaches 128 (verified: `INIT=32`, `increase(2)` × 48 = 96, `32+96=128`).
3. Once the victim has 128 blocks inflight to the malicious peer, stop responding while remaining connected.
4. Observe that `peer_can_fetch_count` returns 0 for the malicious peer and that `InflightBlocks::insert` returns `false` for all 128 locked hashes, preventing honest peers from being asked for those blocks.
5. After 30 seconds, `prune` evicts entries within `tip + 20`; entries beyond that range remain locked until the tip advances.
6. A unit test can be written against `InflightBlocks` directly: insert 128 entries for a peer, call `insert` for the same hashes from a second peer index, assert all return `false`, then assert they remain `false` until `unix_time_as_millis()` advances past `timestamp + BLOCK_DOWNLOAD_TIMEOUT`.