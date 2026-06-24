Audit Report

## Title
`InnerPool::need_clean` samples only one HashMap child, allowing permanent suppression of orphan-pool cleanup — (`chain/src/utils/orphan_block_pool.rs`)

## Summary

`InnerPool::need_clean` decides whether to evict an entire parent group by inspecting exactly one child block chosen by non-deterministic `HashMap::iter().next()`. Because `non_contextual_verify` performs no PoW or epoch validation, any P2P peer can insert a "shield" block with a recent epoch alongside an expired-epoch sibling under the same fake parent hash. Whenever the shield block is iterated first, `need_clean` returns `false` and neither block is ever evicted, causing unbounded memory growth in the orphan pool and eventual OOM crash of the node.

## Finding Description

`InnerPool::need_clean` in `chain/src/utils/orphan_block_pool.rs` (lines 113–122) calls `map.iter().next()` on a `HashMap<Byte32, LonelyBlockHash>`, returning one arbitrary entry:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← only first entry
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
``` [1](#0-0) 

`clean_expired_blocks` calls `need_clean` per leader and only removes the group if it returns `true`: [2](#0-1) 

The precondition — inserting a block with an arbitrary epoch — is trivially reachable. `asynchronous_process_block` calls `non_contextual_verify` before inserting into the orphan pool: [3](#0-2) 

`non_contextual_verify` invokes only `BlockVerifier` and `NonContextualBlockTxsVerifier`: [4](#0-3) 

`BlockVerifier::verify` runs `BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, and `MerkleRootVerifier` — **no PoW, no epoch check**: [5](#0-4) 

PoW and epoch validation live in `HeaderVerifier`, which requires the parent block and is never called on this path: [6](#0-5) 

The `epoch_number` stored in `LonelyBlockHash` is taken directly from the block header field without any validation: [7](#0-6) 

**Exploit flow:**
1. Attacker sends two `SendBlock` messages with the same fake `parent_hash`:
   - Block A: `epoch = 1` (expired when `tip_epoch = 20`, since `1 + 6 < 20`)
   - Block B: `epoch = 19` (recent, since `19 + 6 ≥ 20`)
2. Both pass `non_contextual_verify` (no PoW or epoch check).
3. Both are written to DB and inserted into the orphan pool.
4. When `clean_expired_blocks(20)` fires, `need_clean` samples one child via `map.iter().next()`.
5. If Block B is returned first (~50% probability), `need_clean` returns `false` and neither block is evicted.
6. Attacker repeats with many distinct fake parent hashes; ~50% of groups survive each 60-second cleanup pass permanently.

The block orphan pool has no hard eviction cap (unlike the tx-orphan pool which enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` via `limit_size()`). The pool is initialized with `ORPHAN_BLOCK_SIZE` as initial HashMap capacity only, not as a hard limit: [8](#0-7) 

The cleanup timer fires every 60 seconds: [9](#0-8) 

## Impact Explanation

The orphan block pool grows without bound. An attacker sending a steady stream of (expired, shield) block pairs under distinct fake parent hashes causes monotonically increasing RSS. With no hard cap and a broken cleanup path, the node eventually exhausts available memory and crashes. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**, and also **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since the attack requires no PoW and only two small crafted blocks per fake parent hash.

## Likelihood Explanation

- No PoW required; any P2P peer can send arbitrary `SendBlock` messages with spoofed epoch fields.
- No privileged role needed.
- The attack is cheap: two small crafted blocks per fake parent hash.
- The non-determinism favors the attacker: on average 50% of inserted groups escape cleanup permanently per pass.
- The 60-second cleanup interval means a modest send rate suffices to outpace cleanup.

## Recommendation

Replace the single-sample check with a check over **all** children, using the minimum epoch:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            map.values().any(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
        })
        .unwrap_or_default()
}
```

Additionally, add PoW verification (via `PowVerifier` from `HeaderVerifier`) to `non_contextual_verify` so that epoch-spoofed blocks are rejected at the network boundary before reaching the orphan pool. A hard eviction cap on the block orphan pool (analogous to `DEFAULT_MAX_ORPHAN_TRANSACTIONS` in the tx-orphan pool) should also be added as defense-in-depth.

## Proof of Concept

```rust
// Minimal unit test — insert two children under the same fake parent,
// one expired, one recent; assert the expired one is cleaned.
let pool = OrphanBlockPool::with_capacity(10);
let fake_parent = random_byte32();

// expired: epoch 1, tip will be 20 (1 + 6 < 20)
let expired = make_lonely_block_hash(fake_parent.clone(), epoch = 1);
// shield: epoch 19 (19 + 6 >= 20, not expired)
let shield  = make_lonely_block_hash(fake_parent.clone(), epoch = 19);

pool.insert(expired);
pool.insert(shield);

let removed = pool.clean_expired_blocks(20);

// BUG: if shield is iterated first by HashMap, removed.len() == 0
// The expired block is never cleaned.
// Run repeatedly — fails ~50% of the time.
assert_eq!(removed.len(), 1);
```

To confirm the insertion precondition, send a crafted `SendBlock` P2P message containing a block with `epoch = 1` and an arbitrary (non-PoW-valid) nonce to a live node and verify it is accepted by `non_contextual_verify` and inserted into the orphan pool (observable via `pool.len()` metric or debug logs).

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L98-110)
```rust
    /// cleanup expired blocks(epoch + EXPIRED_EPOCH < tip_epoch)
    pub fn clean_expired_blocks(&mut self, tip_epoch: EpochNumber) -> Vec<LonelyBlockHash> {
        let mut result = vec![];

        for hash in self.leaders.clone().iter() {
            if self.need_clean(hash, tip_epoch) {
                // remove items in orphan pool and return hash to callee(clean header map)
                let descendants = self.remove_blocks_by_parent(hash);
                result.extend(descendants);
            }
        }
        result
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L113-122)
```rust
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

**File:** chain/src/chain_service.rs (L72-89)
```rust
    fn non_contextual_verify(&self, block: &BlockView) -> Result<(), Error> {
        let consensus = self.shared.consensus();
        BlockVerifier::new(consensus).verify(block).map_err(|e| {
            debug!("[process_block] BlockVerifier error {:?}", e);
            e
        })?;

        NonContextualBlockTxsVerifier::new(consensus)
            .verify(block)
            .map_err(|e| {
                debug!(
                    "[process_block] NonContextualBlockTxsVerifier error {:?}",
                    e
                );
                e
            })
            .map(|_| ())
    }
```

**File:** chain/src/chain_service.rs (L117-143)
```rust
        if lonely_block.switch().is_none()
            || matches!(lonely_block.switch(), Some(switch) if !switch.disable_non_contextual())
        {
            let result = self.non_contextual_verify(lonely_block.block());
            if let Err(err) = result {
                error!(
                    "block {}-{} verify failed: {:?}",
                    block_number, block_hash, err
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
                return;
            }
        }

        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
        }

        self.orphan_broker.process_lonely_block(lonely_block.into());
```

**File:** verification/src/block_verifier.rs (L36-48)
```rust
impl<'a> Verifier for BlockVerifier<'a> {
    type Target = BlockView;

    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
    }
}
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

**File:** chain/src/lib.rs (L97-106)
```rust
        let epoch_number: EpochNumber = block.epoch().number();

        LonelyBlockHash {
            block_number_and_hash: BlockNumberAndHash {
                number: block_number,
                hash: block_hash,
            },
            parent_hash,
            epoch_number,
            switch,
```

**File:** chain/src/init.rs (L22-43)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;

/// Here we distinguish between build_chain_services and start_chain_services:
/// * build_chain_services simply initializes ChainController, setting up all relevant
///   threads, and return join handle for the main chain service thread.
/// * start_chain_services first builds relevant data just like build_chain_services,
///   in addition, it register the main chain service thread against CKB's handler. As
///   a result, start_chain_services only returns ChainController, it is expected that
///   CKB's stop handler shall be used to terminate the created chain service.
pub fn start_chain_services(builder: ChainServicesBuilder) -> ChainController {
    let (chain_service, chain_service_thread) = build_chain_services(builder);
    register_thread("ChainService", chain_service_thread);

    chain_service
}

/// Please refer to +start_chain_services+ for difference between build_chain_services
/// and start_chain_services
pub fn build_chain_services(
    builder: ChainServicesBuilder,
) -> (ChainController, thread::JoinHandle<()>) {
    let orphan_blocks_broker = Arc::new(OrphanBlockPool::with_capacity(ORPHAN_BLOCK_SIZE));
```
