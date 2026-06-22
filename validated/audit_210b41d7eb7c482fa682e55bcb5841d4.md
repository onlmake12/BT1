Based on my investigation, I found a valid analog. The `HeaderMapConfig` struct declares two configurable limit fields — `primary_limit` and `backend_close_threshold` — that are never read or applied anywhere in the codebase. Only `memory_limit` is actually passed to `HeaderMap::new`. This is a direct structural analog to the GovNFT `maxBridge` variable: operator-facing configuration fields that appear to control resource limits but have no effect.

---

### Title
`HeaderMapConfig::primary_limit` and `backend_close_threshold` Have No Effect — (`util/app-config/src/configs/network.rs`)

### Summary
`HeaderMapConfig` exposes two configurable fields — `primary_limit` and `backend_close_threshold` — that are documented as controlling in-memory size and backend-close behavior of the `HeaderMap`. Neither field is ever read or passed to `HeaderMap::new`. Only `memory_limit` is actually enforced. An operator who sets these fields to tune memory behavior during IBD sync gets no effect, and the `HeaderMap` can grow beyond the `primary_limit` the operator intended to enforce.

### Finding Description
`HeaderMapConfig` is defined in `util/app-config/src/configs/network.rs` with three fields:

```rust
pub struct HeaderMapConfig {
    /// The maximum size of data in memory
    pub primary_limit: Option<usize>,
    /// Disable cache if the size of data in memory less than this threshold
    pub backend_close_threshold: Option<usize>,
    /// The maximum amount memory limit
    #[serde(default = "default_memory_limit")]
    pub memory_limit: ByteUnit,
}
``` [1](#0-0) 

`HeaderMap::new` — the sole constructor, called once in `shared/src/shared_builder.rs` — accepts only a `memory_limit: usize` parameter:

```rust
pub fn new<P>(
    tmpdir: Option<P>,
    memory_limit: usize,
    async_handle: &Handle,
    ibd_finished: Arc<AtomicBool>,
) -> Self
``` [2](#0-1) 

A grep across the entire Rust source tree confirms `primary_limit` and `backend_close_threshold` appear **only** in `util/app-config/src/configs/network.rs` — they are never read, passed, or applied anywhere else. [3](#0-2) 

The `memory_limit` field is correctly wired through and enforced inside `HeaderMap::new` via `size_limit = memory_limit / ITEM_BYTES_SIZE`, which is passed to `HeaderMapKernel::new`. [4](#0-3) 

### Impact Explanation
`HeaderMap` stores block headers received from sync peers before full block verification, and is heavily used during IBD. `primary_limit` was intended to cap the in-memory portion of this map (distinct from the total `memory_limit`), and `backend_close_threshold` was intended to control when the disk-backed sled cache is disabled. Because neither field is applied:

1. An operator who sets `primary_limit` to a value smaller than `memory_limit` (intending a tighter in-memory cap) gets no enforcement — the in-memory map grows to the full `memory_limit`.
2. An operator who sets `backend_close_threshold` to control disk-backend lifecycle gets no enforcement — the backend lifecycle is uncontrolled by this field.
3. A remote sync peer sending a large volume of block headers during IBD can cause the `HeaderMap` to consume more memory than the operator intended to allow via `primary_limit`, potentially contributing to memory exhaustion on resource-constrained nodes. [5](#0-4) 

### Likelihood Explanation
The entry path is straightforward and requires no privilege: any peer connected to a CKB node during IBD can send `SendHeaders` or `SendBlock` messages that cause headers to be inserted into the `HeaderMap`. The misconfiguration is silent — no warning or error is emitted when `primary_limit` or `backend_close_threshold` are set. An operator who reads the documented field descriptions and sets these values will have a false sense of security about memory bounds. [6](#0-5) 

### Recommendation
Either:
1. Pass `primary_limit` and `backend_close_threshold` from `HeaderMapConfig` into `HeaderMap::new` (and propagate them into `HeaderMapKernel`) and enforce them in the LRU eviction and backend-close logic; or
2. Remove `primary_limit` and `backend_close_threshold` from `HeaderMapConfig` entirely if they are no longer applicable to the current `HeaderMapKernel<SledBackend>` implementation, to avoid misleading operators. [7](#0-6) 

### Proof of Concept
1. Set `primary_limit = 100` and `backend_close_threshold = 50` in the node's network config under `[network.header_map]`.
2. Start the node and connect a sync peer that sends a large number of block headers (e.g., during IBD).
3. Observe via metrics or memory profiling that the `HeaderMap` in-memory entry count grows well beyond 100 entries, confirming `primary_limit` is not enforced.
4. Confirm by grepping the entire source tree: `grep -r "primary_limit\|backend_close_threshold" --include="*.rs"` returns results only in `util/app-config/src/configs/network.rs`, proving neither field is consumed anywhere in the implementation. [8](#0-7)

### Citations

**File:** util/app-config/src/configs/network.rs (L194-212)
```rust
pub struct HeaderMapConfig {
    /// The maximum size of data in memory
    pub primary_limit: Option<usize>,
    /// Disable cache if the size of data in memory less than this threshold
    pub backend_close_threshold: Option<usize>,
    /// The maximum amount memory limit
    #[serde(default = "default_memory_limit")]
    pub memory_limit: ByteUnit,
}

impl Default for HeaderMapConfig {
    fn default() -> Self {
        Self {
            primary_limit: None,
            backend_close_threshold: None,
            memory_limit: default_memory_limit(),
        }
    }
}
```

**File:** shared/src/types/header_map/mod.rs (L34-53)
```rust
    pub fn new<P>(
        tmpdir: Option<P>,
        memory_limit: usize,
        async_handle: &Handle,
        ibd_finished: Arc<AtomicBool>,
    ) -> Self
    where
        P: AsRef<path::Path>,
    {
        if memory_limit < ITEM_BYTES_SIZE {
            panic!("The limit setting is too low");
        }
        if memory_limit < WARN_THRESHOLD {
            ckb_logger::warn!(
                "The low memory limit setting {} will result in inefficient synchronization",
                memory_limit
            );
        }
        let size_limit = memory_limit / ITEM_BYTES_SIZE;
        let inner = Arc::new(HeaderMapKernel::new(tmpdir, size_limit, ibd_finished));
```
