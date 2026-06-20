### Title
Unbounded `block_status_map` Growth via PoW-Free Cascading `BLOCK_INVALID` Insertion - (`sync/src/synchronizer/headers_process.rs`)

### Summary

In `HeaderAcceptor::accept`, when a header's parent is already marked `BLOCK_INVALID`, the header is immediately inserted into `block_status_map` as `BLOCK_INVALID` **without performing any PoW verification**. Because `block_status_map` has no size cap and `BLOCK_INVALID` entries inserted at the header-sync layer are never evicted, an unprivileged peer can cause unbounded memory growth by exploiting this cascading path after a single initial PoW investment.

### Finding Description

**Root cause — `prev_block_check` bypasses PoW before inserting into `block_status_map`**

`HeaderAcceptor::accept` in `sync/src/synchronizer/headers_process.rs` runs three sequential checks: [1](#0-0) 

`prev_block_check` (lines 244–253) only tests whether the parent hash is already `BLOCK_INVALID` in the map. If it is, the function returns `Err(())` immediately — **before** `non_contextual_check` (which contains the PoW verifier) is ever called. The caller then unconditionally inserts the current header's hash as `BLOCK_INVALID`: [2](#0-1) 

**The collection — `block_status_map` has no size limit**

`block_status_map` is a bare `DashMap<Byte32, BlockStatus>` with no capacity bound: [3](#0-2) 

`insert_block_status` performs a plain `DashMap::insert` with no guard: [4](#0-3) 

**`BLOCK_INVALID` entries inserted at the header layer are never removed**

`remove_block_status` is called only from `chain/src/verify.rs` during full block verification. Headers rejected by `HeaderAcceptor` never reach the chain verifier, so their `BLOCK_INVALID` entries persist for the lifetime of the process. [5](#0-4) 

**Cascading amplification**

Once one `BLOCK_INVALID` entry exists (seeded with a single PoW-valid but otherwise invalid header), an attacker can create arbitrarily many child headers (varying nonce/timestamp, no PoW required) that all reference that parent. Each child gets a unique hash inserted into `block_status_map` as `BLOCK_INVALID`. Those children can then serve as parents for the next generation, and so on — each generation requires only a new peer connection, not new PoW.

**Attack path**

1. Mine one header with valid PoW but a property that fails `non_contextual_check` (e.g., wrong epoch). This seeds one `BLOCK_INVALID` entry.
2. Connect with peer P₁; send a `SendHeaders` message whose first header references the `BLOCK_INVALID` parent. `prev_block_check` fires, inserts a new `BLOCK_INVALID` entry, `HeadersProcess` returns `StatusCode::HeaderIsInvalid`, P₁ is banned.
3. Connect with peer P₂; send a header referencing the entry from step 2 (different nonce → different hash). Another `BLOCK_INVALID` entry is inserted.
4. Repeat across many IPs/connections. Each connection costs one ban but inserts one permanent entry. The map grows monotonically.

### Impact Explanation

Each `BLOCK_INVALID` entry occupies ~100–200 bytes of heap (32-byte key + status + `DashMap` overhead). Sustained over many connections the map grows without bound, eventually exhausting process memory and crashing or severely degrading the node. This constitutes a **service-availability** impact reachable by any unprivileged network peer.

### Likelihood Explanation

The initial PoW cost is a one-time investment. After that, each peer connection (from a different IP or after ban expiry) inserts one permanent entry at negligible cost. A botnet or rotating-IP setup makes this practical. The node's default `max_inbound = 125` limits simultaneous connections but not the cumulative rate of insertions over time.

### Recommendation

1. **Cap `block_status_map`**: enforce a maximum entry count (e.g., 1 M) and evict the oldest or lowest-priority entries when the cap is reached.
2. **Periodic pruning**: add a background task that removes `BLOCK_INVALID` entries older than a configurable TTL (analogous to how `OrphanBlockPool` uses `clean_expired_blocks`).
3. **Rate-limit `BLOCK_INVALID` insertions per peer**: track how many `BLOCK_INVALID` entries a peer has contributed and ban earlier if the count is excessive.

### Proof of Concept

```
# Step 1 – seed one BLOCK_INVALID entry (requires PoW)
Mine header H₀ with valid PoW but epoch field set to an impossible value.
Send H₀ via SendHeaders → HeaderAcceptor::non_contextual_check fails →
  shared.insert_block_status(H₀.hash(), BLOCK_INVALID)   // entry 1

# Step 2 – cascade without PoW (repeat with fresh IPs)
for i in 1..N:
    Hᵢ = Header { parent_hash: H_{i-1}.hash(), nonce: i, ... }  // no PoW
    Connect peer Pᵢ, send SendHeaders([Hᵢ])
    → prev_block_check sees parent is BLOCK_INVALID
    → shared.insert_block_status(Hᵢ.hash(), BLOCK_INVALID)       // entry i+1
    → peer Pᵢ banned

# Result: block_status_map contains N+1 permanent BLOCK_INVALID entries,
# growing without bound, consuming O(N * 200 bytes) of heap.
```

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L324-354)
```rust
        if self.prev_block_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-parent header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }
```

**File:** shared/src/shared.rs (L421-457)
```rust
    pub fn block_status_map(&self) -> &DashMap<Byte32, BlockStatus> {
        &self.block_status_map
    }

    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
    }

    pub fn contains_block_status<T: ChainStore>(
        &self,
        block_hash: &Byte32,
        status: BlockStatus,
    ) -> bool {
        self.get_block_status(block_hash).contains(status)
    }

    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
    }
```

**File:** shared/src/shared.rs (L459-468)
```rust
    pub fn remove_block_status(&self, block_hash: &Byte32) {
        let log_now = std::time::Instant::now();
        self.block_status_map.remove(block_hash);
        debug!("remove_block_status cost {:?}", log_now.elapsed());
        shrink_to_fit!(self.block_status_map, SHRINK_THRESHOLD);
        debug!(
            "remove_block_status shrink_to_fit cost {:?}",
            log_now.elapsed()
        );
    }
```
