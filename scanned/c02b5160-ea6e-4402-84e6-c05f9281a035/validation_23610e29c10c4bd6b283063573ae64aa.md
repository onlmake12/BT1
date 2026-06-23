### Title
Hardcoded `TRANSACTION_SIZE_LIMIT` in Tx-Pool Does Not Track Configurable `max_block_bytes` Consensus Parameter — (`util/types/src/core/tx_pool.rs`)

---

### Summary

The tx-pool enforces a hardcoded per-transaction size ceiling (`TRANSACTION_SIZE_LIMIT = 512,000` bytes) that is independent of the consensus-configurable `max_block_bytes` parameter. Because the default `max_block_bytes` (597,000 bytes) already exceeds `TRANSACTION_SIZE_LIMIT`, any transaction sender submitting a transaction between 512,001 and 597,000 bytes will be permanently rejected by the tx-pool even though the transaction is fully valid per consensus rules. If `max_block_bytes` is ever increased further (e.g., via a hard fork or custom chain spec), the gap widens and more valid transactions become unreachable.

---

### Finding Description

`TRANSACTION_SIZE_LIMIT` is a compile-time constant defined in `util/types/src/core/tx_pool.rs`:

```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;  // 512,000 bytes
```

The consensus-configurable block size limit is:

```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
// = 597 * 1_000 = 597,000 bytes (default, but configurable via chain spec)
```

In `tx-pool/src/util.rs`, `non_contextual_verify` runs two sequential size checks:

1. `NonContextualTransactionVerifier::new(tx, consensus).verify()` — internally calls `SizeVerifier::new(tx, consensus.max_block_bytes())`, which rejects transactions larger than `consensus.max_block_bytes()`.
2. A separate hardcoded check: `if tx_size > TRANSACTION_SIZE_LIMIT { return Err(Reject::ExceededTransactionSizeLimit(...)) }`.

Because `MAX_BLOCK_BYTES (597,000) > TRANSACTION_SIZE_LIMIT (512,000)`, step 1 passes for transactions up to 597,000 bytes, but step 2 rejects any transaction larger than 512,000 bytes. Transactions in the range `[512,001, 597,000]` bytes are valid per consensus but are permanently blocked from entering the tx-pool.

The `NonContextualTransactionVerifier` used in step 1 correctly reads `consensus.max_block_bytes()` dynamically:

```rust
size: SizeVerifier::new(tx, consensus.max_block_bytes()),
```

But the second check uses the hardcoded constant that never adapts to the consensus parameter.

---

### Impact Explanation

Any transaction sender who constructs a transaction with serialized size between 512,001 and `consensus.max_block_bytes()` bytes will receive a `PoolRejectedTransactionBySizeLimit` error from the tx-pool, even though the transaction would pass all consensus validation checks and could be included in a block. The transaction cannot be submitted via RPC (`send_transaction`) or relayed via P2P. This is a permanent denial of service on a class of valid transactions. If `max_block_bytes` is increased via a hard fork, the affected range grows proportionally.

---

### Likelihood Explanation

This condition is already active on mainnet: the default `max_block_bytes = 597,000 > TRANSACTION_SIZE_LIMIT = 512,000`. No special configuration or privileged access is required. Any unprivileged transaction sender who constructs a transaction in the affected size range will trigger the rejection. The likelihood is **high** because the gap exists by default and requires no attacker action beyond submitting a large but consensus-valid transaction.

---

### Recommendation

Replace the hardcoded `TRANSACTION_SIZE_LIMIT` check in `non_contextual_verify` with a value derived from `consensus.max_block_bytes()`. If a conservative margin below the block limit is desired (to avoid transactions that are "too close" to the block limit), compute it as a fraction of `consensus.max_block_bytes()` rather than a fixed constant. For example:

```rust
let effective_limit = consensus.max_block_bytes() * 85 / 100; // 85% of block limit
if tx_size > effective_limit {
    return Err(Reject::ExceededTransactionSizeLimit(tx_size, effective_limit));
}
```

This ensures the tx-pool limit always tracks the consensus parameter and does not silently reject valid transactions.

---

### Proof of Concept

1. Default `max_block_bytes = 597,000` bytes. [1](#0-0) 

2. Hardcoded `TRANSACTION_SIZE_LIMIT = 512,000` bytes — a separate, non-consensus-derived constant. [2](#0-1) 

3. `non_contextual_verify` first runs `NonContextualTransactionVerifier` (which uses `consensus.max_block_bytes()` = 597,000), then applies the hardcoded check. [3](#0-2) 

4. `SizeVerifier` inside `NonContextualTransactionVerifier` uses the consensus value, not the hardcoded constant. [4](#0-3) 

5. A transaction of size 550,000 bytes (valid per consensus, `550,000 ≤ 597,000`) passes step 1 but is rejected at step 2 (`550,000 > 512,000`), returning `Reject::ExceededTransactionSizeLimit(550000, 512000)` to the caller via RPC or P2P relay. [5](#0-4)

### Citations

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** util/types/src/core/tx_pool.rs (L305-309)
```rust
/// The maximum size of the tx-pool to accept transactions
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
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

**File:** verification/src/transaction_verifier.rs (L82-91)
```rust
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
