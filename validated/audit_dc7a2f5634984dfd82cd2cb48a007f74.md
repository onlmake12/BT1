### Title
Inconsistent Snapshot in `reply_proof` Produces Invalid `verifiable_last_header` During Reorgs - (File: `util/light-client-protocol-server/src/lib.rs`)

---

### Summary

`reply_proof` in the light-client protocol server fetches a **fresh** `Snapshot` (S2) to compute the MMR `parent_chain_root` and proof, while the `last_block` passed to it was fetched from an **earlier** `Snapshot` (S1) inside `execute()`. During a chain reorganization occurring between these two snapshot fetches, the MMR data in S2 reflects the new chain, while `last_block` belongs to the old chain. The resulting `verifiable_last_header` embeds a `parent_chain_root` from the new chain into a header from the old chain — an internally inconsistent structure that light clients cannot verify.

---

### Finding Description

In `GetTransactionsProofProcess::execute()` and `GetBlocksProofProcess::execute()`, a snapshot S1 is taken at the start of the handler:

```rust
let snapshot = self.protocol.shared.snapshot();  // S1
let last_block = snapshot.get_block(&last_block_hash)...;  // from S1
``` [1](#0-0) 

After all processing, `reply_proof` is called with `last_block` (from S1):

```rust
self.protocol.reply_proof::<packed::SendTransactionsProofV1>(
    self.peer, self.nc, &last_block, positions, ...
).await
``` [2](#0-1) 

Inside `reply_proof`, a **second, independent** snapshot S2 is fetched:

```rust
let snapshot = self.shared.snapshot();  // S2 — potentially different from S1
let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
let parent_chain_root = match mmr.get_root() { ... };
``` [3](#0-2) 

This `parent_chain_root` is then embedded into the `verifiable_last_header` alongside the header from `last_block` (S1):

```rust
let verifiable_last_header = packed::VerifiableHeader::new_builder()
    .header(last_block.data().header())   // from S1 (old chain)
    .parent_chain_root(parent_chain_root) // from S2 (potentially new chain after reorg)
    .build();
``` [4](#0-3) 

The same pattern exists in `GetBlocksProofProcess::execute()`: [5](#0-4) 

The `Snapshot` struct's `chain_root_mmr` reads MMR node data from the underlying store: [6](#0-5) 

After a reorg, the store's MMR data at positions up to `last_block.number() - 1` is overwritten to reflect the new chain. So S2's MMR root at that height belongs to the new chain, while `last_block`'s extension field encodes the old chain's MMR root.

The `VerifiableHeader` validity check requires that `parent_chain_root.calc_mmr_hash()` matches the first 32 bytes of the block's extension: [7](#0-6) 

With a mismatched `parent_chain_root`, this check fails.

---

### Impact Explanation

Any light client peer that sends a `GetTransactionsProof` or `GetBlocksProof` request during a chain reorganization window receives a `verifiable_last_header` whose `parent_chain_root` (from the new chain) does not match the header's extension field (from the old chain). The light client's `is_valid()` check fails, causing it to reject the proof. The light client cannot verify the requested transactions or blocks until it retries after the reorg settles. This is a correctness/reliability failure in the light-client proof protocol: the server produces structurally invalid proofs under normal (non-adversarial) chain conditions.

---

### Likelihood Explanation

Small reorgs (1–2 blocks) occur naturally on any PoW chain. The vulnerable window is the async gap between the snapshot taken in `execute()` and the snapshot taken inside `reply_proof`. Because `reply_proof` is called with `.await`, the async runtime may schedule other tasks (including block processing that updates the shared snapshot) between the two fetches. Any light client peer that happens to send a proof request during a reorg can trigger this inconsistency without any special privileges or coordination.

---

### Recommendation

Pass the snapshot S1 (already held in `execute()`) into `reply_proof` instead of fetching a new one. Alternatively, refactor `reply_proof` to accept a `&Snapshot` parameter so that the same snapshot is used throughout the entire request lifecycle, ensuring the `parent_chain_root` and proof are always computed from the same chain state as `last_block`.

---

### Proof of Concept

1. Light client peer sends `GetTransactionsProof { last_hash: H, tx_hashes: [...] }` where `H` is the current tip.
2. Server's `execute()` takes snapshot S1, confirms `H` is on the main chain, fetches `last_block` from S1.
3. A 1-block reorg occurs: the chain reorganizes, replacing the block at `last_block.number()` with a different block. The store's MMR data is updated to reflect the new chain.
4. `reply_proof` takes snapshot S2 (post-reorg), computes `parent_chain_root` from S2's MMR at `last_block.number() - 1` — this is now the new chain's MMR root.
5. The `verifiable_last_header` is assembled with `last_block`'s header (old chain) and the new chain's `parent_chain_root`.
6. The light client calls `is_valid()` on the received `verifiable_last_header`: the `parent_chain_root.calc_mmr_hash()` does not match the first 32 bytes of `last_block`'s extension (which encodes the old chain's MMR root). Verification fails; the proof is rejected.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L41-52)
```rust
        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendTransactionsProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L137-146)
```rust
        self.protocol
            .reply_proof::<packed::SendTransactionsProofV1>(
                self.peer,
                self.nc,
                &last_block,
                positions,
                proved_items,
                missing_items,
            )
            .await
```

**File:** util/light-client-protocol-server/src/lib.rs (L195-219)
```rust
        let (parent_chain_root, proof) = if last_block.is_genesis() {
            (Default::default(), Default::default())
        } else {
            let snapshot = self.shared.snapshot();
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
            };
            (parent_chain_root, proof)
        };
```

**File:** util/light-client-protocol-server/src/lib.rs (L220-225)
```rust
        let verifiable_last_header = packed::VerifiableHeader::new_builder()
            .header(last_block.data().header())
            .uncles_hash(last_block.calc_uncles_hash())
            .extension(Pack::pack(&last_block.extension()))
            .parent_chain_root(parent_chain_root)
            .build();
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L104-113)
```rust
        self.protocol
            .reply_proof::<packed::SendBlocksProofV1>(
                self.peer,
                self.nc,
                &last_block,
                positions,
                proved_items,
                missing_items,
            )
            .await
```

**File:** util/snapshot/src/lib.rs (L180-184)
```rust
    /// Returns the chain root MMR for a provided block.
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** util/types/src/utilities/merkle_mountain_range.rs (L220-231)
```rust
                let is_extension_beginning_with_chain_root_hash = self
                    .extension()
                    .map(|extension| {
                        let actual_extension_data = extension.raw_data();
                        let parent_chain_root_hash = self.parent_chain_root().calc_mmr_hash();
                        actual_extension_data.starts_with(parent_chain_root_hash.as_slice())
                    })
                    .unwrap_or(false);
                if !is_extension_beginning_with_chain_root_hash {
                    return false;
                }
            }
```
