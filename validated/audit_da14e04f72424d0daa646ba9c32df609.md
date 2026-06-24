All code claims verified against the actual repository. Every cited line matches exactly.

**Verification summary:**

- `delete_block_body` at `store/src/write_batch.rs:97` deletes `COLUMN_BLOCK_UNCLE` but leaves `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` intact. [1](#0-0) 
- `wipe_out_frozen_data` at `shared/src/shared.rs:221-226` explicitly comments "remain header" and calls `delete_block_body`, confirming the asymmetric deletion. [2](#0-1) 
- `is_main_chain` at `store/src/store.rs:279-281` reads only `COLUMN_INDEX`, which is never deleted during freezing — returns `true` for frozen blocks. [3](#0-2) 
- `get_block_uncles` at `store/src/store.rs:205-224` has no freezer fallback — only reads `COLUMN_BLOCK_UNCLE`. Contrast with `get_block` at lines 44-54 which checks `freezer.number()` and retrieves from flat files. [4](#0-3) 
- `GetBlocksProofProcess::execute` at `get_blocks_proof.rs:88-90` calls `get_block_uncles` with unconditional `.expect` after partitioning by `is_main_chain`. [5](#0-4) 

---

Audit Report

## Title
Remote Peer Can Panic Light-Client Node via `GetBlocksProof` on Frozen Block — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
When the freezer is enabled, `wipe_out_frozen_data` calls `delete_block_body` which removes uncle data from `COLUMN_BLOCK_UNCLE` while intentionally leaving `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` intact. Any remote peer can send a `GetBlocksProof` message referencing a frozen block hash, causing `is_main_chain` to return `true`, `get_block_uncles` to return `None` (no freezer fallback), and the unconditional `.expect("block uncles must be stored")` at line 90 to panic, terminating the node process.

## Finding Description
`StoreWriteBatch::delete_block_body` deletes `COLUMN_BLOCK_UNCLE` for every block moved to the freezer, but the comment "remain header" in `wipe_out_frozen_data` confirms `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` are intentionally preserved:

```rust
// shared/src/shared.rs:221-226
// remain header
for (hash, (number, txs)) in &frozen {
    batch.delete_block_body(*number, hash, *txs)...
```

```rust
// store/src/write_batch.rs:97
self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
```

`is_main_chain` reads only `COLUMN_INDEX` (never deleted), so it returns `true` for frozen blocks:

```rust
// store/src/store.rs:279-281
fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
    self.get(COLUMN_INDEX, hash.as_slice()).is_some()
}
```

`get_block_uncles` has no freezer fallback — it only reads `COLUMN_BLOCK_UNCLE`, which has been deleted:

```rust
// store/src/store.rs:212
let ret = self.get(COLUMN_BLOCK_UNCLE, hash.as_slice()).map(|slice| { ... });
```

This contrasts with `get_block` (lines 44–54), which checks `freezer.number()` and retrieves from flat files before falling through to RocksDB. `get_block_uncles` has no equivalent guard.

In `GetBlocksProofProcess::execute`, frozen block hashes pass the `is_main_chain` partition check (line 74), then the loop at lines 81–95 calls `get_block_uncles` with an unconditional `.expect`:

```rust
// get_blocks_proof.rs:88-90
let uncles = snapshot
    .get_block_uncles(&block_hash)
    .expect("block uncles must be stored");  // panics for frozen blocks
```

The full exploit path:
1. Freezer runs normally; blocks older than `freezer.number()` have `COLUMN_BLOCK_UNCLE` deleted but `COLUMN_INDEX` and `COLUMN_BLOCK_HEADER` intact.
2. Attacker sends `GetBlocksProof { block_hashes: [frozen_hash], last_hash: tip_hash }`.
3. `is_main_chain(frozen_hash)` → `true` (COLUMN_INDEX present).
4. `frozen_hash` enters the `found` partition.
5. `get_block_header(frozen_hash)` → `Some` (COLUMN_BLOCK_HEADER present).
6. `get_block_uncles(frozen_hash)` → `None` (COLUMN_BLOCK_UNCLE deleted).
7. `.expect("block uncles must be stored")` panics → process terminates.

No existing guard prevents this: the `is_main_chain` check at line 74 is the only filter, and it affirmatively passes frozen blocks through.

## Impact Explanation
The panic terminates the CKB node process. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."** The crash is deterministic and repeatable — any frozen block hash triggers it. Nodes running both the freezer and the light-client protocol server are directly affected.

## Likelihood Explanation
- The freezer is a supported production opt-in feature; nodes that enable it and serve the light-client protocol are directly vulnerable.
- The attacker requires zero privileges: only a P2P connection and any frozen block hash, which are trivially obtained from any block explorer or by syncing headers.
- A single well-formed `GetBlocksProof` message suffices — no PoW, no key material, no majority hashpower.
- The crash is repeatable: the attacker can re-trigger it after any node restart.

## Recommendation
In `GetBlocksProofProcess::execute`, replace the direct `get_block_uncles` call with the freezer-aware `get_block` path already present in `ChainStore::get_block` (lines 44–54 of `store/src/store.rs`), extracting uncles from the retrieved `BlockView`. Alternatively, add a freezer fallback inside `get_block_uncles` mirroring the pattern in `get_block`. At minimum, replace the `.expect` with a graceful error return so a missing uncle causes the block to be treated as not found rather than crashing the node.

## Proof of Concept
```rust
// Construct a store state matching normal post-freeze state:
//   COLUMN_INDEX[frozen_hash]       = present  (is_main_chain -> true)
//   COLUMN_BLOCK_HEADER[frozen_hash] = present  (get_block_header -> Some)
//   COLUMN_BLOCK_UNCLE[frozen_hash]  = absent   (deleted by delete_block_body)
//
// Send: GetBlocksProof { block_hashes: [frozen_hash], last_hash: current_tip_hash }
//
// Expected (buggy) execution:
//   execute() -> partition -> frozen_hash in `found`
//   -> get_block_header(frozen_hash) -> Some(header)
//   -> get_block_uncles(frozen_hash) -> None
//   -> .expect("block uncles must be stored") -> PANIC -> process exit
//
// This state is produced by the normal freezer lifecycle:
//   freeze() writes block to flat files
//   wipe_out_frozen_data() calls delete_block_body() which deletes COLUMN_BLOCK_UNCLE
//   COLUMN_BLOCK_HEADER and COLUMN_INDEX are left intact ("remain header")
```

### Citations

**File:** store/src/write_batch.rs (L91-119)
```rust
    pub fn delete_block_body(
        &mut self,
        number: BlockNumber,
        hash: &packed::Byte32,
        txs_len: u32,
    ) -> Result<(), Error> {
        self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
        self.inner.delete(COLUMN_BLOCK_EXTENSION, hash.as_slice())?;
        self.inner
            .delete(COLUMN_BLOCK_PROPOSAL_IDS, hash.as_slice())?;
        self.inner.delete(
            COLUMN_NUMBER_HASH,
            packed::NumberHash::new_builder()
                .number(number)
                .block_hash(hash.clone())
                .build()
                .as_slice(),
        )?;

        let key_range = (0u32..txs_len).map(|i| {
            packed::TransactionKey::new_builder()
                .block_hash(hash.clone())
                .index(i)
                .build()
        });

        self.inner.delete_range(COLUMN_BLOCK_BODY, key_range)?;
        Ok(())
    }
```

**File:** shared/src/shared.rs (L220-226)
```rust
        if !frozen.is_empty() {
            // remain header
            for (hash, (number, txs)) in &frozen {
                batch.delete_block_body(*number, hash, *txs).map_err(|e| {
                    ckb_logger::error!("Freezer delete_block_body failed {}", e);
                    e
                })?;
```

**File:** store/src/store.rs (L42-54)
```rust
    fn get_block(&self, h: &packed::Byte32) -> Option<BlockView> {
        let header = self.get_block_header(h)?;
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
        }
```

**File:** store/src/store.rs (L278-281)
```rust
    /// Returns true if the block is on the main chain.
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```
