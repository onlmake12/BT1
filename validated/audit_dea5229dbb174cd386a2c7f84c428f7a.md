The code path is fully traceable. Here is the complete analysis:

**Entrypoint**: Any unprivileged peer sends a `GetBlockTransactions` P2P relay message.

**Execution path**:

1. `GetBlockTransactionsProcess::execute` receives the message. The only guards are a count check against `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) and a per-peer rate limiter of **30 req/s** keyed by `(PeerIndex, message_type)`. [1](#0-0) [2](#0-1) 

2. With no check for whether the block is frozen, the code unconditionally calls `shared.store().get_block(&block_hash)`. [3](#0-2) 

3. `get_block()` detects the block is frozen (`header.number() < freezer.number()`), calls `freezer.retrieve(header.number())` which reads the **entire** serialized block from the flat-file store into a heap-allocated `Vec<u8>`, then fully deserializes it via `packed::BlockReader::from_compatible_slice(&raw_block).to_entity().into_view()`. [4](#0-3) 

4. `freezer.retrieve` reads the full block bytes from disk (including optional Snappy decompression), proportional to the entire block size — not the size of the requested transactions. [5](#0-4) 

5. Back in `execute`, only the peer-specified transaction indexes are extracted from the fully-materialized `BlockView`. The rest of the deserialized block is discarded. [6](#0-5) 

**Rate limiter assessment**: The governor rate limiter allows 30 `GetBlockTransactions` messages per second per peer. With `MAX_RELAY_PEERS = 128`, this permits up to 3,840 full frozen-block reads per second across all peers. Each read is proportional to the full block size (up to ~1 MB), meaning up to ~3.8 GB/s of unnecessary freezer I/O is theoretically inducible — far exceeding what is needed to serve the actual requested transactions. [7](#0-6) 

**Invariant check**: The `get_transaction_with_info` path in the same store already demonstrates the correct pattern — it retrieves the full frozen block but then accesses only the specific transaction by index without materializing the rest. The `get_block` path used here has no such optimization; it always calls `.to_entity().into_view()` which fully materializes every field. [8](#0-7) 

---

### Title
Full Frozen-Block Deserialization on Partial `GetBlockTransactions` Request — (`sync/src/relayer/get_block_transactions_process.rs`)

### Summary
Any unprivileged peer can send a `GetBlockTransactions` relay message referencing a frozen (ancient) block hash. The handler unconditionally calls `get_block()`, which reads and fully deserializes the entire frozen block from the flat-file freezer store, even when only a single transaction index is requested. The rate limiter (30 req/s per peer, 128 max peers) permits up to 3,840 such reads per second, each proportional to the full block size.

### Finding Description
`GetBlockTransactionsProcess::execute` calls `shared.store().get_block(&block_hash)` without checking whether the block is frozen. `get_block()` in `store/src/store.rs` detects a frozen block by comparing `header.number() < freezer.number()`, then calls `freezer.retrieve(header.number())` to read the entire serialized block from the append-only flat-file store. The raw bytes are then passed through `packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data").to_entity().into_view()`, which fully deserializes every transaction, uncle, proposal, and extension field into heap-allocated Rust objects. Only after this full materialization does the handler extract the peer-requested transaction indexes and discard the rest.

The freezer's `retrieve` implementation reads exactly `end_offset - start_offset` bytes (the full block) from disk, optionally decompresses them, and returns a `Vec<u8>`. There is no mechanism to read only a sub-range corresponding to a specific transaction.

### Impact Explanation
- **Disk I/O amplification**: A peer requesting 1 transaction from a 1 MB frozen block causes 1 MB of disk reads instead of ~a few KB.
- **CPU amplification**: Full molecule deserialization of every field in the block is performed for every such request.
- **Multiplied by rate limit**: 30 req/s × 128 peers = 3,840 full-block reads/s, potentially gigabytes per second of unnecessary freezer I/O, degrading node responsiveness for legitimate sync and relay operations.
- **No PoW or consensus barrier**: Frozen block hashes are publicly known from the canonical chain; no privileged knowledge is required.

