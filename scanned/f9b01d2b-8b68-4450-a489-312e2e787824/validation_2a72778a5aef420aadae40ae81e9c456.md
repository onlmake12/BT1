### Title
Unbounded `OrphanBlockPool` Growth Due to Missing Size Enforcement on Insert - (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`OrphanBlockPool` is initialized with a nominal `ORPHAN_BLOCK_SIZE` capacity, but this value is only passed as a `HashMap::with_capacity()` pre-allocation hint — not an enforced upper bound. The `insert()` path performs no size check. Any unprivileged peer can flood the node with blocks referencing unknown parent hashes, causing the orphan block pool to grow without bound and exhaust node memory.

### Finding Description

`OrphanBlockPool` is created in `chain/src/init.rs` with a constant:

```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize; // 8192
``` [1](#0-0) 

This value is passed to `OrphanBlockPool::with_capacity`, which forwards it only to `HashMap::with_capacity`:

```rust
fn with_capacity(capacity: usize) -> Self {
    InnerPool {
        blocks: HashMap::with_capacity(capacity),
        parents: HashMap::new(),
        leaders: HashSet::new(),
    }
}
``` [2](#0-1) 

`HashMap::with_capacity` is a memory pre-allocation hint — it does **not** cap the number of entries. The `insert()` method on both `OrphanBlockPool` and `InnerPool` contains no size check:

```rust
pub fn insert(&self, lonely_block: LonelyBlockHash) {
    self.inner.write().insert(lonely_block);
}
``` [3](#0-2) 

```rust
fn insert(&mut self, lonely_block: LonelyBlockHash) {
    let hash = lonely_block.hash();
    let parent_hash = lonely_block.parent_hash();
    self.blocks
        .entry(parent_hash.clone())
        .or_default()
        .insert(hash.clone(), lonely_block);
    ...
    self.parents.insert(hash, parent_hash);
}
``` [4](#0-3) 

The only eviction mechanism is `clean_expired_blocks`, which removes blocks whose epoch is more than `EXPIRED_EPOCH = 6` epochs behind the tip — a very slow, epoch-gated cleanup that provides no protection against a rapid flood. [5](#0-4) 

The attacker-controlled entry path is `OrphanBroker::process_lonely_block`. When a received block's parent is neither stored nor pending verification, the block is unconditionally inserted into the orphan pool:

```rust
} else {
    self.orphan_blocks_broker.insert(lonely_block);
}
``` [6](#0-5) 

This is the direct analog to the CLOB bug: the CLOB tree size check used `==` and failed to shrink the tree after partial removal; here, the "capacity" parameter is a HashMap hint that never enforces a limit, so the pool grows without bound.

### Impact Explanation

An unprivileged peer can craft and relay an unbounded number of syntactically valid blocks referencing random/unknown parent hashes. Each such block passes the parent-unknown branch and is inserted into `OrphanBlockPool` with no eviction. The node's memory grows without bound until OOM termination or severe degradation. This is a remote, unauthenticated DoS against any CKB full node.

### Likelihood Explanation

The attack requires only a TCP connection to the CKB sync port and the ability to send `SendBlock` messages. No key material, mining power, or privileged access is needed. The block bodies can be minimal (empty transactions, valid PoW not required for orphan admission). The attack is cheap to mount and can be sustained indefinitely.

### Recommendation

Enforce a hard cap inside `InnerPool::insert()` (or `OrphanBlockPool::insert()`). When `self.parents.len() >= capacity`, evict the oldest or a random entry before inserting the new one — mirroring the pattern used in `OrphanPool` (the tx-pool orphan pool) which correctly enforces `DEFAULT_MAX_ORPHAN_TRANSACTIONS` via `limit_size()` called on every `add_orphan_tx`. [7](#0-6) 

### Proof of Concept

1. Connect to a CKB node's sync port.
2. In a loop, construct `LonelyBlock` messages where `parent_hash` is a random 32-byte value not present in the chain or orphan pool.
3. Send each block via the `SendBlock` sync message.
4. Each block enters `process_lonely_block` → parent unknown → `orphan_blocks_broker.insert(lonely_block)` with no size check.
5. Observe `ckb_chain_orphan_count` metric growing without bound; node RSS grows proportionally until OOM. [3](#0-2) [8](#0-7)

### Citations

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

**File:** chain/src/utils/orphan_block_pool.rs (L28-34)
```rust
    fn with_capacity(capacity: usize) -> Self {
        InnerPool {
            blocks: HashMap::with_capacity(capacity),
            parents: HashMap::new(),
            leaders: HashSet::new(),
        }
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L36-54)
```rust
    fn insert(&mut self, lonely_block: LonelyBlockHash) {
        let hash = lonely_block.hash();
        let parent_hash = lonely_block.parent_hash();
        self.blocks
            .entry(parent_hash.clone())
            .or_default()
            .insert(hash.clone(), lonely_block);
        // Out-of-order insertion needs to be deduplicated
        self.leaders.remove(&hash);
        // It is a possible optimization to make the judgment in advance,
        // because the parent of the block must not be equal to its own hash,
        // so we can judge first, which may reduce one arc clone
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
        self.parents.insert(hash, parent_hash);
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L98-122)
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

**File:** chain/src/utils/orphan_block_pool.rs (L141-143)
```rust
    pub fn insert(&self, lonely_block: LonelyBlockHash) {
        self.inner.write().insert(lonely_block);
    }
```

**File:** chain/src/orphan_broker.rs (L119-123)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```
