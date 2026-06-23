### Title
Stale `pending_compact_blocks` Entries Lack Time-Based Expiry, Enabling Memory Growth and Sync Delay — (File: `sync/src/types/mod.rs`, `sync/src/relayer/compact_block_process.rs`)

---

### Summary

The `pending_compact_blocks` map in `SyncState` stores compact blocks awaiting missing transactions, recording a timestamp at insertion time. However, that timestamp is **never used for expiry**. Cleanup is only triggered epoch-by-epoch when a new block is accepted. Within the same epoch, a peer who has mined a valid block can flood the map with compact blocks that can never be reconstructed, causing unbounded in-memory accumulation and interfering with sync task scheduling.

---

### Finding Description

`SyncState` holds a `pending_compact_blocks` field typed as `PendingCompactBlockMap`:

```
HashMap<Byte32, (CompactBlock, HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>, u64)>
```

where the `u64` is a wall-clock timestamp recorded at insertion. [1](#0-0) 

Entries are inserted in `missing_or_collided_post_process` whenever a compact block cannot be reconstructed (missing transactions or short-ID collision):

```rust
.or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
``` [2](#0-1) 

The **only** cleanup path is inside `CompactBlockProcess::execute`, which fires when a new block is successfully accepted and removes entries whose epoch number is strictly less than the accepted block's epoch:

```rust
pending_compact_blocks.retain(|_, (v, _, _)| {
    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
        .number()
        >= block.epoch().number()
});
``` [3](#0-2) 

The stored timestamp is only consulted in `compare_with_pending_compact` to impose a 2-second back-off before sync creates download tasks — it is **never** used to evict stale entries:

```rust
pending.get(hash)
    .map(|(_, _, time)| now > time + 2000)
    .unwrap_or(true)
``` [4](#0-3) 

There is no periodic timer, no maximum map size, and no wall-clock TTL that would remove a `pending_compact_blocks` entry that was never resolved. The `shrink_to_fit!` call only reclaims allocator capacity, not logical entries. [5](#0-4) 

An attacker who has mined even a single valid block (e.g., on a fork, at any difficulty) can send that compact block to a victim node with a fabricated or withheld transaction list. The node passes PoW verification in `contextual_check`, attempts reconstruction, fails, and inserts the entry. The attacker can repeat this for every valid block they mine within the current epoch. All entries persist until the epoch rolls over (≈4 hours on mainnet).

The `non_contextual_check` only rejects blocks whose number is below `tip - epoch_length`, so blocks from the current epoch are accepted: [6](#0-5) 

---

### Impact Explanation

1. **Memory growth**: Each entry holds a full `CompactBlock` (up to `max_block_bytes`) plus per-peer index vectors. With no size cap and no TTL, the map grows for the entire epoch duration. On a low-difficulty network (testnet, devnet) the attacker can mine many blocks cheaply.

2. **Sync task delay**: `compare_with_pending_compact` is consulted before the synchronizer creates block-download tasks. While the 2-second window is short per entry, keeping the map non-empty for a specific block hash continuously re-triggers the delay for that hash.

The CHANGELOG entry `#3110: Fix pending compact block memory bloat on abnormal flow` confirms this class of issue was previously exploitable and that the epoch-based `retain` was introduced as a partial fix — but it does not address within-epoch accumulation.

---

### Likelihood Explanation

**Low–Medium.** The attacker must possess valid PoW for at least one block (header verification is enforced before insertion). This is not free, but it does not require majority hashpower — any miner, even one with a tiny fraction of hashrate, can mine occasional blocks on a fork and relay them as compact blocks. On testnet or any low-difficulty deployment the bar is trivially low. The attack is fully reachable via the standard relay P2P protocol without any privileged access.

---

### Recommendation

Add a time-based eviction pass over `pending_compact_blocks`. The existing timestamp field makes this straightforward:

```rust
// In the periodic maintenance tick (e.g., alongside inflight_blocks.prune):
let now = unix_time_as_millis();
const PENDING_COMPACT_TTL_MS: u64 = 30_000; // 30 seconds
pending_compact_blocks.retain(|_, (_, _, ts)| now < ts + PENDING_COMPACT_TTL_MS);
```

Additionally, enforce a hard cap on the number of entries (e.g., 32) to bound worst-case memory regardless of timing.

---

### Proof of Concept

1. Attacker mines block `B` at height `h` (any valid PoW, even on a minority fork).
2. Attacker constructs a `CompactBlock` for `B` with `short_ids` referencing transactions the victim does not have in its mempool.
3. Attacker sends the `CompactBlock` to the victim via the relay protocol.
4. Victim passes `non_contextual_check` (height is within epoch range) and `contextual_check` (PoW is valid, parent is known).
5. Reconstruction fails (`ReconstructionResult::Missing`); `missing_or_collided_post_process` inserts `(compact_block, {attacker_peer: missing_indexes}, now_ms)` into `pending_compact_blocks`.
6. Attacker never responds to `GetBlockTransactions`. The entry remains until the epoch advances.
7. Attacker repeats for each block they mine. The map grows without bound within the epoch.

### Citations

**File:** sync/src/types/mod.rs (L979-987)
```rust
// <CompactBlockHash, (CompactBlock, <PeerIndex, (Vec<TransactionsIndex>, Vec<UnclesIndex>)>, timestamp)>
pub(crate) type PendingCompactBlockMap = HashMap<
    Byte32,
    (
        packed::CompactBlock,
        HashMap<PeerIndex, (Vec<u32>, Vec<u32>)>,
        u64,
    ),
>;
```

**File:** sync/src/types/mod.rs (L1362-1370)
```rust
    pub fn compare_with_pending_compact(&self, hash: &Byte32, now: u64) -> bool {
        let pending = self.pending_compact_blocks.blocking_lock();
        // After compact block request 2s or pending is empty, sync can create tasks
        pending.is_empty()
            || pending
                .get(hash)
                .map(|(_, _, time)| now > time + 2000)
                .unwrap_or(true)
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L106-117)
```rust
                let mut pending_compact_blocks = shared.state().pending_compact_blocks().await;
                pending_compact_blocks.remove(&block_hash);
                // remove all pending request below this block epoch
                //
                // use epoch as the judgment condition because we accept
                // all block in current epoch as uncle block
                pending_compact_blocks.retain(|_, (v, _, _)| {
                    Into::<EpochNumberWithFraction>::into(v.header().as_reader().raw().epoch())
                        .number()
                        >= block.epoch().number()
                });
                shrink_to_fit!(pending_compact_blocks, 20);
```

**File:** sync/src/relayer/compact_block_process.rs (L211-222)
```rust
    // Only accept blocks with a height greater than tip - N
    // where N is the current epoch length
    let block_hash = header.hash();
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }

    Status::ok()
```

**File:** sync/src/relayer/compact_block_process.rs (L354-361)
```rust
    shared
        .state()
        .pending_compact_blocks()
        .await
        .entry(block_hash.clone())
        .or_insert_with(|| (compact_block, HashMap::default(), unix_time_as_millis()))
        .1
        .insert(peer, (missing_transactions.clone(), missing_uncles.clone()));
```
