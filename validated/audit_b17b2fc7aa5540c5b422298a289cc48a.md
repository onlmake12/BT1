### Title
Unconditional Panic in `block_median_time` Due to Missing Ancestor Header — No Fallback Source (`File: traits/src/header_provider.rs`)

---

### Summary

The `block_median_time` function in `traits/src/header_provider.rs` unconditionally panics via `.expect()` when any ancestor header is absent from the header store. There is no fallback or graceful error path. This is the direct CKB analog of the oracle gap vulnerability: a single data source (the header store) is consulted with no reserve, and a gap in that source causes an unrecoverable abort rather than a handled error. The panic is reachable from two consensus-critical call sites — block header verification during compact-block relay and transaction `since`-field verification — both of which are reachable by an unprivileged network peer or transaction sender.

---

### Finding Description

`block_median_time` in `traits/src/header_provider.rs` walks up to `median_block_count` (37 on mainnet) ancestor headers by following `parent_hash` links:

```rust
// traits/src/header_provider.rs  lines 35-38
for _ in 0..median_block_count {
    let header_fields = self
        .get_header_fields(&block_hash)
        .expect("parent header exist");   // ← unconditional panic on None
``` [1](#0-0) 

If `get_header_fields` returns `None` for any link in the ancestor chain, the thread panics. There is no fallback data source, no `Result` propagation, and no graceful degradation — exactly the "no reserve price source" pattern from the report.

A second independent panic site exists in `SinceVerifier::parent_median_time`:

```rust
// verification/src/transaction_verifier.rs  lines 619-622
let header_fields = self
    .data_loader
    .get_header_fields(block_hash)
    .expect("parent block exist");   // ← unconditional panic on None
``` [2](#0-1) 

Both callers assume the header store is complete and contiguous. Neither provides a reserve/fallback source.

---

### Impact Explanation

A panic in either call site unwinds the handling thread. In the compact-block relay path this aborts the async task processing the peer message; depending on the executor and panic handler configuration, it can crash the entire node process. At minimum it terminates block acceptance for that peer session, stalling sync. In the transaction verification path it aborts the verification task, potentially crashing the node or causing it to drop valid blocks that depend on the affected transaction.

---

### Likelihood Explanation

**Compact-block relay path** (higher likelihood): `CompactBlockProcess::execute` calls `contextual_check`, which constructs a `CompactBlockMedianTimeView` backed only by the in-memory pending-header cache. A malicious peer can send a compact block whose parent chain contains a header not yet present in that cache. `TimestampVerifier::verify` then calls `block_median_time` on the missing ancestor, triggering the panic. [3](#0-2) 

`TimestampVerifier::verify` calls `block_median_time` unconditionally for every non-genesis block: [4](#0-3) 

**Transaction `since` path** (lower but non-zero likelihood): Any RPC caller or P2P transaction relayer that submits a transaction with a timestamp-based `since` field referencing a block whose ancestor headers are not fully stored triggers `SinceVerifier::parent_median_time` → `block_median_time` → panic. [5](#0-4) 

---

### Recommendation

Replace both `.expect()` calls with proper `Result`/`Option` propagation:

1. Change `block_median_time` signature to `fn block_median_time(...) -> Option<u64>` (or `Result<u64, Error>`), returning `None`/`Err` when any ancestor header is absent.
2. Change `SinceVerifier::parent_median_time` to propagate the error upward instead of panicking.
3. All callers (`TimestampVerifier::verify`, `SinceVerifier::verify_*`) should treat a missing ancestor as a verification error (e.g., `UnknownParent`) and reject the block/transaction gracefully — analogous to adding a reserve price source so that the absence of the primary source does not cause a revert/crash.

---

### Proof of Concept

1. Run a CKB node in relay mode.
2. Craft a compact block whose header's `parent_hash` points to a block that exists on the node's main chain, but whose grandparent (or any ancestor within the 37-block median window) is absent from the node's pending-header cache (e.g., by sending the compact block before the full ancestor chain has been synced).
3. The node calls `CompactBlockProcess::execute` → `contextual_check` → `HeaderVerifier` → `TimestampVerifier::verify` → `block_median_time`.
4. `get_header_fields` returns `None` for the missing ancestor.
5. `.expect("parent header exist")` panics, crashing the relay handler thread. [6](#0-5)

### Citations

**File:** traits/src/header_provider.rs (L32-50)
```rust
    fn block_median_time(&self, block_hash: &Byte32, median_block_count: usize) -> u64 {
        let mut timestamps: Vec<u64> = Vec::with_capacity(median_block_count);
        let mut block_hash = block_hash.clone();
        for _ in 0..median_block_count {
            let header_fields = self
                .get_header_fields(&block_hash)
                .expect("parent header exist");
            timestamps.push(header_fields.timestamp);
            block_hash = header_fields.parent_hash;

            if header_fields.number == 0 {
                break;
            }
        }

        // return greater one if count is even.
        timestamps.sort_unstable();
        timestamps[timestamps.len() >> 1]
    }
```

**File:** verification/src/transaction_verifier.rs (L618-630)
```rust
    fn parent_median_time(&self, block_hash: &Byte32) -> u64 {
        let header_fields = self
            .data_loader
            .get_header_fields(block_hash)
            .expect("parent block exist");
        self.block_median_time(&header_fields.parent_hash)
    }

    fn block_median_time(&self, block_hash: &Byte32) -> u64 {
        let median_block_count = self.consensus.median_time_block_count();
        self.data_loader
            .block_median_time(block_hash, median_block_count)
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L56-73)
```rust
    pub async fn execute(self) -> Status {
        let instant = Instant::now();
        let shared = self.relayer.shared();
        let active_chain = shared.active_chain();
        let compact_block = self.message.to_entity();
        let header = compact_block.header().into_view();
        let block_hash = header.hash();

        let status =
            non_contextual_check(&compact_block, &header, shared.consensus(), &active_chain);
        if !status.is_ok() {
            return status;
        }

        let status = contextual_check(&header, shared, &active_chain, &self.nc, self.peer).await;
        if !status.is_ok() {
            return status;
        }
```

**File:** verification/src/header_verifier.rs (L70-79)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        // skip genesis block
        if self.header.is_genesis() {
            return Ok(());
        }

        let min = self.data_loader.block_median_time(
            &self.header.data().raw().parent_hash(),
            self.median_block_count,
        );
```
