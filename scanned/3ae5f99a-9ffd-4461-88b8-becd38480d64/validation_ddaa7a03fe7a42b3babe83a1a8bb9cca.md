### Title
Missing Implementation of `depends` Field in `TransactionTemplate` Causes Invalid Block Assembly for Miners Doing Custom Transaction Selection - (File: tx-pool/src/block_assembler/mod.rs)

### Summary
The `tx_entry_to_template` function in `tx-pool/src/block_assembler/mod.rs` always sets `depends: None` and `required: false` with explicit `// unimplemented` comments. The `depends` field is a documented, critical hint that tells miners which transactions in the block template depend on other transactions in the same template. Without it, miners who perform custom transaction selection — a use case explicitly supported and documented by the `get_block_template` RPC — will produce invalid blocks when dependent transactions are present, causing their submitted blocks to be rejected by the network and wasting PoW effort.

### Finding Description

The `get_block_template` RPC is explicitly designed to allow miners to remove or add transactions:

> "A miner gets a template from CKB, optionally selects transactions, resolves the PoW puzzle, and submits the found new block."

The `TransactionTemplate` type includes a `depends` field documented as:

> "Transaction dependencies. This is a hint to help miners selecting transactions. This transaction can only be committed if its dependencies are also committed in the new block."

However, in `tx_entry_to_template`, this field is hardcoded to `None`:

