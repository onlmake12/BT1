Based on my analysis of the actual code, here is my assessment:

---

### Title
Missing `txs_len` Consensus Bound Check Allows Attacker-Controlled Memory Allocation in CompactBlock Processing — (`sync/src/relayer/compact_block_verifier.rs`, `sync/src/relayer/compact_block_process.rs`)

### Summary

`non_contextual_check` guards `uncles` and `proposals` counts against consensus limits but has no corresponding guard for `short_ids.len() + prefilled_transactions.len()`. This allows a remote peer to send a `CompactBlock` with an attacker-controlled `txs_len` that causes proportional `HashSet` and `Vec` allocations before any consensus-bounded rejection.

### Finding Description

`non_contextual_check` explicitly checks:

```rust
compact_block.uncles().len() > consensus.max_uncles_num()
compact_block.proposals().len() > consensus.max_block_proposals_limit()
``` [1](#0-0) 

But there is **no** equivalent check on `short_ids.len() + prefilled_transactions.len()` against any consensus transaction count limit.

`CompactBlockVerifier::verify` then calls `ShortIdsVerifier::verify`, which unconditionally builds a `HashSet<ProposalShortId>` from all short IDs:

```rust
let short_ids_set: HashSet<packed::ProposalShortId> =
    short_ids.clone().into_iter().collect();
``` [2](#0-1) 

Then `reconstruct_block` computes `txs_len()` as:

```rust
pub fn txs_len(&self) -> usize {
    self.prefilled_transactions().len() + self.short_ids().len()
}
``` [3](#0-2) 

And allocates directly from it:

```rust
let txs_len = compact_block.txs_len();
let mut block_transactions: Vec<Option<core::TransactionView>> =
    Vec::with_capacity(txs_len);
``` [4](#0-3) 

Plus a second `HashSet` built from `short_ids` in `reconstruct_block`:

```rust
let mut short_ids_set: HashSet<ProposalShortId> =
    compact_block.short_ids().into_iter().collect();
``` [5](#0-4) 

### Impact Explanation

Each `ProposalShortId` is 10 bytes on the wire. If the network frame limit is ~4 MB, an attacker can pack ~400,000 short IDs into a single message. A valid block can hold far fewer transactions (bounded by block byte size and minimum tx size). The node allocates a `Vec` and two `HashSet`s proportional to the attacker-supplied count — significantly exceeding what any valid block could justify — before any consensus-bounded rejection occurs. Multiple concurrent peers can amplify this into sustained memory pressure.

### Likelihood Explanation

The precondition (known parent + PoW/timestamp passing) is non-trivial but not required: `CompactBlockVerifier::verify` is called **after** `contextual_check` but the `ShortIdsVerifier` allocation happens inside `CompactBlockVerifier::verify` itself, which runs before `reconstruct_block`. An attacker who can pass the header checks (or even just reach `CompactBlockVerifier::verify` with a crafted block) triggers the allocations. The asymmetry — uncles and proposals are bounded, transactions are not — strongly suggests an oversight rather than an intentional design.

### Recommendation

Add a check in `non_contextual_check` (or at the top of `CompactBlockVerifier::verify`) before any allocation:

```rust
let txs_len = compact_block.short_ids().len() + compact_block.prefilled_transactions().len();
if txs_len > consensus.max_block_proposals_limit() as usize /* or a dedicated max_block_txs */ {
    return StatusCode::ProtocolMessageIsMalformed.with_context(...);
}
```

This mirrors the existing pattern for uncles and proposals. [1](#0-0) 

### Proof of Concept

1. Craft a `CompactBlock` with a valid header (passes PoW/timestamp), one prefilled cellbase at index 0, and `short_ids` filled to `floor(MAX_FRAME_SIZE / 10)` entries (all distinct 10-byte values).
2. Send to a node that has the parent block.
3. Observe: `non_contextual_check` passes (no txs count check), `ShortIdsVerifier::verify` allocates a `HashSet` of ~400K entries, `reconstruct_block` allocates `Vec::with_capacity(~400K)` and a second `HashSet`.
4. Repeat from multiple peers concurrently to amplify memory pressure.
5. Assert that without the fix, RSS grows proportionally to `short_ids.len()` far beyond what `max_block_txs` would permit.

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

**File:** sync/src/relayer/compact_block_verifier.rs (L66-67)
```rust
        let short_ids_set: HashSet<packed::ProposalShortId> =
            short_ids.clone().into_iter().collect();
```

**File:** util/gen-types/src/extension/shortcut.rs (L187-189)
```rust
    pub fn txs_len(&self) -> usize {
        self.prefilled_transactions().len() + self.short_ids().len()
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
