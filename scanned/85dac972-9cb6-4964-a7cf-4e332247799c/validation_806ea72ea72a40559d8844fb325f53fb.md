Now I have all the evidence needed. Let me trace the full attack path precisely.

### Title
Orphan Pool Memory Exhaustion via `need_clean` Single-Child Sampling on Non-Deterministic HashMap — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`need_clean` samples only the first entry from a `HashMap<packed::Byte32, LonelyBlockHash>` via `map.iter().next()` to decide whether an entire sibling group should be evicted. Because Rust's `HashMap` iteration order is non-deterministic across process restarts (random seed), a remote peer can insert a single crafted orphan block with a far-future `epoch_number` and a trivially-easy `compact_target` to permanently prevent that sibling group — including legitimately expired blocks — from ever being cleaned. The orphan pool has no hard block count cap, so the pool grows without bound, enabling memory exhaustion.

### Finding Description

**Root cause — `need_clean` samples one child:** [1](#0-0) 

`map.iter().next()` returns an arbitrary child of the leader group. If that child's `epoch_number + EXPIRED_EPOCH >= tip_epoch`, the function returns `false` and `clean_expired_blocks` skips the entire group — including any siblings whose epoch is genuinely expired.

**`epoch_number` is taken verbatim from the attacker-supplied block header:** [2](#0-1) 

**Epoch correctness is only checked in contextual verification, which never runs for orphan blocks:** [3](#0-2) 

Orphan blocks (parent unknown) are inserted into the pool after only `non_contextual_verify` passes. `EpochVerifier` — which would reject a mismatched epoch number — is part of contextual verification and is never reached for orphan blocks.

**PoW is trivially satisfiable because `compact_target` is also attacker-controlled and only validated contextually:** [4](#0-3) 

The PoW engine checks `hash <= compact_to_target(header.compact_target())`. The attacker sets `compact_target = 0x207fffff` (maximum target, minimum difficulty). The contextual `EpochVerifier` that would reject a mismatched `compact_target` never runs for orphan blocks, so the attacker can mine a valid orphan block on a single CPU core in milliseconds.

**The timer that calls cleanup fires every 60 seconds:** [5](#0-4) 

**The orphan pool has no hard block count limit** (unlike the tx orphan pool which enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS`): [6](#0-5) 

### Impact Explanation

An attacker with a CPU (no significant mining power required) can:

1. Craft block B1: `parent_hash = P` (unknown to victim), `epoch_number = tip_epoch + 100`, `compact_target = 0x207fffff`. Mine trivially. Send via P2P.
2. Craft block B2: same `parent_hash = P`, `epoch_number = tip_epoch - 10` (expired). Mine trivially. Send via P2P.
3. Both pass non-contextual verification and enter the orphan pool under leader `P`.
4. Every 60 s, `clean_expired_orphans` calls `need_clean(P, tip_epoch)`. If `map.iter().next()` returns B1 first, the check evaluates `(tip_epoch+100)+6 < tip_epoch` → `false` → neither block is ever cleaned.
5. Repeat with fresh unknown parent hashes. The orphan pool grows without bound.

Even without the sibling trick, a single block with `epoch_number = u64::MAX - 5` is **never** cleaned because `need_clean` will always return `false` for it. The attacker can flood the pool at CPU speed.

### Likelihood Explanation

- No mining power beyond a single CPU core is required.
- No privileged access, leaked keys, or social engineering is needed.
- The attack is reachable via the standard P2P block-relay path (`Synchronizer::received` → `asynchronous_process_block` → `process_lonely_block` → `orphan_blocks_broker.insert`).
- The HashMap ordering is fixed for the lifetime of the process (random seed at startup), so once the "shield" block wins the ordering lottery, it shields its siblings permanently until the process restarts.

### Recommendation

1. **Fix `need_clean` to check all children**, not just the first: return `true` if **any** child satisfies `epoch_number + EXPIRED_EPOCH < tip_epoch`, or better, evict individual expired children rather than the whole group.
2. **Enforce a hard cap on orphan block pool size** and evict by oldest epoch when the cap is reached, analogous to `DEFAULT_MAX_ORPHAN_TRANSACTIONS` in the tx-pool.
3. **Add a minimum `compact_target` check in non-contextual verification** to reject blocks whose declared difficulty is implausibly low relative to the genesis target, preventing trivially-mined orphan spam.

### Proof of Concept

```rust
// Pseudocode — mirrors the existing test in chain/src/tests/orphan_block_pool.rs
let pool = OrphanBlockPool::with_capacity(10);
let tip_epoch = 20_u64;
let unknown_parent = random_byte32();

// B1: far-future epoch, trivially mined (compact_target = 0x207fffff)
let b1 = make_orphan(unknown_parent, epoch = tip_epoch + 100, compact_target = 0x207fffff);
// B2: expired epoch, also trivially mined
let b2 = make_orphan(unknown_parent, epoch = tip_epoch - 10, compact_target = 0x207fffff);

pool.insert(b1.into());
pool.insert(b2.into());

let cleaned = pool.clean_expired_blocks(tip_epoch);
// Depending on HashMap iteration order, cleaned.len() may be 0 (b1 first)
// or 2 (b2 first). The invariant "all blocks with epoch+6 < tip_epoch must
// be cleaned" is violated when b1 is iterated first.
assert_eq!(cleaned.len(), 2); // This assertion is NOT guaranteed to hold.
```

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L15-25)
```rust
#[derive(Default)]
struct InnerPool {
    // Group by blocks in the pool by the parent hash.
    blocks: HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>,
    // The map tells the parent hash when given the hash of a block in the pool.
    //
    // The block is in the orphan pool if and only if the block hash exists as a key in this map.
    parents: HashMap<packed::Byte32, ParentHash>,
    // Leaders are blocks not in the orphan pool but having at least a child in the pool.
    leaders: HashSet<ParentHash>,
}
```

**File:** chain/src/utils/orphan_block_pool.rs (L112-122)
```rust
    /// get 1st block belongs to that parent and check if it's expired block
    fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
        self.blocks
            .get(parent_hash)
            .and_then(|map| {
                map.iter().next().map(|(_, lonely_block)| {
                    lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
                })
            })
            .unwrap_or_default()
    }
```

**File:** chain/src/lib.rs (L97-97)
```rust
        let epoch_number: EpochNumber = block.epoch().number();
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L488-509)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let header = self.block.header();
        let actual_epoch_with_fraction = header.epoch();
        let block_number = header.number();
        let epoch_with_fraction = self.epoch.number_with_fraction(block_number);
        if actual_epoch_with_fraction != epoch_with_fraction {
            return Err(EpochError::NumberMismatch {
                expected: epoch_with_fraction.full_value(),
                actual: actual_epoch_with_fraction.full_value(),
            }
            .into());
        }
        let actual_compact_target = header.compact_target();
        if self.epoch.compact_target() != actual_compact_target {
            return Err(EpochError::TargetMismatch {
                expected: self.epoch.compact_target(),
                actual: actual_compact_target,
            }
            .into());
        }
        Ok(())
    }
```

**File:** pow/src/eaglesong.rs (L16-23)
```rust
        let (block_target, overflow) = compact_to_target(header.raw().compact_target().into());

        if block_target.is_zero() || overflow {
            debug!(
                "compact_target is invalid: {:#x}",
                header.raw().compact_target()
            );
            return false;
```

**File:** chain/src/chain_service.rs (L40-63)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));

        loop {
            select! {
                recv(self.process_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: lonely_block }) => {
                        // asynchronous_process_block doesn't interact with tx-pool,
                        // no need to pause tx-pool's chunk_process here.
                        let _trace_now = minstant::Instant::now();
                        self.asynchronous_process_block(lonely_block);
                        if let Some(handle) = ckb_metrics::handle(){
                            handle.ckb_chain_async_process_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }
                        let _ = responder.send(());
                    },
                    _ => {
                        error!("process_block_receiver closed");
                        break;
                    },
                },
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```
