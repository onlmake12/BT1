### Title
`get_block_template` RPC Returns Permanently Unimplemented `depends` and `required` Fields in `TransactionTemplate` - (File: tx-pool/src/block_assembler/mod.rs)

### Summary

The `get_block_template` RPC exposes a `TransactionTemplate` type that documents two semantically critical fields — `depends` and `required` — but the block assembler hardcodes both to stub values (`None` / `false`) with explicit `// unimplemented` comments. Any miner or mining-pool software that relies on these fields to perform custom transaction selection will receive permanently incorrect data, causing it to assemble invalid blocks that the CKB node will reject.

### Finding Description

In `tx-pool/src/block_assembler/mod.rs`, the function `tx_entry_to_template` converts a pool entry into the JSON `TransactionTemplate` that is returned to callers of `get_block_template`:

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

The `TransactionTemplate` type in `util/jsonrpc-types/src/block_template.rs` documents these fields with precise, actionable semantics:

- **`required: bool`** — "Whether miner must include this transaction in the new block." The parent `BlockTemplate` doc states: *"Miners must include the transactions marked as `required` in the assembled new block."*
- **`depends: Option<Vec<Uint64>>`** — "Transaction dependencies. This is a hint to help miners selecting transactions. **This transaction can only be committed if its dependencies are also committed in the new block.** This field is a list of indices into the array `transactions` in the block template." [2](#0-1) 

The `BlockTemplate` documentation also explicitly states the RPC is designed to allow miners to remove transactions:

> "Miners can assemble the new block from the template. The RPC is designed to allow miners to remove transactions and adding new transactions to the block." [3](#0-2) 

### Impact Explanation

**`depends: None` (always):** When a miner performs custom transaction selection — removing some transactions from the template — it must know the intra-block dependency graph to avoid including a child transaction whose parent was removed. Without `depends`, a miner cannot determine which transactions are prerequisites for others. If it includes a child while omitting its parent, the CKB node will reject the submitted block with `OutPointError::OutOfOrder` or `OutPointError::Unknown`, as confirmed by the block cell resolution logic and tests: [4](#0-3) 

**`required: false` (always):** The protocol contract states miners must include `required` transactions. Since this is never set, miners have no way to know which transactions are mandatory, breaking the documented protocol guarantee.

The net effect is that the `get_block_template` RPC is not compliant with its own documented interface. Mining software that implements custom transaction selection — a use case the RPC explicitly advertises — cannot function correctly and will produce invalid blocks.

### Likelihood Explanation

The entry path is any caller of the `get_block_template` RPC (`miner-get_block_template`), which is a standard, unprivileged, publicly documented RPC method. Third-party mining pools and custom miners that implement transaction selection logic (e.g., to prioritize high-fee transactions or exclude certain transactions) are directly affected. The CKB miner itself uses all template transactions as-is and is not affected, but the RPC is explicitly designed to support custom selection. [5](#0-4) 

### Recommendation

Implement the `depends` field in `tx_entry_to_template` by computing the set of intra-template dependencies for each transaction. For each `TxEntry`, inspect its inputs and cell-deps; if any reference an output of another transaction already present in the template's `transactions` list, record that transaction's index in `depends`. This requires access to the full list of selected `TxEntry` objects during template serialization.

For `required`, define and document the conditions under which a transaction should be marked required (e.g., transactions that are part of a mandatory package), and populate the field accordingly. [1](#0-0) 

### Proof of Concept

1. Start a CKB node and submit two transactions where `tx_b` spends an output of `tx_a`.
2. After both are proposed and enter the committed window, call `get_block_template`.
3. Observe that both `tx_a` and `tx_b` appear in `template.transactions`, but `tx_b.depends` is `null` and `tx_b.required` is `false`.
4. Build a custom miner that removes `tx_a` from the template (e.g., to save block space) while keeping `tx_b`, since `depends` gave no indication of the dependency.
5. Submit the block via `submit_block`. The CKB node rejects it with an `OutPointError` because `tx_b`'s input references a dead/unknown cell.

The `ProposeOutOfOrder` integration test confirms the node enforces correct transaction ordering in committed blocks: [6](#0-5)

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

**File:** util/jsonrpc-types/src/block_template.rs (L10-13)
```rust
/// A block template for miners.
///
/// Miners optional pick transactions and then assemble the final block.
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
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

**File:** util/types/src/core/tests/cell.rs (L269-309)
```rust
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
```

**File:** rpc/src/module/miner.rs (L26-30)
```rust
    /// Returns block template for miners.
    ///
    /// Miners can assemble the new block from the template. The RPC is designed to allow miners
    /// to remove transactions and adding new transactions to the block.
    ///
```

**File:** test/src/specs/tx_pool/descendant.rs (L97-129)
```rust
pub struct ProposeOutOfOrder;

impl Spec for ProposeOutOfOrder {
    // Case: Even if the proposals is out of order of relatives(child transaction
    //       proposed before its parent transaction), miner commits in order of
    //       relatives
    //   1. Put `tx_family` into pending-pool.
    //   2. Propose `[tx_family.b, tx_family.a]`, then continuously submit blank blocks.
    //   3. Expect committing `[tx_family.a, tx_family.b]`.
    fn run(&self, nodes: &mut Vec<Node>) {
        let node = &nodes[0];
        let window = node.consensus().tx_proposal_window();
        node.mine(window.farthest() + 2);

        // 1. Put `tx_family` into pending-pool.
        let family = prepare_tx_family(node);
        node.submit_transaction(family.a());
        node.submit_transaction(family.b());

        // 2. Propose `[tx_family.b, tx_family.a]`, then continuously submit blank blocks.
        node.submit_block(&propose(node, &[family.b(), family.a()]));
        (0..window.closest()).for_each(|_| {
            node.submit_block(&blank(node)); // continuously submit blank blocks.
        });

        // 3. Expect committing `[tx_family.a, tx_family.b]`.
        let block = node.new_block_with_blocking(|template| template.transactions.len() != 2);
        assert_eq!(
            [family.a().to_owned(), family.b().to_owned()],
            block.transactions()[1..],
        );
        node.submit_block(&block);
    }
```
