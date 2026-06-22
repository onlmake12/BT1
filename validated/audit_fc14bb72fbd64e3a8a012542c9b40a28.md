### Title
Static `TRANSACTION_SIZE_LIMIT` Does Not Scale With Consensus `max_block_bytes` — (`util/types/src/core/tx_pool.rs`)

---

### Summary

The tx-pool admission gate uses a hardcoded constant `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes to reject transactions, but this value is never derived from or compared against the consensus-level `max_block_bytes` parameter. Because `max_block_bytes` is a chain-spec-configurable value (default 597,000 bytes), the static threshold creates a permanent mismatch: it is simultaneously too restrictive for the default mainnet (rejecting consensus-valid transactions) and too permissive for any chain spec whose block size limit is below 512 KB (admitting transactions that can never be mined).

---

### Finding Description

`TRANSACTION_SIZE_LIMIT` is defined as a compile-time constant:

```rust
// util/types/src/core/tx_pool.rs:309
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

It is applied unconditionally in `non_contextual_verify`, the first admission check executed for every transaction submitted to the pool:

```rust
// tx-pool/src/util.rs:67-73
let tx_size = tx.data().serialized_size_in_block() as u64;
if tx_size > TRANSACTION_SIZE_LIMIT {
    return Err(Reject::ExceededTransactionSizeLimit(
        tx_size,
        TRANSACTION_SIZE_LIMIT,
    ));
}
```

The consensus-level block size limit is a separate, configurable value:

```rust
// spec/src/consensus.rs:83
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
// = 597 * 1_000 = 597_000 bytes (default)
```

The consensus-level `SizeVerifier` uses the actual `block_bytes_limit` from the chain spec, not the hardcoded constant:

```rust
// verification/src/transaction_verifier.rs:314-325
pub fn verify(&self) -> Result<(), Error> {
    let size = self.transaction.data().serialized_size_in_block() as u64;
    if size <= self.block_bytes_limit { Ok(()) } else { Err(...) }
}
```

The block assembler also derives its limit dynamically from `max_block_bytes`:

```rust
// tx-pool/src/block_assembler/mod.rs:206-208
let txs_size_limit = max_block_bytes
    .checked_sub(basic_size)
    .ok_or(BlockAssemblerError::Overflow)?;
```

The tx-pool admission path (`non_contextual_verify`) is the only place that uses the hardcoded constant instead of the consensus parameter.

---

### Impact Explanation

**Scenario A — Default mainnet (max_block_bytes = 597 KB, TRANSACTION_SIZE_LIMIT = 512 KB):**
Any transaction with serialized size between 512,001 and 597,000 bytes is consensus-valid (it fits in a block) but is permanently rejected by the tx-pool with `ExceededTransactionSizeLimit`. A transaction sender cannot submit such a transaction through the normal RPC path (`send_transaction` / `test_tx_pool_accept`), even though miners could legally include it in a block. The sender has no recourse: the rejection is not a transient condition.

**Scenario B — Chain spec with max_block_bytes < 512 KB:**
The tx-pool accepts transactions up to 512 KB that can never be included in any block. An unprivileged RPC caller can fill the pool with permanently unminable transactions, wasting pool memory and CPU verification cycles. The pool's `max_tx_pool_size` eviction logic will evict legitimate lower-fee transactions to make room for these permanently stuck entries.

---

### Likelihood Explanation

**Scenario A** is triggered on the default mainnet by any RPC caller who submits a transaction whose serialized size falls in the 512–597 KB range. Large transactions with many inputs/outputs or large witness data can reach this range. The condition is deterministic and reproducible.

**Scenario B** applies to any CKB-derived chain (testnet, devnet, or custom deployment) configured with a smaller block size. The CKB codebase explicitly supports per-chain-spec consensus parameters, making this a realistic deployment configuration.

---

### Recommendation

Replace the hardcoded constant with a value derived from the consensus `max_block_bytes`. The `non_contextual_verify` function already receives a reference to `Consensus`; the limit should be computed as:

```rust
let size_limit = consensus.max_block_bytes();
if tx_size > size_limit {
    return Err(Reject::ExceededTransactionSizeLimit(tx_size, size_limit));
}
```

If a safety margin below the full block limit is desired (to account for block header and cellbase overhead), it should be expressed as a ratio of `max_block_bytes` rather than a fixed constant, analogous to the recommendation in the external report to use `totalSupply / CONSTANT_VALUE`.

---

### Proof of Concept

1. Start a CKB node with the default chain spec (`max_block_bytes = 597,000`).
2. Construct a transaction whose serialized size in block is 513,000 bytes (e.g., by adding many outputs or a large witness).
3. Submit via `send_transaction` RPC.
4. Observe rejection: `Transaction size 513000 exceeded maximum limit 512000`.
5. Verify the transaction is consensus-valid by checking `size <= max_block_bytes` (513,000 ≤ 597,000).
6. Confirm no alternative submission path exists: the tx-pool is the only route to get a transaction mined.

**Root cause chain:**
`send_transaction` RPC → `TxPoolController` → `non_contextual_verify` (`tx-pool/src/util.rs:67`) → hardcoded `TRANSACTION_SIZE_LIMIT` (`util/types/src/core/tx_pool.rs:309`) → `Reject::ExceededTransactionSizeLimit`.

---

**Supporting code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** util/types/src/core/tx_pool.rs (L305-309)
```rust
/// The maximum size of the tx-pool to accept transactions
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** tx-pool/src/util.rs (L64-73)
```rust
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
```

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** verification/src/transaction_verifier.rs (L314-325)
```rust
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
```

**File:** tx-pool/src/block_assembler/mod.rs (L206-208)
```rust
            let txs_size_limit = max_block_bytes
                .checked_sub(basic_size)
                .ok_or(BlockAssemblerError::Overflow)?;
```
