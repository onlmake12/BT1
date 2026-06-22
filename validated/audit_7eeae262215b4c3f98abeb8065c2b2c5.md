### Title
Light-Client Peer Can Panic Node via `GetTransactionsProof` on Frozen-Block Transactions — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

An unprivileged light-client peer can crash a CKB node by sending a `GetTransactionsProof` message containing a `tx_hash` that belongs to a **frozen block**. The freezer migration deletes `COLUMN_BLOCK_UNCLE` for frozen blocks but preserves `COLUMN_TRANSACTION_INFO`, breaking the invariant assumed by the `.expect("block uncles must be stored")` at line 119–121. The result is an unrecoverable `panic!` in the node process.

---

### Finding Description

**The `.expect()` at line 119–121:** [1](#0-0) 

```rust
let uncles = snapshot
    .get_block_uncles(&block_hash)
    .expect("block uncles must be stored");
```

**Why `get_block()` at line 83 does NOT panic first for frozen blocks:**

`get_block()` in `store/src/store.rs` takes an early-return path for frozen blocks, bypassing its own internal `get_block_uncles()` call: [2](#0-1) 

```rust
if let Some(freezer) = self.freezer()
    && header.number() > 0
    && header.number() < freezer.number()
{
    let raw_block = freezer.retrieve(header.number())...;
    ...
    return Some(raw_block_reader.to_entity().into_view()); // early return
}
// The .expect("block uncles must be stored") below is SKIPPED for frozen blocks
let uncles = self.get_block_uncles(h).expect("block uncles must be stored");
``` [3](#0-2) 

So for a frozen block, `get_block()` at line 83 succeeds (returns the block from the freezer), and execution reaches line 119.

**Why `COLUMN_BLOCK_UNCLE` is absent for frozen blocks:**

`delete_block_body()` in `StoreWriteBatch` explicitly deletes `COLUMN_BLOCK_UNCLE` as part of the freezer migration: [4](#0-3) 

```rust
self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
```

**Why `COLUMN_TRANSACTION_INFO` is NOT deleted:**

`delete_block_body()` does not touch `COLUMN_TRANSACTION_INFO`: [5](#0-4) 

So after freezer migration, for any frozen block:
- `COLUMN_TRANSACTION_INFO` → **present** (tx lookup succeeds, `is_main_chain` returns true)
- `COLUMN_BLOCK_UNCLE` → **deleted** (line 119 returns `None` → `.expect()` panics)

**`get_block_uncles()` always reads from `COLUMN_BLOCK_UNCLE`**, regardless of whether the block is frozen: [6](#0-5) 

The `StoreSnapshot` carries an `Option<Freezer>` field, so this code path is active on any node with a freezer configured: [7](#0-6) 

---

### Impact Explanation

An unprivileged light-client peer can crash the node process with a single crafted `GetTransactionsProof` message. The panic is unrecoverable and terminates the node. This is a remote denial-of-service against any CKB node running the light-client protocol server with a freezer configured.

---

### Likelihood Explanation

- All historical `tx_hash` values are publicly available on-chain; the attacker needs no special knowledge.
- The freezer is a standard feature of long-running CKB nodes.
- The message requires no authentication or privilege.
- The only guard is the `GET_TRANSACTIONS_PROOF_LIMIT` count check and the `is_main_chain` check — neither prevents this path. [8](#0-7) 

---

### Recommendation

At line 119–121, replace `.expect()` with a graceful fallback. For frozen blocks, the uncles are already embedded in the freezer's raw block data (retrieved at line 83). The fix should extract uncles from the already-retrieved `block` object instead of re-querying `COLUMN_BLOCK_UNCLE`:

```rust
// Instead of:
let uncles = snapshot.get_block_uncles(&block_hash)
    .expect("block uncles must be stored");

// Use:
let uncles = block.uncles();
```

This is consistent with how `get_block()` itself handles frozen blocks — it reconstructs the full `BlockView` (including uncles) from the freezer, so `block.uncles()` is already correct and available.

---

### Proof of Concept

1. Configure a CKB node with a freezer (standard long-running node).
2. Wait for blocks to be frozen (or use an existing node where freezer threshold has been crossed).
3. Obtain any `tx_hash` from a frozen block (from a public block explorer).
4. Connect as a light-client peer and send:
   ```
   GetTransactionsProof {
       last_hash: <any recent main-chain block hash>,
       tx_hashes: [<tx_hash from frozen block>],
   }
   ```
5. The node panics at `get_transactions_proof.rs:121` with `"block uncles must be stored"`.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-64)
```rust
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }

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

        let (found, missing): (Vec<_>, Vec<_>) = self
            .message
            .tx_hashes()
            .to_entity()
            .into_iter()
            .partition(|tx_hash| {
                snapshot
                    .get_transaction_info(tx_hash)
                    .map(|tx_info| snapshot.is_main_chain(&tx_info.block_hash))
                    .unwrap_or_default()
            });
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L119-121)
```rust
            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
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

**File:** store/src/store.rs (L55-58)
```rust
        let body = self.get_block_body(h);
        let uncles = self
            .get_block_uncles(h)
            .expect("block uncles must be stored");
```

**File:** store/src/store.rs (L205-215)
```rust
    fn get_block_uncles(&self, hash: &packed::Byte32) -> Option<UncleBlockVecView> {
        if let Some(cache) = self.cache()
            && let Some(data) = cache.block_uncles.lock().get(hash)
        {
            return Some(data.clone());
        };

        let ret = self.get(COLUMN_BLOCK_UNCLE, hash.as_slice()).map(|slice| {
            let reader = packed::UncleBlockVecViewReader::from_slice_should_be_ok(slice.as_ref());
            Into::<UncleBlockVecView>::into(reader)
        });
```

**File:** store/src/write_batch.rs (L91-118)
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
```

**File:** store/src/snapshot.rs (L12-25)
```rust
pub struct StoreSnapshot {
    pub(crate) inner: RocksDBSnapshot,
    pub(crate) freezer: Option<Freezer>,
    pub(crate) cache: Arc<StoreCache>,
}

impl ChainStore for StoreSnapshot {
    fn cache(&self) -> Option<&StoreCache> {
        Some(&self.cache)
    }

    fn freezer(&self) -> Option<&Freezer> {
        self.freezer.as_ref()
    }
```