```rust
pub(crate) fn tx_entry_to_template(entry: &TxEntry) -> TransactionTemplate {
    TransactionTemplate {
        hash: entry.transaction().hash().into(),
        required: false, // unimplemented
        cycles: Some(entry.cycles.into()),
        depends: None, // unimplemented
        data: entry.transaction().data().into(),
    }
}
``` [1](#0-0) 

The `depends` field is defined in `util/jsonrpc-types/src/block_template.rs` as:

```rust
pub depends: Option<Vec<Uint64>>,
``` [2](#0-1) 

CKB enforces strict transaction ordering within a block: if transaction B spends an output of transaction A, A must appear before B. This is validated at block resolution time via `BlockCellProvider`, which returns `OutPointError::OutOfOrder` if a transaction references an output from a later transaction in the same block. [3](#0-2) 

When a miner removes transaction A from the template but keeps transaction B (which depends on A), the submitted block is invalid and rejected. Without the `depends` field being populated, the miner has no way to know about this dependency relationship.

### Impact Explanation

**Impact: Low**

Any miner implementing custom transaction selection using the `get_block_template` RPC — as explicitly documented and supported — will produce invalid blocks when the template contains transactions with intra-block dependencies. The submitted block is rejected by the network, wasting the miner's PoW computation. There is no consensus harm to the network itself, but miners suffer economic loss (wasted hashrate) and transactions may be delayed. The `get_block_template` documentation explicitly states miners can remove transactions, making this a realistic operational failure mode. [4](#0-3) 

### Likelihood Explanation

**Likelihood: Medium**

The `get_block_template` RPC is explicitly designed for miners to perform custom transaction selection. Any miner that implements transaction filtering or prioritization logic (e.g., to optimize fee revenue, exclude low-fee transactions, or stay within size/cycle limits) will encounter this issue whenever the tx-pool contains chains of dependent transactions — a common occurrence in normal network operation. The `// unimplemented` comment confirms this is a known gap, not an edge case. [5](#0-4) 

### Recommendation

Implement the `depends` field in `tx_entry_to_template`. For each transaction entry, inspect its inputs and cell deps to determine if any of them reference outputs from other transactions in the same block template. If so, populate `depends` with the indices of those transactions in the `transactions` array. This mirrors the intent described in the `TransactionTemplate` documentation and is necessary for miners to safely perform custom transaction selection. [6](#0-5) 

### Proof of Concept

1. Submit two transactions to the tx-pool: `tx_parent` (spends a confirmed cell) and `tx_child` (spends an output of `tx_parent`). Both are proposed and enter the commitment window.
2. Call `get_block_template`. Both transactions appear in `template.transactions`. The `depends` field for `tx_child` is `null` (not `[0]` as it should be).
3. A miner implementing custom selection removes `tx_parent` (e.g., it has a lower fee rate) but keeps `tx_child`.
4. The miner assembles the block and calls `submit_block`.
5. The node rejects the block with `OutPointError::OutOfOrder` because `tx_child` references an output of `tx_parent` which is not present in the block.
6. The miner's PoW work is wasted.

The test `resolve_transaction_should_reject_incorrect_order_txs` in `util/types/src/core/tests/cell.rs` confirms that out-of-order transactions within a block are rejected at the `BlockCellProvider` level. [7](#0-6)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L985-993)
```rust
pub(crate) fn tx_entry_to_template(entry: &TxEntry) -> TransactionTemplate {
    TransactionTemplate {
        hash: entry.transaction().hash().into(),
        required: false, // unimplemented
        cycles: Some(entry.cycles.into()),
        depends: None, // unimplemented
        data: entry.transaction().data().into(),
    }
}
```

**File:** util/jsonrpc-types/src/block_template.rs (L228-255)
```rust
/// Transaction template which is ready to be committed in the new block.
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct TransactionTemplate {
    /// Transaction hash.
    pub hash: H256,
    /// Whether miner must include this transaction in the new block.
    pub required: bool,
    /// The hint of how many cycles this transaction consumes.
    ///
    /// Miners can utilize this field to ensure that the total cycles do not
    /// exceed the limit while selecting transactions.
    pub cycles: Option<Cycle>,
    /// Transaction dependencies.
    ///
    /// This is a hint to help miners selecting transactions.
    ///
    /// This transaction can only be committed if its dependencies are also committed in the new block.
    ///
    /// This field is a list of indices into the array `transactions` in the block template.
    ///
    /// For example, `depends = [1, 2]` means this transaction depends on
    /// `block_template.transactions[1]` and `block_template.transactions[2]`.
    pub depends: Option<Vec<Uint64>>,
    /// The transaction.
    ///
    /// Miners must keep it unchanged when including it in the new block.
    pub data: Transaction,
}
```

**File:** util/types/src/core/tests/cell.rs (L268-330)
```rust
#[test]
fn resolve_transaction_should_reject_incorrect_order_txs() {
    let out_point = OutPoint::new(h256!("0x2").into(), 3);

    let tx1 = TransactionBuilder::default()
        .input(CellInput::new(out_point, 0))
        .output(
            CellOutput::new_builder()
                .capacity(capacity_bytes!(2))
                .build(),
        )
        .output_data(packed::Bytes::default())
        .build();

    let tx2 = TransactionBuilder::default()
        .input(CellInput::new(OutPoint::new(tx1.hash(), 0), 0))
        .build();

    let dep = CellDep::new_builder()
        .out_point(OutPoint::new(tx1.hash(), 0))
        .build();
    let tx3 = TransactionBuilder::default().cell_dep(dep).build();

    // tx1 <- tx2
    // ok
    {
        let block = generate_block(vec![tx1.clone(), tx2.clone()]);
        let provider = BlockCellProvider::new(&block);
        assert!(provider.is_ok());
    }

    // tx1 -> tx2
    // resolve err
    {
        let block = generate_block(vec![tx2, tx1.clone()]);
        let provider = BlockCellProvider::new(&block);

        assert_error_eq!(
            provider.err().unwrap(),
            OutPointError::OutOfOrder(OutPoint::new(tx1.hash(), 0)),
        );
    }

    // tx1 <- tx3
    // ok
    {
        let block = generate_block(vec![tx1.clone(), tx3.clone()]);
        let provider = BlockCellProvider::new(&block);

        assert!(provider.is_ok());
    }

    // tx1 -> tx3
    // resolve err
    {
        let block = generate_block(vec![tx3, tx1.clone()]);
        let provider = BlockCellProvider::new(&block);

        assert_error_eq!(
            provider.err().unwrap(),
            OutPointError::OutOfOrder(OutPoint::new(tx1.hash(), 0)),
        );
    }
```

**File:** rpc/src/module/miner.rs (L19-30)
```rust
/// RPC Module Miner for miners.
///
/// A miner gets a template from CKB, optionally selects transactions, resolves the PoW puzzle, and
/// submits the found new block.
#[rpc(openrpc)]
#[async_trait]
pub trait MinerRpc {
    /// Returns block template for miners.
    ///
    /// Miners can assemble the new block from the template. The RPC is designed to allow miners
    /// to remove transactions and adding new transactions to the block.
    ///
```
