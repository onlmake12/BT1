### Title
Mismatched Proof Indices in `verify_transaction_and_witness_proof` Allow Witness-Transaction Decoupling — (`File: rpc/src/module/chain.rs`)

---

### Summary

`verify_transaction_and_witness_proof` accepts two independent Merkle proofs — `transactions_proof` and `witnesses_proof` — each carrying their own `indices` field. The function verifies each proof independently against the block's data and checks that the combined Merkle root matches the block header's `transactions_root`, but **never checks that `transactions_proof.indices` equals `witnesses_proof.indices`**. An unprivileged RPC caller can craft a proof where the transactions sub-proof covers tx[i] and the witnesses sub-proof covers witness[j] (i ≠ j), causing the function to return tx[i]'s hash as if it has been jointly proven with witness[j]'s content.

---

### Finding Description

CKB's block header commits to both transactions and their witnesses via:

```
transactions_root = merkle_root([raw_transactions_root, witnesses_root])
```

The RPC method `verify_transaction_and_witness_proof` is designed to let a caller (typically a light client) prove that specific transactions, together with their witnesses, are included in a committed block. It accepts a `TransactionAndWitnessProof` struct: [1](#0-0) 

The implementation builds two independent `MerkleProof` objects from the caller-supplied `indices` and `lemmas`: [2](#0-1) 

It then verifies each proof against the block independently and checks the combined root: [3](#0-2) 

The combined root check passes as long as each sub-proof is individually valid against the block — it does **not** require that the two proofs cover the same leaf positions. The generator `get_transaction_and_witness_proof` always uses the same `leaf_indices` for both proofs: [4](#0-3) 

But the verifier imposes no such constraint on the caller-supplied proof.

---

### Impact Explanation

Any consumer of `verify_transaction_and_witness_proof` that relies on the result to conclude "transaction T was committed with witness W" can be deceived. Concretely:

- A light client verifying that a transaction carries a valid authorization witness (e.g., a lock-script unlock signature embedded in the witness) can be shown a proof where the transactions sub-proof covers the target tx and the witnesses sub-proof covers a different tx's witness.
- The function returns the target tx's hash with a success result, falsely implying the witness was jointly proven.
- This breaks the security guarantee that `transactions_root` is supposed to provide: binding each transaction to its witness at a specific index.

---

### Likelihood Explanation

The attack requires only:
1. A block with at least two transactions (any non-empty block has a cellbase plus at least one regular tx).
2. The ability to call the public JSON-RPC endpoint `verify_transaction_and_witness_proof` with crafted `indices` fields.

No privileged access, key material, or majority hashpower is needed. Any unprivileged RPC caller can construct the mismatched proof from publicly available block data.

---

### Recommendation

Add an explicit check that the two proof index sets are identical before proceeding:

```rust
fn verify_transaction_and_witness_proof(
    &self,
    tx_proof: TransactionAndWitnessProof,
) -> Result<Vec<H256>> {
    // Enforce that both proofs cover the same leaf positions.
    if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
        return Err(RPCError::invalid_params(
            "transactions_proof and witnesses_proof must have identical indices",
        ));
    }
    // ... existing verification logic ...
}
```

This mirrors the generator's invariant — `get_transaction_and_witness_proof` always builds both proofs from the same `leaf_indices` — and closes the gap in the verifier. [5](#0-4) 

---

### Proof of Concept

Given a block `B` in the canonical chain containing at least two transactions `tx[0]` (cellbase) and `tx[1]`:

1. Call `get_transaction_and_witness_proof(["<hash of tx[1]>"])` to obtain a well-formed proof with `transactions_proof.indices = [1]` and `witnesses_proof.indices = [1]`.

2. Craft a malicious `TransactionAndWitnessProof`:
   - Keep `transactions_proof` as-is (indices = [1], proves tx[1]).
   - Replace `witnesses_proof` with a fresh proof built for index 0 (the cellbase witness): `witnesses_proof.indices = [0]`, with lemmas recomputed from the block's witness CBMT.

3. Call `verify_transaction_and_witness_proof` with the crafted proof.

4. The function computes:
   - `raw_transactions_root` from the transactions sub-proof (valid, covers tx[1]).
   - `witnesses_proof_root` from the witnesses sub-proof (valid, covers witness[0]).
   - `merkle_root([raw_transactions_root, witnesses_proof_root])` — this equals `block.transactions_root()` because both individual CBMT roots are correct roots of their respective full trees.

5. The function returns `[hash_of_tx1]` with `Ok`, falsely implying tx[1] has been jointly proven with the cellbase's witness, not its own. [6](#0-5)

### Citations

**File:** util/jsonrpc-types/src/blockchain.rs (L1366-1375)
```rust
/// Merkle proof for transactions' witnesses in a block.
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct TransactionAndWitnessProof {
    /// Block hash
    pub block_hash: H256,
    /// Merkle proof of all transactions' hash
    pub transactions_proof: MerkleProof,
    /// Merkle proof of transactions' witnesses
    pub witnesses_proof: MerkleProof,
}
```

**File:** rpc/src/module/chain.rs (L1986-2002)
```rust
        let (block, leaf_indices) = self.get_tx_indices(tx_hashes, block_hash)?;
        Ok(TransactionAndWitnessProof {
            block_hash: block.hash().into(),
            transactions_proof: CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &leaf_indices,
            )
            .expect("build proof with verified inputs should be OK")
            .into(),
            witnesses_proof: CBMT::build_merkle_proof(block.tx_witness_hashes(), &leaf_indices)
                .expect("build proof with verified inputs should be OK")
                .into(),
        })
```

**File:** rpc/src/module/chain.rs (L2005-2070)
```rust
    fn verify_transaction_and_witness_proof(
        &self,
        tx_proof: TransactionAndWitnessProof,
    ) -> Result<Vec<H256>> {
        let snapshot = self.shared.snapshot();
        snapshot
            .get_block(&(&tx_proof.block_hash).into())
            .ok_or_else(|| {
                RPCError::invalid_params(format!("Cannot find block {:#x}", tx_proof.block_hash))
            })
            .and_then(|block| {
                let transactions_merkle_proof = MerkleProof::new(
                    tx_proof
                        .transactions_proof
                        .indices
                        .into_iter()
                        .map(|index| index.value())
                        .collect(),
                    tx_proof
                        .transactions_proof
                        .lemmas
                        .into_iter()
                        .map(|lemma| lemma.into())
                        .collect(),
                );
                let witnesses_merkle_proof = MerkleProof::new(
                    tx_proof
                        .witnesses_proof
                        .indices
                        .into_iter()
                        .map(|index| index.value())
                        .collect(),
                    tx_proof
                        .witnesses_proof
                        .lemmas
                        .into_iter()
                        .map(|lemma| lemma.into())
                        .collect(),
                );

                CBMT::retrieve_leaves(block.tx_witness_hashes(), &witnesses_merkle_proof)
                    .and_then(|witnesses_hashes| witnesses_merkle_proof.root(&witnesses_hashes))
                    .and_then(|witnesses_proof_root| {
                        CBMT::retrieve_leaves(block.tx_hashes(), &transactions_merkle_proof)
                            .and_then(|tx_hashes| {
                                transactions_merkle_proof.root(&tx_hashes).and_then(
                                    |raw_transactions_root| {
                                        if block.transactions_root()
                                            == merkle_root(&[
                                                raw_transactions_root,
                                                witnesses_proof_root,
                                            ])
                                        {
                                            Some(tx_hashes.iter().map(|hash| hash.into()).collect())
                                        } else {
                                            None
                                        }
                                    },
                                )
                            })
                    })
                    .ok_or_else(|| {
                        RPCError::invalid_params("Invalid transaction_and_witness proof")
                    })
            })
    }
```
