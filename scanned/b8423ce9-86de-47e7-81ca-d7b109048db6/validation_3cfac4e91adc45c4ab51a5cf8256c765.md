### Title
Block Assembler Rejects Valid Exact-Boundary Block Sizes Due to Off-by-One in `<` vs `<=` Check - (`tx-pool/src/block_assembler/mod.rs`)

### Summary

In `BlockAssembler::update_uncles()` and `BlockAssembler::update_proposals()`, the block template is only updated when `new_total_size < max_block_bytes`. However, the consensus-layer `BlockBytesVerifier` accepts blocks where `block_bytes <= block_bytes_limit`. This mismatch means the block assembler refuses to build a block template that is exactly at the byte limit, even though such a block is fully valid by consensus. Miners cannot utilize the last byte of block capacity for uncles or proposals.

### Finding Description

The consensus verifier in `verification/src/block_verifier.rs` uses a `<=` comparison:

```rust
// verification/src/block_verifier.rs:257
if block_bytes <= self.block_bytes_limit {
    Ok(())
} else {
    Err(BlockErrorKind::ExceededMaximumBlockBytes.into())
}
``` [1](#0-0) 

This means a block with `block_bytes == block_bytes_limit` is **valid** by consensus.

However, in the block assembler, both `update_uncles()` and `update_proposals()` use strict `<`:

```rust
// update_uncles — line 350
if new_total_size < max_block_bytes {
    // accept uncles into template
}

// update_proposals — line 391
if new_total_size < max_block_bytes {
    // accept proposals into template
}
``` [2](#0-1) [3](#0-2) 

When `new_total_size == max_block_bytes`, the condition is `false`, so the template update is silently skipped. The block assembler will never produce a template that fills the block to exactly its byte limit with uncles or proposals.

There is also a secondary off-by-one in the uncle pre-guard at line 344:

```rust
if remain_size > UncleBlockView::serialized_size_in_block() {
``` [4](#0-3) 

When `remain_size == UncleBlockView::serialized_size_in_block()` (exactly one uncle fits), the guard rejects the attempt entirely, compounding the off-by-one.

### Impact Explanation

Miners calling `get_block_template` via RPC receive a template that cannot be filled to exactly `max_block_bytes` with uncles or proposals. This means:

- **Uncle rewards**: If including a set of uncles would bring the block to exactly `max_block_bytes`, those uncles are excluded. Miners lose uncle-inclusion rewards.
- **Proposal throughput**: If a set of proposals would bring the block to exactly `max_block_bytes`, those proposals are excluded. This delays transaction confirmation and reduces future fee revenue.

The resulting block is always one byte short of the limit when the exact-boundary case arises, wasting block space that is valid by consensus.

### Likelihood Explanation

The `get_block_template` RPC is called continuously by mining software. The block byte size is a sum of serialized components (header, cellbase, uncles, proposals, transactions). While hitting exactly `max_block_bytes` is not guaranteed on every block, it is a reachable condition over time, particularly as blocks fill up and the assembler iteratively adds components. The bug is deterministic: whenever the exact boundary is reached, the update is always rejected.

### Recommendation

Change both strict `<` comparisons to `<=` to match the consensus rule:

```rust
// update_uncles
if new_total_size <= max_block_bytes {  // was: <

// update_proposals
if new_total_size <= max_block_bytes {  // was: <
```

Also fix the uncle pre-guard:

```rust
if remain_size >= UncleBlockView::serialized_size_in_block() {  // was: >
```

### Proof of Concept

1. Consensus allows `block_bytes == block_bytes_limit` (verified at `verification/src/block_verifier.rs:257` using `<=`). [5](#0-4) 
2. Miner calls `get_block_template` → `BlockAssembler::update_uncles()` is invoked.
3. After computing `new_total_size`, suppose `new_total_size == max_block_bytes` (e.g., one uncle fits exactly).
4. The check `if new_total_size < max_block_bytes` evaluates to `false`; the uncle set is discarded.
5. The miner receives a template missing valid uncles, losing uncle rewards, even though submitting a block with those uncles would pass `BlockBytesVerifier`. [6](#0-5)

### Citations

**File:** verification/src/block_verifier.rs (L251-262)
```rust
    pub fn verify(&self, block: &BlockView) -> Result<(), Error> {
        // Skip bytes limit on genesis block
        if block.is_genesis() {
            return Ok(());
        }
        let block_bytes = block.data().serialized_size_without_uncle_proposals() as u64;
        if block_bytes <= self.block_bytes_limit {
            Ok(())
        } else {
            Err(BlockErrorKind::ExceededMaximumBlockBytes.into())
        }
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L344-345)
```rust
            if remain_size > UncleBlockView::serialized_size_in_block() {
                let uncles = self.prepare_uncles(&current.snapshot, &current.epoch).await;
```

**File:** tx-pool/src/block_assembler/mod.rs (L347-362)
```rust
                let new_uncle_size = uncles.len() * UncleBlockView::serialized_size_in_block();
                let new_total_size = current.size.calc_total_by_uncles(new_uncle_size);

                if new_total_size < max_block_bytes {
                    let mut builder = BlockTemplateBuilder::from_template(&current.template);
                    builder
                        .set_uncles(uncles)
                        .work_id(self.work_id.fetch_add(1, Ordering::SeqCst))
                        .current_time(cmp::max(
                            unix_time_as_millis(),
                            current.template.current_time,
                        ));
                    current.template = builder.build();
                    current.size.uncles = new_uncle_size;
                    current.size.total = new_total_size;

```

**File:** tx-pool/src/block_assembler/mod.rs (L391-392)
```rust
        if new_total_size < max_block_bytes {
            let mut builder = BlockTemplateBuilder::from_template(&current.template);
```
