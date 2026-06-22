### Title
`TransactionTemplate.depends` and `TransactionTemplate.required` Are Permanently Unimplemented in Block Assembler — (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

The `get_block_template` RPC returns `TransactionTemplate` objects whose `depends` and `required` fields are explicitly hardcoded to `None` and `false` respectively, with inline `// unimplemented` comments. Both fields are fully documented in the JSON-RPC spec and the type definition as semantically meaningful signals that miners **must** respect. Because they are never populated, any miner that selects a subset of the provided transactions risks assembling an invalid block, wasting proof-of-work.

---

### Finding Description

In `tx-pool/src/block_assembler/mod.rs`, the function `tx_entry_to_template` converts a pool entry into the JSON-RPC `TransactionTemplate`: [1](#0-0) 

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

The `TransactionTemplate` type documents both fields with precise semantics: [2](#0-1) 

- **`required: bool`** — "Whether miner must include this transaction in the new block."
- **`depends: Option<Vec<Uint64>>`** — "This transaction can only be committed if its dependencies are also committed in the new block. This field is a list of indices into the array `transactions` in the block template."

The RPC README reinforces this contract: [3](#0-2) 

> "Miners must include the transactions marked as `required` in the assembled new block."

Because the block assembler packages transactions from the mempool in topological order, the template may contain child transactions that spend outputs of earlier transactions in the same template. Without `depends` being populated, a miner that selects a subset of the template (e.g., to stay within `bytes_limit` or `cycles_limit`) has no way to know which transactions must be co-included. Including a child without its parent produces an invalid block.

---

### Impact Explanation

A miner calling `get_block_template` via RPC receives a list of `TransactionTemplate` entries where `depends` is always `None`. If the miner's software attempts to trim the transaction list (a normal optimization to respect size/cycle limits), it cannot determine which transactions have intra-block dependencies. Selecting a child transaction without its parent causes the assembled block to reference a non-existent cell, making the block invalid. The node will reject the submitted block, and the miner's proof-of-work is wasted. Additionally, since `required` is always `false`, any future protocol-level requirement to mandate specific transactions in a block cannot be communicated to miners through this interface.

---

### Likelihood Explanation

Any miner or mining pool operator using the standard `get_block_template` RPC endpoint is affected. This is the primary interface for block assembly in CKB. Miners that naively include all template transactions in order are unaffected, but miners that implement transaction selection logic (a common optimization) will silently receive broken dependency information. The entry path requires only an unprivileged RPC call to `get_block_template`.

---

### Recommendation

Populate `depends` in `tx_entry_to_template` by computing, for each transaction in the assembled template, the set of indices of other template transactions whose outputs it spends. Populate `required` according to whatever protocol-level criteria mandate inclusion. At minimum, remove the `// unimplemented` stubs and either implement the fields or document that they are intentionally unused with a rationale.

---

### Proof of Concept

1. Call `get_block_template` on a node with a mempool containing a transaction chain: `tx_parent → tx_child` (child spends an output of parent).
2. Observe that both entries appear in `transactions[]` with `depends: null` and `required: false`.
3. Write a miner that selects only `tx_child` (e.g., it has higher fee density).
4. Submit the block. The node rejects it because `tx_child` references a live cell that was not consumed in the same block.
5. The miner's PoW is wasted with no protocol-level warning that the selection was invalid.

The root cause is the hardcoded stub at: [1](#0-0)

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

**File:** rpc/README.md (L5829-5832)
```markdown
* `transactions`: `Array<` [`TransactionTemplate`](#type-transactiontemplate) `>` - Provided valid transactions which can be committed in the new block.

    Miners must include the transactions marked as `required` in the assembled new block.

```
