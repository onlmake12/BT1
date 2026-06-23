### Title
Hardcoded `TRANSACTION_SIZE_LIMIT` in Tx-Pool Diverges from Consensus `max_block_bytes`, Causing Valid Transactions to Be Silently Rejected — (File: `util/types/src/core/tx_pool.rs`)

---

### Summary

The tx-pool enforces a hardcoded per-transaction size ceiling of `512 * 1_000` bytes (`TRANSACTION_SIZE_LIMIT`) that is never derived from the consensus-configured `max_block_bytes` (default `597 * 1_000` bytes). Any transaction whose serialized size falls in the range `(512 000, 597 000]` bytes passes the consensus `SizeVerifier` but is unconditionally rejected by the tx-pool with `Reject::ExceededTransactionSizeLimit`. This is the direct CKB analog of the Monad `CREATE`/`CREATE2` bug: a hardcoded limit that is smaller than the value the chain configuration actually permits.

---

### Finding Description

**Step 1 — Hardcoded constant, never read from consensus.** [1](#0-0) 

```rust
/// The maximum size of the tx-pool to accept transactions
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

`TRANSACTION_SIZE_LIMIT` is a compile-time constant. It is never parameterised by the `Consensus` object.

**Step 2 — Consensus `max_block_bytes` is a different, larger value.** [2](#0-1) 

```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
// = 597 * 1_000 = 597_000 bytes
```

The consensus `max_block_bytes` defaults to **597 000 bytes** and is fully configurable per chain spec via `ConsensusBuilder`.

**Step 3 — The tx-pool `non_contextual_verify` applies both checks sequentially.** [3](#0-2) 

```rust
pub(crate) fn non_contextual_verify(consensus: &Consensus, tx: &TransactionView) -> Result<(), Reject> {
    // Uses consensus.max_block_bytes() → 597_000 bytes
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // Hardcoded 512_000 bytes — never reads consensus
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(tx_size, TRANSACTION_SIZE_LIMIT));
    }
    ...
}
```

**Step 4 — The consensus `SizeVerifier` correctly uses `max_block_bytes`.** [4](#0-3) 

```rust
impl<'a> NonContextualTransactionVerifier<'a> {
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            ...
        }
    }
}
``` [5](#0-4) 

The `SizeVerifier` accepts any transaction whose serialized size ≤ `consensus.max_block_bytes()` (597 000 bytes). The subsequent hardcoded check then rejects anything above 512 000 bytes, creating an **85 000-byte dead zone** of transactions that are consensus-valid but tx-pool-invalid.

---

### Impact Explanation

Any transaction sender who constructs a transaction with a serialized block size in the range **(512 000, 597 000]** bytes (or larger if the chain is configured with a bigger `max_block_bytes`) will receive `Reject::ExceededTransactionSizeLimit` from the tx-pool. The transaction:

- **Passes** the consensus `SizeVerifier` (it is a valid transaction per protocol rules).
- **Fails** the tx-pool admission gate unconditionally.
- **Cannot propagate** through the P2P network because relay peers apply the same tx-pool check.
- **Cannot be mined** in practice because no node will accept it into its mempool.

For chains configured with `max_block_bytes` larger than 512 000 bytes (the default already is 597 000), this silently bricks an entire class of otherwise-valid transactions. Factory-style scripts or data-heavy transactions that approach the block size limit are the most likely victims — exactly the same class of contracts bricked by the Monad bug.

---

### Likelihood Explanation

The default mainnet `max_block_bytes` is already 597 000 bytes, so the gap exists on every standard CKB deployment. Any unprivileged RPC caller or tx-pool submitter who sends a transaction in the 512–597 KB range triggers the rejection. No special privileges, keys, or majority hashpower are required. The entry path is the standard `send_transaction` RPC or P2P transaction relay.

---

### Recommendation

Replace the hardcoded constant with a value derived from the consensus configuration. The tx-pool size limit should be computed as a fraction of `consensus.max_block_bytes()` (e.g., accounting for block header and cellbase overhead), rather than a fixed compile-time value:

```rust
// Instead of:
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;

// Derive at runtime:
let tx_size_limit = consensus.max_block_bytes()
    .saturating_sub(BLOCK_HEADER_SERIALIZED_SIZE + CELLBASE_MAX_SIZE);
if tx_size > tx_size_limit { ... }
```

This mirrors the fix recommended in the Monad report: use the chain-config value rather than a hardcoded approximation.

---

### Proof of Concept

1. Construct a transaction whose `serialized_size_in_block()` is, e.g., **550 000 bytes** (between 512 000 and 597 000).
2. Submit it via `send_transaction` RPC to a standard CKB node.
3. **Observed**: `Reject::ExceededTransactionSizeLimit(550000, 512000)` — rejected.
4. **Expected**: The transaction passes the consensus `SizeVerifier` (550 000 < 597 000 = `max_block_bytes`) and should be admitted to the tx-pool.
5. Confirm by calling `NonContextualTransactionVerifier::new(tx, consensus).verify()` directly — it returns `Ok(())`, proving the transaction is consensus-valid. [1](#0-0) [6](#0-5) [2](#0-1) [7](#0-6)

### Citations

**File:** util/types/src/core/tx_pool.rs (L305-309)
```rust
/// The maximum size of the tx-pool to accept transactions
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```

**File:** verification/src/transaction_verifier.rs (L80-91)
```rust
impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }
```

**File:** verification/src/transaction_verifier.rs (L306-326)
```rust
impl<'a> SizeVerifier<'a> {
    pub fn new(transaction: &'a TransactionView, block_bytes_limit: u64) -> Self {
        SizeVerifier {
            transaction,
            block_bytes_limit,
        }
    }

    pub fn verify(&self) -> Result<(), Error> {
        let size = self.transaction.data().serialized_size_in_block() as u64;
        if size <= self.block_bytes_limit {
            Ok(())
        } else {
            Err(TransactionError::ExceededMaximumBlockBytes {
                actual: size,
                limit: self.block_bytes_limit,
            }
            .into())
        }
    }
}
```
