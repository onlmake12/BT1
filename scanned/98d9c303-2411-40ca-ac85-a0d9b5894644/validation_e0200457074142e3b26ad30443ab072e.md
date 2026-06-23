### Title
Unbounded `short_ids` Count in CompactBlock Causes Excessive Memory Allocation in `reconstruct_block` - (File: `sync/src/relayer/compact_block_process.rs`, `sync/src/relayer/mod.rs`)

---

### Summary

The `non_contextual_check` function validates a received `CompactBlock` against consensus limits for uncles and proposals, but imposes **no upper bound** on `compact_block.short_ids().len()` or `compact_block.prefilled_transactions().len()`. A malicious peer can craft a `CompactBlock` message containing the maximum number of short IDs permitted by the 8 MB P2P decompression limit (~800,000 entries), causing `reconstruct_block` to allocate large heap structures proportional to the attacker-controlled `txs_len` before any transaction-root or block-validity check is performed.

---

### Finding Description

**Root cause — missing count bound in `non_contextual_check`:**

`sync/src/relayer/compact_block_process.rs` calls `non_contextual_check` before `reconstruct_block`. That function enforces:

```rust
if compact_block.uncles().len() > consensus.max_uncles_num() { … }
if (compact_block.proposals().len() as u64) > consensus.max_block_proposals_limit() { … }
``` [1](#0-0) 

There is **no analogous check** on `compact_block.short_ids().len()` against any consensus-derived transaction limit.

**Allocation site — `reconstruct_block`:**

```rust
let txs_len = compact_block.txs_len();          // attacker-controlled
let mut block_transactions: Vec<Option<core::TransactionView>> =
    Vec::with_capacity(txs_len);                // heap allocation
``` [2](#0-1) 

`txs_len()` is defined as:

```rust
pub fn txs_len(&self) -> usize {
    self.prefilled_transactions().len() + self.short_ids().len()
}
``` [3](#0-2) 

Additional allocations in the same function:

```rust
let mut short_ids_set: HashSet<ProposalShortId> =
    compact_block.short_ids().into_iter().collect();   // ~16 MB at max
``` [4](#0-3) 

And in `block_short_ids()` (called by `BlockTransactionsVerifier`):

```rust
let txs_len = self.txs_len();
let mut block_short_ids: Vec<Option<packed::ProposalShortId>> = Vec::with_capacity(txs_len);
``` [5](#0-4) 

**`CompactBlockVerifier` does not bound the count:**

`CompactBlockVerifier::verify` only checks ordering, cellbase presence, index bounds relative to `txs_len`, and duplicate short IDs — none of which limit the absolute count. [6](#0-5) 

**P2P layer bound:**

The only constraint is the decompression limit:

```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
``` [7](#0-6) 

Each `ProposalShortId` is 10 bytes, so a single message can carry up to ~800,000 short IDs. The consensus maximum for block transactions is far lower (derived from `MAX_BLOCK_BYTES = 597,000` bytes; a minimum-size transaction is ~200 bytes, giving ~2,985 transactions). No check enforces this tighter bound on the incoming compact block.

---

### Impact Explanation

Per malicious compact block message, the node allocates approximately:

| Structure | Size |
|---|---|
| `HashSet<ProposalShortId>` (800k entries) | ~16 MB |
| `Vec<Option<TransactionView>>` (800k capacity) | ~6.4 MB |
| `Vec<Option<ProposalShortId>>` in `block_short_ids()` | ~12.8 MB |
| **Total per message** | **~35 MB** |

Additionally, `tx_pool.fetch_txs(short_ids_set)` is called with the full 800k-entry set, causing 800,000 hash-map lookups in the tx pool under a lock.

A small number of concurrent malicious peers (e.g., 10–20) sending such messages can push the node's heap usage by 350–700 MB above baseline, potentially triggering OOM on memory-constrained deployments or causing severe latency spikes in the tx pool. The node does not ban the peer for this behavior because the message passes all structural checks; the failure only occurs later at the transaction-root comparison.

---

### Likelihood Explanation

- **Entry path:** Any unauthenticated P2P peer on the relay protocol can send a `CompactBlock` message. No stake, key, or privileged role is required.
- **Ease of construction:** The attacker simply builds a `CompactBlock` with 1 prefilled cellbase (index 0) and fills the remaining 8 MB with unique 10-byte short IDs. The `CompactBlockVerifier` passes this message.
- **Amplification:** The attacker can open multiple connections (up to the node's peer limit) and send such messages in a tight loop, since each message is processed before the peer is disconnected.
- **No rate limiting** on compact block messages per peer is visible in the relay handler.

---

### Recommendation

Add an explicit upper-bound check on `short_ids().len()` (and optionally `prefilled_transactions().len()`) inside `non_contextual_check`, bounded by the consensus-derived maximum block transaction count:

```rust
let max_block_txs = consensus.max_block_bytes() / MIN_TRANSACTION_SERIALIZED_SIZE;
if compact_block.short_ids().len() + compact_block.prefilled_transactions().len()
    > max_block_txs as usize
{
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "CompactBlock txs count({}) exceeds consensus max({})",
        compact_block.txs_len(), max_block_txs
    ));
}
```

This mirrors the existing pattern for uncles and proposals: [1](#0-0) 

---

### Proof of Concept

1. Connect to a CKB mainnet/testnet node as a relay peer (SupportProtocols::RelayV3).
2. Construct a `CompactBlock` molecule message:
   - `header`: any valid-looking header (parent hash = tip hash, valid PoW not required to pass `non_contextual_check`).
   - `prefilled_transactions`: one `IndexTransaction` with `index = 0` and a minimal cellbase transaction.
   - `short_ids`: fill with ~800,000 unique 10-byte `ProposalShortId` values until the serialized message approaches 8 MB.
   - `uncles`: empty.
   - `proposals`: empty.
3. Send the message. The node will:
   - Pass `CompactBlockVerifier::verify` (cellbase present, no duplicates, index 0 < txs_len).
   - Pass `non_contextual_check` (uncles = 0, proposals = 0).
   - Enter `reconstruct_block`, allocating ~35 MB of heap.
   - Call `tx_pool.fetch_txs` with 800,000 short IDs.
   - Eventually fail the transaction-root check and return `Collided` or `Missing` — **without banning the peer**.
4. Repeat from multiple connections to amplify memory pressure.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L196-209)
```rust
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
```

**File:** sync/src/relayer/mod.rs (L371-372)
```rust
        let mut short_ids_set: HashSet<ProposalShortId> =
            compact_block.short_ids().into_iter().collect();
```

**File:** sync/src/relayer/mod.rs (L395-397)
```rust
        let txs_len = compact_block.txs_len();
        let mut block_transactions: Vec<Option<core::TransactionView>> =
            Vec::with_capacity(txs_len);
```

**File:** util/gen-types/src/extension/shortcut.rs (L187-189)
```rust
    pub fn txs_len(&self) -> usize {
        self.prefilled_transactions().len() + self.short_ids().len()
    }
```

**File:** util/types/src/extension.rs (L121-122)
```rust
        let txs_len = self.txs_len();
        let mut block_short_ids: Vec<Option<packed::ProposalShortId>> = Vec::with_capacity(txs_len);
```

**File:** sync/src/relayer/compact_block_verifier.rs (L11-15)
```rust
    pub(crate) fn verify(block: &packed::CompactBlock) -> Status {
        attempt!(PrefilledVerifier::verify(block));
        attempt!(ShortIdsVerifier::verify(block));
        Status::ok()
    }
```

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```