### Likelihood Explanation
The attack requires only a standard P2P connection and knowledge of any frozen block hash (trivially obtained from any block explorer or by syncing the chain). The rate limiter provides partial mitigation but does not prevent the amplification at scale with multiple peers.

### Recommendation
In `GetBlockTransactionsProcess::execute`, avoid calling `get_block()` for frozen blocks. Instead, retrieve individual transactions directly using `get_transaction_with_info` (which already implements the correct pattern of accessing only the needed transaction from the frozen block reader without full materialization), or add a dedicated `get_block_transactions_from_freezer` path that uses `packed::BlockReader::from_compatible_slice` to access only the requested indexes without calling `.to_entity().into_view()` on the full block.

### Proof of Concept
1. Freeze a large block (e.g., block #1 with many transactions) by running the node past the freeze threshold.
2. Connect as a peer and send a `GetBlockTransactions` message with `block_hash = frozen_block_hash`, `indexes = [0]` (one transaction).
3. Instrument `freezer_files.rs` `retrieve` to log bytes read (or observe `ckb_freezer_read` metrics).
4. Observe that bytes read equals the full block size, not the size of the single requested transaction.
5. Repeat at 30 req/s from multiple peer connections; observe freezer I/O metrics growing proportionally to `num_peers × block_size × 30`.

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-43)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L60-60)
```rust
        if let Some(block) = shared.store().get_block(&block_hash) {
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L61-71)
```rust
            let transactions = self
                .message
                .indexes()
                .iter()
                .filter_map(|i| {
                    block
                        .transactions()
                        .get(Into::<u32>::into(i) as usize)
                        .cloned()
                })
                .collect::<Vec<_>>();
```

**File:** sync/src/relayer/mod.rs (L59-92)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;

type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** store/src/store.rs (L44-53)
```rust
        if let Some(freezer) = self.freezer()
            && header.number() > 0
            && header.number() < freezer.number()
        {
            let raw_block = freezer.retrieve(header.number()).expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == h.as_slice() {
                return Some(raw_block_reader.to_entity().into_view());
            }
```

**File:** store/src/store.rs (L321-335)
```rust
        if let Some(freezer) = self.freezer()
            && tx_info.block_number > 0
            && tx_info.block_number < freezer.number()
        {
            let raw_block = freezer
                .retrieve(tx_info.block_number)
                .expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == tx_info.block_hash.as_slice()
                && let Some(tx_reader) = raw_block_reader.transactions().get(tx_info.index)
                && tx_reader.calc_tx_hash().as_slice() == hash.as_slice()
            {
                return Some((tx_reader.to_entity().into_view(), tx_info));
            }
```

**File:** freezer/src/freezer_files.rs (L176-204)
```rust
        let bounds = self.get_bounds(item)?;
        if let Some((start_offset, end_offset, file_id)) = bounds {
            let open_read_only;

            let mut file = if let Some(file) = self.files.get(&file_id) {
                file
            } else {
                open_read_only = self.open_read_only(file_id)?;
                &open_read_only
            };

            let size = (end_offset - start_offset) as usize;
            let mut data = vec![0u8; size];
            file.seek(SeekFrom::Start(start_offset))?;
            file.read_exact(&mut data)?;

            if self.enable_compression {
                data = SnappyDecoder::new().decompress_vec(&data).map_err(|e| {
                    IoError::other(format!(
                        "decompress file-id-{file_id} offset-{start_offset} size-{size}: error {e}"
                    ))
                })?;
            }

            if let Some(metrics) = ckb_metrics::handle() {
                metrics
                    .ckb_freezer_read
                    .inc_by(size as u64 + 2 * INDEX_ENTRY_SIZE);
            }
```
