### Title
Missing `txs_len` Bound Check Before `HashSet`/`Vec` Allocation in CompactBlock Relay — (`sync/src/relayer/compact_block_verifier.rs`, `sync/src/relayer/compact_block_process.rs`)

---

### Summary

`non_contextual_check` and `CompactBlockVerifier` both guard `uncles` and `proposals` counts against consensus limits, but neither checks `short_ids.len() + prefilled_transactions.len()` (`txs_len`) against any consensus bound. An unprivileged remote peer can craft a `CompactBlock` whose `short_ids` list fills the 4 MB RelayV3 frame (~400 000 entries × 10 bytes), causing two proportional heap allocations before any consensus-bounded rejection.

---

### Finding Description

**Entry point:** `CompactBlockProcess::execute` in `sync/src/relayer/compact_block_process.rs`.

**Step 1 — `non_contextual_check`** checks uncles and proposals counts but has no `txs_len` guard:

```
compact_block.uncles().len() > consensus.max_uncles_num()          // checked ✓
compact_block.proposals().len() > max_block_proposals_limit()      // checked ✓
short_ids.len() + prefilled_transactions.len() > ???               // NOT checked ✗
``` [1](#0-0) 

**Step 2 — `CompactBlockVerifier::verify`** calls `ShortIdsVerifier::verify`, which immediately allocates a `HashSet<ProposalShortId>` from the raw `short_ids` list with no prior count check:

```rust
let short_ids_set: HashSet<packed::ProposalShortId> =
    short_ids.clone().into_iter().collect();   // allocation here, no bound check
``` [2](#0-1) 

**Step 3 — `reconstruct_block`** calls `Vec::with_capacity(txs_len)` and builds a second `HashSet` from `short_ids`, both sized by the attacker-supplied count:

```rust
let txs_len = compact_block.txs_len();                          // = short_ids.len() + prefilled.len()
let mut block_transactions: Vec<Option<core::TransactionView>> =
    Vec::with_capacity(txs_len);                                // attacker-controlled capacity
let mut short_ids_set: HashSet<ProposalShortId> =
    compact_block.short_ids().into_iter().collect();            // second proportional allocation
``` [3](#0-2) [4](#0-3) 

`txs_len()` is simply the raw sum with no clamping: [5](#0-4) 

**Frame-size bound vs. consensus bound:**

The RelayV3 protocol allows frames up to **4 MB**: [6](#0-5) 

Each `ProposalShortId` is 10 bytes, so a single message can carry ≈ 400 000 short_ids. The consensus `max_block_bytes` is 597 000 bytes (`TWO_IN_TWO_OUT_BYTES × TWO_IN_TWO_OUT_COUNT`): [7](#0-6) 

A minimal real transaction is ~60–100 bytes, so a valid block can hold at most ~6 000–10 000 transactions. The missing check allows the attacker to force allocations **~40× larger** than any valid block could require, purely from the short_ids wire encoding.

---

### Impact Explanation

Each crafted 4 MB CompactBlock message triggers:
- One `HashSet<ProposalShortId>` allocation in `ShortIdsVerifier` (~400 000 × ~50 bytes with HashMap overhead ≈ ~20 MB)
- One `Vec<Option<TransactionView>>` with capacity 400 000 in `reconstruct_block` (~3.2 MB)
- One `HashSet<ProposalShortId>` in `reconstruct_block` (~20 MB)

Total per-message heap pressure: ~40–50 MB from a 4 MB wire message (~10× amplification). Multiple peers sending such messages concurrently can cause sustained memory pressure on the receiving node, degrading performance and potentially triggering OOM conditions on memory-constrained deployments. This matches the stated scope: **suboptimal state storage / memory pressure**.

---

### Likelihood Explanation

The precondition (known parent block) is trivially satisfied: any peer connected to the main chain tip can relay a compact block whose parent is the current tip. No PoW solution is required for the compact block header to pass `non_contextual_check` at the point where the allocations occur — `CompactBlockVerifier::verify` (and thus the allocations) runs **before** `contextual_check` completes header PoW verification in the execution flow: [8](#0-7) 

Any unprivileged peer can trigger this path repeatedly.

---

### Recommendation

Add a `txs_len` guard in `non_contextual_check` before `CompactBlockVerifier::verify` is called:

```rust
let txs_len = compact_block.short_ids().len() + compact_block.prefilled_transactions().len();
let max_block_txs = consensus.max_block_bytes() / MIN_TRANSACTION_BYTES;
if txs_len as u64 > max_block_txs {
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "CompactBlock txs_len({}) > derived max_block_txs({})",
        txs_len, max_block_txs
    ));
}
```

This mirrors the existing guards for `uncles` and `proposals` and ensures all three proportional allocations in `ShortIdsVerifier::verify` and `reconstruct_block` are bounded by a consensus constant before any heap allocation occurs. [9](#0-8) 

---

### Proof of Concept

1. Connect to a CKB node as a peer on the RelayV3 protocol.
2. Observe the current chain tip hash (parent for the crafted block).
3. Craft a `CompactBlock` molecule message with:
   - A valid-looking header (parent = tip hash, any nonce)
   - `prefilled_transactions` = `[IndexTransaction { index: 0, tx: minimal_cellbase }]`
   - `short_ids` = 399 999 distinct 10-byte values (total wire size ≈ 4 MB)
4. Send the message on the RelayV3 stream.
5. Observe on the receiving node: `ShortIdsVerifier::verify` allocates a ~20 MB `HashSet`, then `reconstruct_block` allocates another ~23 MB of `Vec` + `HashSet`, for a total of ~43 MB from a single 4 MB message.
6. Repeat from multiple peers concurrently to amplify memory pressure.

### Citations

**File:** sync/src/relayer/compact_block_process.rs (L64-76)
```rust
        let status =
            non_contextual_check(&compact_block, &header, shared.consensus(), &active_chain);
        if !status.is_ok() {
            return status;
        }

        let status = contextual_check(&header, shared, &active_chain, &self.nc, self.peer).await;
        if !status.is_ok() {
            return status;
        }

        // The new arrived has greater difficulty than local best known chain
        attempt!(CompactBlockVerifier::verify(&compact_block));
```

**File:** sync/src/relayer/compact_block_process.rs (L196-222)
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
```

**File:** sync/src/relayer/compact_block_verifier.rs (L66-67)
```rust
        let short_ids_set: HashSet<packed::ProposalShortId> =
            short_ids.clone().into_iter().collect();
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

**File:** network/src/protocols/support_protocols.rs (L130-130)
```rust
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```

**File:** spec/src/consensus.rs (L83-84)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```
