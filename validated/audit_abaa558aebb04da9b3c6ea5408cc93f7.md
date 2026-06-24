Audit Report

## Title
Unbounded `pending_compact_blocks` Growth via Trivially-Mined Compact Blocks with Missing Transactions — (`sync/src/relayer/compact_block_process.rs`)

## Summary
An unprivileged remote peer can insert an unlimited number of entries into `pending_compact_blocks` by sending compact blocks whose headers pass all relay-time checks but whose `compact_target` is set to minimum difficulty (`DIFF_TWO = 0x2080_0000`). Because the relay-time `EpochVerifier` never validates `compact_target` against the epoch's expected difficulty, and the map has no size cap, TTL, or per-peer limit, an attacker can accumulate unbounded entries that survive for an entire epoch, causing heap exhaustion and node crash.

## Finding Description

**Root cause — `PowVerifier` uses the header's own `compact_target`**

The Eaglesong PoW engine derives the target directly from the header field being verified: [1](#0-0) 

Setting `compact_target = DIFF_TWO` (0x2080_0000) expands to a near-maximum target, so any nonce satisfies the check. This is the minimal difficulty constant defined in the codebase: [2](#0-1) 

**Relay-time `HeaderVerifier` does not check `compact_target` against epoch**

The relay-time verifier runs `PowVerifier`, `NumberVerifier`, `EpochVerifier`, and `TimestampVerifier`: [3](#0-2) 

The relay-time `EpochVerifier` only validates `is_well_formed()` and `is_successor_of(parent)` — no `compact_target` check: [4](#0-3) 

The `compact_target`-vs-epoch check exists only in the contextual `EpochVerifier`, which runs exclusively during full block acceptance: [5](#0-4) 

**`non_contextual_check` has no `compact_target` gate**

The function only checks uncle count, proposals count, and block height window — no difficulty validation: [6](#0-5) 

**Unconditional insertion into an unbounded map**

When reconstruction yields `Missing` or `Collided`, `missing_or_collided_post_process` inserts the entry unconditionally: [7](#0-6) 

The map type is a plain `HashMap` with no capacity bound, initialized with no limit: [8](#0-7) [9](#0-8) 

**Cleanup does not evict attacker entries within an epoch**

The `retain` call keeps all entries whose epoch number is ≥ the accepted block's epoch number: [10](#0-9) 

Since attacker-crafted blocks use the current epoch number, they survive every legitimate block acceptance within that epoch. Cleanup only occurs at epoch transitions (~1000 blocks, ~2.2 hours).

**Deduplication is per-(hash, peer), not per-peer**

The pending check only blocks the same block hash from the same peer: [11](#0-10) 

Different nonces produce different block hashes, so a single peer sending N distinct headers inserts N independent entries. No rate limiting or per-peer entry cap exists anywhere in the sync relay path.

## Impact Explanation

Each `pending_compact_blocks` entry stores a full `CompactBlock` (header + short IDs + prefilled transactions + proposals). With epoch_length ≈ 1000 and even a handful of attacker-controlled peers, the map accumulates thousands of entries holding megabytes of heap-allocated data. This causes unbounded heap growth that can exhaust available memory and crash the node. Additionally, `pending_compact_blocks` is protected by a `tokio::sync::Mutex`; a bloated map degrades every legitimate compact block relay that must acquire this lock.

**Impact: High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attack requires no privileged access. The only cost is mining headers against minimum difficulty (`DIFF_TWO`), which is trivially satisfiable — any nonce passes because the target is near-maximum. A single commodity machine can generate thousands of valid headers per second. The attacker only needs one known valid parent block hash (the current tip) and varies the nonce to produce arbitrarily many distinct block hashes. The attack is repeatable indefinitely and does not require victim mistakes or external context.

## Recommendation

1. **Verify `compact_target` against the epoch** inside `contextual_check`, mirroring the check in `verification/contextual/src/contextual_block_verifier.rs`'s `EpochVerifier` (lines 500–507), so headers with an anomalously easy target are rejected before insertion.
2. **Add a size cap** on `pending_compact_blocks` (e.g., reject insertion when the map exceeds a constant like 512 entries).
3. **Add a TTL sweep**: periodically evict entries older than a configurable timeout (e.g., 30 seconds) regardless of whether their transactions arrived.
4. **Add a per-peer entry limit**: track how many pending entries each peer has contributed and disconnect or ignore peers that exceed a threshold.

## Proof of Concept

```
1. Connect to a CKB node as a relay peer.
2. Obtain the current tip block hash T and its epoch info.
3. For i in 1..1000:
     a. Build a header: parent_hash=T, number=tip+1,
        compact_target=DIFF_TWO (0x2080_0000),
        epoch=successor(tip.epoch), timestamp=tip.median_time+1,
        nonce=i  (trivially satisfies PowVerifier since target is maximum).
     b. Wrap in a CompactBlock with one short_id referencing a
        transaction not in the node's mempool (guaranteed Missing).
     c. Send via the relay protocol.
4. Assert len(pending_compact_blocks) == 1000.
5. Observe that no legitimate block acceptance within the same epoch
   removes these entries (retain keeps epoch >= current epoch).
6. Repeat to exhaust node memory.
```

### Citations

**File:** pow/src/eaglesong.rs (L16-16)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());
```

**File:** util/types/src/utilities/difficulty.rs (L5-5)
```rust
pub const DIFF_TWO: u32 = 0x2080_0000;
```

**File:** verification/src/header_verifier.rs (L30-51)
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
}
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L500-507)
```rust
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
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

**File:** sync/src/relayer/compact_block_process.rs (L190-223)
```rust
fn non_contextual_check(
    compact_block: &CompactBlock,
    header: &HeaderView,
    consensus: &Consensus,
    active_chain: &ActiveChain,
) -> Status {
    if compact_block.uncles().len() > consensus.max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock uncles count({}) > consensus max_uncles_num({})",
            compact_block.uncles().len(),
            consensus.max_uncles_num()
        ));
    }
    if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "CompactBlock proposals count({}) > consensus max_block_proposals_limit({})",
            compact_block.proposals().len(),
            consensus.max_block_proposals_limit(),
        ));
    }

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
