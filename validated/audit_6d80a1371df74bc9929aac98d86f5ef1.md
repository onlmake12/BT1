### Title
Unchecked `None` Dereference in `get_block_filter` RPC Handler Panics When Filter Hash Is Missing — (`rpc/src/module/chain.rs`)

### Summary

The `get_block_filter` RPC handler calls `store.get_block_filter()` (which returns `Option<packed::Bytes>`) and, when it returns `Some(data)`, immediately calls `store.get_block_filter_hash().expect("stored filter hash")` without checking whether the hash column is actually populated. If the two RocksDB columns (`COLUMN_BLOCK_FILTER` and `COLUMN_BLOCK_FILTER_HASH`) are ever out of sync — a realistic condition after a partial migration — any RPC caller can trigger a panic in the handler by supplying the hash of an affected block.

### Finding Description

`get_block_filter` in `rpc/src/module/chain.rs` reads filter data and filter hash from two separate RocksDB columns:

```rust
fn get_block_filter(&self, block_hash: H256) -> Result<Option<BlockFilter>> {
    let store = self.shared.store();
    let block_hash = block_hash.into();
    if !store.is_main_chain(&block_hash) {
        return Ok(None);
    }
    Ok(store.get_block_filter(&block_hash).map(|data| {
        let hash = store
            .get_block_filter_hash(&block_hash)
            .expect("stored filter hash");   // ← panics if None
        BlockFilter {
            data: data.into(),
            hash: hash.into(),
        }
    }))
}
``` [1](#0-0) 

`get_block_filter` reads from `COLUMN_BLOCK_FILTER` and `get_block_filter_hash` reads from `COLUMN_BLOCK_FILTER_HASH` — two independent columns: [2](#0-1) 

Under normal operation both columns are written atomically inside `insert_block_filter`: [3](#0-2) 

However, the `COLUMN_BLOCK_FILTER_HASH` column was added after `COLUMN_BLOCK_FILTER`. The migration `AddBlockFilterHash` backfills it in batches of 10 000 blocks per RocksDB commit: [4](#0-3) 

If the node crashes between two batch commits during this migration, blocks in the uncommitted batch will have filter data in `COLUMN_BLOCK_FILTER` but no entry in `COLUMN_BLOCK_FILTER_HASH`. After restart, if the migration framework considers the migration complete (or if the DB is otherwise inconsistent), those blocks permanently lack a filter hash. Any subsequent `get_block_filter` RPC call for one of those block hashes will reach the `.expect("stored filter hash")` and panic.

The same inconsistency can arise from any other DB-level corruption that leaves the two columns out of sync.

### Impact Explanation

A panic inside the RPC handler propagates through the `jsonrpc-core` task. Depending on whether the runtime catches the unwind, the outcome is either:

- **Node crash / RPC service termination** — if the panic is not caught, the thread or async task terminates, potentially taking down the RPC service or the entire node process.
- **Internal server error returned to caller** — if the panic is caught by the framework, the caller receives a 500-equivalent error, but repeated calls keep triggering the panic, constituting a denial-of-service against the RPC endpoint.

Either outcome is reachable by any unprivileged RPC caller who knows (or guesses) a block hash that is on the main chain and has filter data but no filter hash.

### Likelihood Explanation

The precondition — `COLUMN_BLOCK_FILTER` populated but `COLUMN_BLOCK_FILTER_HASH` absent — is realistic for any node that:

1. Ran an older CKB version that wrote filter data before the hash column existed, then upgraded.
2. Experienced a crash or power loss during the `AddBlockFilterHash` migration between batch commits.

The migration commits every 10 000 blocks, so a crash at any point during a large backfill leaves a window of up to 10 000 blocks in the inconsistent state. An attacker who can observe which blocks a node has filter data for (e.g., via the P2P filter protocol) can identify the affected range and craft the triggering RPC call.

### Recommendation

Replace the `.expect()` with a proper error return so the RPC handler degrades gracefully instead of panicking:

```rust
Ok(store.get_block_filter(&block_hash).map(|data| {
    let hash = store
        .get_block_filter_hash(&block_hash)
        .ok_or_else(|| RPCError::custom(
            RPCError::ChainIndexIsInconsistent,
            format!("filter hash missing for block {block_hash:#x}"),
        ))?;
    Ok(BlockFilter {
        data: data.into(),
        hash: hash.into(),
    })
}).transpose()?)
```

Additionally, the `AddBlockFilterHash` migration should be made idempotent and resumable so that a crash mid-migration does not leave the DB in a permanently inconsistent state.

### Proof of Concept

1. Identify a node that ran the `AddBlockFilterHash` migration and crashed between batch commits (or simulate by manually writing a filter entry to `COLUMN_BLOCK_FILTER` for a main-chain block without a corresponding `COLUMN_BLOCK_FILTER_HASH` entry).
2. Confirm the block is on the main chain (`is_main_chain` returns true).
3. Send an RPC call: `{"method":"get_block_filter","params":["<affected_block_hash>"],"id":1,"jsonrpc":"2.0"}`.
4. The handler reaches `get_block_filter_hash(...).expect("stored filter hash")`, finds `None`, and panics — crashing the handler or returning an internal server error.

### Citations

**File:** rpc/src/module/chain.rs (L1732-1747)
```rust
    fn get_block_filter(&self, block_hash: H256) -> Result<Option<BlockFilter>> {
        let store = self.shared.store();
        let block_hash = block_hash.into();
        if !store.is_main_chain(&block_hash) {
            return Ok(None);
        }
        Ok(store.get_block_filter(&block_hash).map(|data| {
            let hash = store
                .get_block_filter_hash(&block_hash)
                .expect("stored filter hash");
            BlockFilter {
                data: data.into(),
                hash: hash.into(),
            }
        }))
    }
```

**File:** store/src/store.rs (L485-495)
```rust
    /// Gets block filter data by block hash
    fn get_block_filter(&self, hash: &packed::Byte32) -> Option<packed::Bytes> {
        self.get(COLUMN_BLOCK_FILTER, hash.as_slice())
            .map(|slice| packed::BytesReader::from_slice_should_be_ok(slice.as_ref()).to_entity())
    }

    /// Gets block filter hash by block hash
    fn get_block_filter_hash(&self, hash: &packed::Byte32) -> Option<packed::Byte32> {
        self.get(COLUMN_BLOCK_FILTER_HASH, hash.as_slice())
            .map(|slice| packed::Byte32Reader::from_slice_should_be_ok(slice.as_ref()).to_entity())
    }
```

**File:** store/src/transaction.rs (L392-414)
```rust
    pub fn insert_block_filter(
        &self,
        block_hash: &packed::Byte32,
        filter_data: &packed::Bytes,
        parent_block_filter_hash: &packed::Byte32,
    ) -> Result<(), Error> {
        self.insert_raw(
            COLUMN_BLOCK_FILTER,
            block_hash.as_slice(),
            filter_data.as_slice(),
        )?;
        let current_block_filter_hash = calc_filter_hash(parent_block_filter_hash, filter_data);
        self.insert_raw(
            COLUMN_BLOCK_FILTER_HASH,
            block_hash.as_slice(),
            current_block_filter_hash.as_slice(),
        )?;
        self.insert_raw(
            COLUMN_META,
            META_LATEST_BUILT_FILTER_DATA_KEY,
            block_hash.as_slice(),
        )
    }
```

**File:** util/migrate/src/migrations/add_block_filter_hash.rs (L53-87)
```rust
            let mut block_number = 0;
            let mut parent_block_filter_hash = [0u8; 32];
            loop {
                let db_txn = chain_db.db().transaction();
                for _ in 0..10000 {
                    if block_number > latest_built_filter_data_block_number {
                        break;
                    }
                    let block_hash = chain_db.get_block_hash(block_number).expect("index stored");
                    let filter_data = chain_db
                        .get_block_filter(&block_hash)
                        .expect("filter data stored");
                    parent_block_filter_hash = blake2b_256(
                        [
                            parent_block_filter_hash.as_slice(),
                            filter_data.calc_raw_data_hash().as_slice(),
                        ]
                        .concat(),
                    );
                    db_txn
                        .put(
                            COLUMN_BLOCK_FILTER_HASH,
                            block_hash.as_slice(),
                            parent_block_filter_hash.as_slice(),
                        )
                        .expect("db transaction put should be ok");
                    pbi.inc(1);
                    block_number += 1;
                }
                db_txn.commit()?;

                if block_number > latest_built_filter_data_block_number {
                    break;
                }
            }
```
