### Title
Inconsistent Transaction Size Validation Between tx-pool and Block Verifier — Denial of Service for Large Consensus-Valid Transactions - (File: `tx-pool/src/util.rs`)

### Summary

The tx-pool's `non_contextual_verify` function imposes a hard `TRANSACTION_SIZE_LIMIT` of 512 KB that the consensus-level `NonContextualTransactionVerifier` does not enforce. Transactions whose serialized size falls between 512 KB and `max_block_bytes` are valid by consensus and can be committed in a block, but are unconditionally rejected by the tx-pool with `Reject::ExceededTransactionSizeLimit`. This mirrors the original report's pattern: a wrapper component adds a restriction the underlying component does not have, blocking a legitimate operation.

### Finding Description

**Component A — tx-pool (`tx-pool/src/util.rs`, `non_contextual_verify`, lines 56–83):**

```rust
let tx_size = tx.data().serialized_size_in_block() as u64;
if tx_size > TRANSACTION_SIZE_LIMIT {          // 512 * 1_000 bytes
    return Err(Reject::ExceededTransactionSizeLimit(tx_size, TRANSACTION_SIZE_LIMIT));
}
``` [1](#0-0) 

The constant is defined as:

```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
``` [2](#0-1) 

This check runs for every transaction entering the pool — via `send_transaction` RPC, relay from a peer, or `test_tx_pool_accept`.

**Component B — consensus verifier (`verification/src/transaction_verifier.rs`, `NonContextualTransactionVerifier`, lines 80–102):**

```rust
NonContextualTransactionVerifier {
    size: SizeVerifier::new(tx, consensus.max_block_bytes()),
    ...
}
``` [3](#0-2) 

`SizeVerifier` only checks `tx_size <= block_bytes_limit` (i.e., `max_block_bytes`), which is larger than 512 KB. There is no `TRANSACTION_SIZE_LIMIT` check here. [4](#0-3) 

The code comment in the tx-pool explicitly acknowledges the gap:

> "The ckb consensus does not limit the size of a single transaction, but if the size of the transaction is close to the limit of the block, it may cause the transaction to fail to be packed" [5](#0-4) 

**Secondary inconsistency — `is_malformed_tx()` / `is_allowed_relay()`:**

`Reject::ExceededTransactionSizeLimit` is not classified as a malformed transaction:

```rust
pub fn is_malformed_tx(&self) -> bool {
    match self {
        Reject::Malformed(_, _) => true,
        Reject::DeclaredWrongCycles(..) => true,
        Reject::Verification(err) => is_malformed_from_verification(err),
        Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
        _ => false,   // ExceededTransactionSizeLimit falls here
    }
}
``` [6](#0-5) 

Because `is_malformed_tx()` returns `false`, `is_allowed_relay()` returns `true` for this rejection:

```rust
pub fn is_allowed_relay(&self) -> bool {
    matches!(self, Reject::DeclaredWrongCycles(..))
        || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
}
``` [7](#0-6) 

This means a peer sending a transaction that exceeds the tx-pool's size limit is never banned, even though the tx-pool will always reject it.

### Impact Explanation

Any unprivileged RPC caller or transaction relay peer who submits a transaction whose serialized size is between 512 KB and `max_block_bytes` will receive `PoolRejectedTransactionBySizeLimit (-1110)` unconditionally, even though the transaction is fully valid by consensus and could be committed in a block. The transaction cannot enter the pool, cannot be relayed to miners through the normal path, and cannot be proposed or committed via the standard flow. A miner would have to include it out-of-band, bypassing the tx-pool entirely — an option unavailable to ordinary users.

The secondary inconsistency means a peer can repeatedly submit oversized transactions without being banned, since the rejection is not treated as evidence of a malformed transaction.

### Likelihood Explanation

Transactions approaching 512 KB are uncommon in practice, but they are reachable: a script author or integrator building a transaction with many inputs, large witnesses, or substantial cell data can legitimately produce one. The RPC entry point (`send_transaction`) is fully reachable by any unprivileged caller. No special role or key is required.

### Recommendation

1. **Align the limit or document the gap explicitly.** If the intent is to keep the tx-pool conservative, the `TRANSACTION_SIZE_LIMIT` should be documented as a deliberate policy divergence from consensus, and the RPC error message should explain that the transaction may still be mined directly.

2. **Classify `ExceededTransactionSizeLimit` as malformed.** Since the tx-pool will always reject transactions above this limit regardless of context, `is_malformed_tx()` should return `true` for `Reject::ExceededTransactionSizeLimit`. This would prevent peers from repeatedly submitting oversized transactions without penalty and would stop the node from treating such transactions as relay-eligible.

### Proof of Concept

1. Craft a transaction whose `serialized_size_in_block()` is, e.g., 520,000 bytes (> 512,000 but ≤ `max_block_bytes`).
2. Submit via `send_transaction` RPC.
3. Observe `PoolRejectedTransactionBySizeLimit (-1110)`.
4. Verify the transaction passes `NonContextualTransactionVerifier` (consensus size check) by constructing a block containing it and running `BlockVerifier` — it will succeed.
5. Observe that the submitting peer is not banned (`is_malformed_tx()` = false) and the rejection is marked `is_allowed_relay()` = true, confirming the inconsistency.

### Citations

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

**File:** util/types/src/core/tx_pool.rs (L87-97)
```rust
impl Reject {
    /// Returns true if the reject reason is malformed tx.
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L110-113)
```rust
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
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

**File:** verification/src/transaction_verifier.rs (L306-325)
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
```
