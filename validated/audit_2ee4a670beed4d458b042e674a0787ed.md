### Title
Unbounded `pending_compact_blocks` Growth via Trivially-Mined Compact Blocks with Missing Transactions — (`sync/src/relayer/compact_block_process.rs`)

---

### Summary

An unprivileged remote peer can cause the `pending_compact_blocks` map to grow without bound by repeatedly sending compact blocks that pass header verification (using a maximally-easy `compact_target`) but always result in `ReconstructionResult::Missing`. Because the only cleanup path requires a block to be *fully reconstructed*, and there is no size cap, TTL, or per-peer entry limit on the map, an attacker can accumulate O(epoch\_length × peers) entries that are never evicted until a legitimate block is accepted.

---

### Finding Description

**Entry point — `non_contextual_check`**

The first gate checks structural limits and the height window: [1](#0-0) 

Any header whose `number` is within `[tip - epoch_length, ...]` passes. There is no check on `compact_target` here.

**Entry point — `contextual_check` / `HeaderVerifier`**

The second gate runs `HeaderVerifier::verify`, which checks PoW, number, epoch continuity, and timestamp: [2](#0-1) 

Critically, the `EpochVerifier` used here only checks that the epoch field is a *successor* of the parent's epoch — it does **not** verify that `compact_target` matches the epoch's expected difficulty: [3](#0-2) 

The compact\_target-vs-epoch check lives in `contextual_block_verifier.rs`'s `EpochVerifier` and only runs during full block acceptance, not during compact block relay. This means an attacker can freely set `compact_target = 0x207fffff` (maximum target, minimum difficulty), making PoW trivially satisfiable with any nonce.

**Insertion into `pending_compact_blocks`**

When reconstruction yields `Missing`, `missing_or_collided_post_process` inserts the entry unconditionally: [4](#0-3) 

The map type has no capacity bound: [5](#0-4) 

It is initialized as a plain `HashMap::default()`: [6](#0-5) 

**The only cleanup path**

Entries are removed only in the `ReconstructionResult::Block` branch — i.e., only when a compact block is *fully reconstructed*: [7](#0-6) 

The `shrink_to_fit!` call on line 117 only shrinks the allocation; it does not remove entries. There is no TTL, no periodic sweep, and no per-peer entry cap anywhere in the codebase.

**Deduplication is per-(hash, peer), not per-peer**

The pending check only blocks the same block hash from the same peer: [8](#0-7) 

A single peer sending N distinct headers (different nonces → different hashes, all with max `compact_target`) inserts N independent entries.

---

### Impact Explanation

Each `pending_compact_blocks` entry stores a full `CompactBlock` (header + short IDs + prefilled transactions + proposals). With `epoch_length ≈ 1000` and even a handful of attacker-controlled peers, the map can accumulate thousands of entries holding megabytes of heap-allocated data. This causes:

- **Memory exhaustion**: unbounded heap growth proportional to attacker throughput.
- **Lock contention**: `pending_compact_blocks` is protected by a `tokio::sync::Mutex`; a bloated map degrades every legitimate compact block relay that must acquire this lock.

---

### Likelihood Explanation

The attack requires no privileged access. The only real cost is mining headers against `compact_target = 0x207fffff`, which is trivially satisfiable (any nonce passes). A single commodity machine can generate thousands of valid headers per second. The attacker only needs one known valid parent block hash (the current tip suffices) and can vary the nonce to produce arbitrarily many distinct block hashes.

---

### Recommendation

1. **Add a size cap** on `pending_compact_blocks` (e.g., reject insertion when the map exceeds a constant like 512 entries).
2. **Add a TTL sweep**: periodically evict entries older than a configurable timeout (e.g., 30 seconds) regardless of whether their transactions arrived.
3. **Add a per-peer entry limit**: track how many pending entries each peer has contributed and disconnect or ignore peers that exceed a threshold.
4. **Verify `compact_target` against the epoch** inside `contextual_check`, mirroring the check in `contextual_block_verifier.rs`'s `EpochVerifier`, so headers with an anomalously easy target are rejected before insertion.

---

### Proof of Concept

```
1. Connect to a CKB node as a relay peer.
2. Obtain the current tip block hash T.
3. For i in 1..1000:
     a. Build a header: parent_hash=T, number=tip+1,
        compact_target=0x207fffff, epoch=successor(tip.epoch),
        timestamp=tip.median_time+1, nonce=i  (trivially satisfies PoW).
     b. Wrap in a CompactBlock with one short_id referencing a
        transaction not in the node's mempool (guaranteed Missing).
     c. Send via the relay protocol.
4. Assert len(pending_compact_blocks) == 1000.
5. Repeat from step 3 with a new parent or new peers.
   The map never shrinks until a legitimate block is accepted.
```

### Citations

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

**File:** sync/src/relayer/compact_block_process.rs (L214-220)
```rust
    let tip = active_chain.tip_header();
    let epoch_length = active_chain.epoch_ext().length();
    let lowest_number = tip.number().saturating_sub(epoch_length);

    if lowest_number > header.number() {
        return StatusCode::CompactBlockIsStaled.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L284-291)
```rust
    let pending_compact_blocks = shared.state().pending_compact_blocks().await;
    if pending_compact_blocks
        .get(&block_hash)
        .map(|(_, peers_map, _)| peers_map.contains_key(&peer))
        .unwrap_or(false)
    {
        return StatusCode::CompactBlockIsAlreadyPending.with_context(block_hash);
    }
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

**File:** verification/src/header_verifier.rs (L30-50)
```rust
impl<'a, DL: HeaderFieldsProvider> Verifier for HeaderVerifier<'a, DL> {
    type Target = HeaderView;
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
```

**File:** verification/src/header_verifier.rs (L123-148)
```rust
pub struct EpochVerifier<'a> {
    parent: EpochNumberWithFraction,
    header: &'a HeaderView,
}

impl<'a> EpochVerifier<'a> {
    pub fn new(parent: EpochNumberWithFraction, header: &'a HeaderView) -> Self {
        EpochVerifier { parent, header }
    }

    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

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

**File:** sync/src/types/mod.rs (L1022-1022)
```rust
            pending_compact_blocks: tokio::sync::Mutex::new(HashMap::default()),
```
