### Title
Malicious Peer Can Occupy `InflightBlocks` Slot to Delay Block Synchronization — (`sync/src/types/mod.rs`)

### Summary

`InflightBlocks::insert` enforces a strict one-peer-per-block rule: once a block hash is registered as in-flight for any peer, no other peer can be assigned that block until the original peer's entry is pruned by timeout. A malicious peer can advertise blocks it will never deliver, occupying in-flight slots and preventing legitimate peers from being assigned those blocks for the duration of `BLOCK_DOWNLOAD_TIMEOUT`.

### Finding Description

`InflightBlocks::insert` in `sync/src/types/mod.rs` checks whether a block is already registered in `inflight_states`. If the entry is `Occupied`, it immediately returns `false` without updating the assigned peer:

```rust
pub fn insert(&mut self, peer: PeerIndex, block: BlockNumberAndHash) -> bool {
    let state = self.inflight_states.entry(block.clone());
    match state {
        Entry::Occupied(_entry) => return false,   // ← slot already taken, no reassignment
        Entry::Vacant(entry) => entry.insert(InflightState::new(peer)),
    };
    ...
}
``` [1](#0-0) 

In `block_fetcher.rs`, when `insert` returns `false`, the block is simply not added to the fetch list — it is silently skipped for the current peer:

```rust
&& state
    .write_inflight_blocks()
    .insert(self.peer, (header.number(), hash).into())
{
    fetch.push(header)   // ← only reached if insert returned true
}
``` [2](#0-1) 

A malicious peer can exploit this by:
1. Sending a `SendHeaders` message advertising a block it knows about but will not deliver.
2. The victim node calls `insert` for the malicious peer — it succeeds (slot is vacant).
3. The victim sends `GetBlocks` to the malicious peer; the malicious peer never responds.
4. When a legitimate peer also advertises the same block, `insert` returns `false` (slot is `Occupied` by the malicious peer).
5. The block is not requested from the legitimate peer until `BLOCK_DOWNLOAD_TIMEOUT` expires and `prune` clears the stale entry.

The block hash is fully known to any peer that has received the chain headers, making the target slot entirely predictable. No privileged access is required.

### Impact Explanation

Block synchronization for targeted block hashes is delayed by at least one full `BLOCK_DOWNLOAD_TIMEOUT` per malicious peer per block. An attacker controlling multiple connections (up to the node's `max_peers` limit) can stagger timeouts across different blocks, sustaining a degraded sync rate. During IBD (Initial Block Download), this can significantly slow a node's ability to catch up to the chain tip. The `prune` function does eventually clear stale entries and returns a disconnect list, but the attacker can reconnect and repeat the pattern. [3](#0-2) 

### Likelihood Explanation

Any unprivileged peer reachable by the victim node can perform this attack. No special knowledge, leaked keys, or majority hashpower is required. The attacker only needs to:
- Establish a connection to the victim node.
- Send a valid `SendHeaders` message for blocks it knows about (publicly available from the chain).
- Ignore subsequent `GetBlocks` requests.

The `DownloadScheduler` per-peer capacity limit (`MAX_BLOCKS_IN_TRANSIT_PER_PEER`) bounds how many slots one peer can occupy simultaneously, but multiple connections multiply the effect. [4](#0-3) [5](#0-4) 

### Recommendation

When `insert` returns `false` for a new peer advertising a block that is already in-flight, the node should check whether the currently assigned peer has been unresponsive for a significant fraction of `BLOCK_DOWNLOAD_TIMEOUT`. If so, the in-flight assignment should be transferred to the new peer (or the block should be requested from both peers simultaneously). Alternatively, allow a secondary peer to be registered for the same block so that if the primary peer times out, the secondary can immediately serve the request without waiting for the next `prune` cycle.

### Proof of Concept

1. Attacker peer connects to victim CKB node.
2. Attacker sends `SendHeaders` containing headers for blocks `B1, B2, ..., Bn` (all known from the public chain).
3. Victim's `BlockFetcher` calls `inflight_blocks.insert(attacker_peer, Bi)` for each `Bi` — all succeed.
4. Victim sends `GetBlocks([B1..Bn])` to attacker; attacker ignores the message.
5. Legitimate peer also advertises `B1..Bn`; victim calls `inflight_blocks.insert(legit_peer, Bi)` — all return `false` (slots occupied).
6. Victim cannot download `B1..Bn` from the legitimate peer until `BLOCK_DOWNLOAD_TIMEOUT` elapses and `prune` fires.
7. Attacker reconnects and repeats, sustaining the delay. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** sync/src/types/mod.rs (L499-506)
```rust
impl DownloadScheduler {
    fn inflight_count(&self) -> usize {
        self.hashes.len()
    }

    fn can_fetch(&self) -> usize {
        self.task_count.saturating_sub(self.hashes.len())
    }
```

**File:** sync/src/types/mod.rs (L534-543)
```rust
#[derive(Clone)]
pub struct InflightBlocks {
    pub(crate) download_schedulers: HashMap<PeerIndex, DownloadScheduler>,
    inflight_states: BTreeMap<BlockNumberAndHash, InflightState>,
    pub(crate) trace_number: HashMap<BlockNumberAndHash, u64>,
    pub(crate) restart_number: BlockNumber,
    time_analyzer: TimeAnalyzer,
    pub(crate) adjustment: bool,
    pub(crate) protect_num: usize,
}
```

**File:** sync/src/types/mod.rs (L638-686)
```rust
    pub fn prune(&mut self, tip: BlockNumber) -> HashSet<PeerIndex> {
        let now = unix_time_as_millis();
        let mut disconnect_list = HashSet::new();
        // Since statistics are currently disturbed by the processing block time, when the number
        // of transactions increases, the node will be accidentally evicted.
        //
        // Especially on machines with poor CPU performance, the node connection will be frequently
        // disconnected due to statistics.
        //
        // In order to protect the decentralization of the network and ensure the survival of low-performance
        // nodes, the penalty mechanism will be closed when the number of download nodes is less than the number of protected nodes
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let adjustment = self.adjustment;

        let trace = &mut self.trace_number;
        let download_schedulers = &mut self.download_schedulers;
        let states = &mut self.inflight_states;

        let mut remove_key = Vec::new();
        // Since this is a btreemap, with the data already sorted,
        // we don't have to worry about missing points, and we don't need to
        // iterate through all the data each time, just check within tip + 20,
        // with the checkpoint marking possible blocking points, it's enough
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

**File:** sync/src/types/mod.rs (L748-764)
```rust
    pub fn insert(&mut self, peer: PeerIndex, block: BlockNumberAndHash) -> bool {
        let state = self.inflight_states.entry(block.clone());
        match state {
            Entry::Occupied(_entry) => return false,
            Entry::Vacant(entry) => entry.insert(InflightState::new(peer)),
        };

        if self.restart_number >= block.number {
            // All new requests smaller than restart_number mean that they are cleaned up and
            // cannot be immediately marked as cleaned up again.
            self.trace_number
                .insert(block.clone(), unix_time_as_millis());
        }

        let download_scheduler = self.download_schedulers.entry(peer).or_default();
        download_scheduler.hashes.insert(block)
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L269-284)
```rust
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

**File:** sync/src/tests/inflight_blocks.rs (L11-13)
```rust
    // don't allow 2 peer for one block
    assert!(inflight_blocks.insert(2.into(), (1, h256!("0x1").into()).into()));
    assert!(!inflight_blocks.insert(1.into(), (1, h256!("0x1").into()).into()));
```
