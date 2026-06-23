The implementation is fully visible. Let me trace the exact logic.

**Root cause — no index cross-check, and no collision needed**

`verify_transaction_and_witness_proof` at lines 2005–2070 does the following:

1. Builds `transactions_merkle_proof` from `tx_proof.transactions_proof.indices/lemmas`
2. Builds `witnesses_merkle_proof` from `tx_proof.witnesses_proof.indices/lemmas`
3. Calls `CBMT::retrieve_leaves(block.tx_witness_hashes(), &witnesses_merkle_proof)` → gets `witnesses_hashes` at the witness-proof indices
4. Calls `witnesses_merkle_proof.root(&witnesses_hashes)` → reconstructs `witnesses_proof_root`
5. Calls `CBMT::retrieve_leaves(block.tx_hashes(), &transactions_merkle_proof)` → gets `tx_hashes` at the tx-proof indices
6. Calls `transactions_merkle_proof.root(&tx_hashes)` → reconstructs `raw_transactions_root`
7. Checks: `block.transactions_root() == merkle_root(&[raw_transactions_root, witnesses_proof_root])`

The critical property of a CBMT Merkle proof: `proof.root(leaf_values)` always reconstructs the **root of the entire tree**, not a partial root. The lemmas supply all sibling hashes up to the root. So:

- `witnesses_proof_root` = CBMT root of **all** `tx_witness_hashes` (regardless of which index was proven)
- `raw_transactions_root` = CBMT root of **all** `tx_hashes` (regardless of which index was proven)

Therefore `merkle_root(&[raw_transactions_root, witnesses_proof_root])` equals `block.transactions_root()` for **any** valid pair of proofs, even when `transactions_proof.indices = [0]` and `witnesses_proof.indices = [1]`. No hash collision is required. The check passes trivially.

There is **no guard** anywhere in the function that asserts `transactions_proof.indices == witnesses_proof.indices`.

### Title
Missing Index Cross-Validation in `verify_transaction_and_witness_proof` Allows Witness-Transaction Mismatch Proof — (`rpc/src/module/chain.rs`)

---

### Summary

`verify_transaction_and_witness_proof` independently verifies the transaction Merkle proof and the witness Merkle proof, then checks that their reconstructed roots combine to equal `block.transactions_root()`. Because a valid CBMT Merkle proof always reconstructs the **full tree root** regardless of which leaf index it proves, this check passes for any pair of individually-valid proofs — even when `transactions_proof.indices` and `witnesses_proof.indices` point to different transactions. No hash collision is required. The function returns `Ok(tx_hashes)` for a proof that binds tx[i]'s hash to witness[j]'s data.

---

### Finding Description

The block header's `transactions_root` is defined as:

```
transactions_root = merkle_root([raw_transactions_root, witnesses_root])
```

where `raw_transactions_root = CBMT_root(tx_hashes)` and `witnesses_root = CBMT_root(tx_witness_hashes)`. [1](#0-0) 

`verify_transaction_and_witness_proof` builds two independent `MerkleProof` objects and verifies them separately: [2](#0-1) 

It then calls `witnesses_merkle_proof.root(&witnesses_hashes)` and `transactions_merkle_proof.root(&tx_hashes)` to reconstruct the two sub-roots, and checks their combination against `block.transactions_root()`: [3](#0-2) 

The fundamental property of a CBMT Merkle proof is that `proof.root(leaf_values)` reconstructs the **root of the entire tree** by walking up with the supplied lemmas (sibling hashes). The result is the same full-tree root regardless of which leaf index was proven. Therefore:

- `witnesses_proof_root` = CBMT root of **all** `tx_witness_hashes` — identical for any valid witness proof, regardless of index
- `raw_transactions_root` = CBMT root of **all** `tx_hashes` — identical for any valid tx proof, regardless of index

So `merkle_root(&[raw_transactions_root, witnesses_proof_root])` equals `block.transactions_root()` for **any** pair of individually-valid proofs, even with completely mismatched indices. There is no guard asserting `transactions_proof.indices == witnesses_proof.indices` anywhere in the function. [4](#0-3) 

The `get_transaction_and_witness_proof` builder correctly uses the same `leaf_indices` for both proofs: [5](#0-4) 

But the verifier never enforces this invariant on the consumer side.

---

### Impact Explanation

The function returns `Ok(vec![tx_hash_i])` — the hash of the transaction proven by `transactions_proof` — while the `witnesses_proof` actually proves witness[j] for a different transaction j. A light client consuming this RPC for SPV verification receives a success response and the hash of tx[i], but the witness verification it believes was performed actually covers tx[j]'s witness. If tx[i] carries an invalid or absent witness (e.g., a missing lock script signature), an attacker can substitute a valid witness proof from any other transaction in the same block and obtain a false-positive verification result. This enables signature bypass for economic transactions verified by light clients relying on this RPC.

---

### Likelihood Explanation

The attack requires no privileges, no hash collision, no PoW, and no key material. Any caller with RPC access to a node that has a block with at least two transactions can craft the mismatched proof immediately. The only prerequisite is a valid on-chain block, which is always satisfied on mainnet. The exploit is deterministic and locally testable.

---

### Recommendation

Add an explicit index equality check before proceeding with verification:

```rust
if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
    return Err(RPCError::invalid_params(
        "transactions_proof.indices must equal witnesses_proof.indices"
    ));
}
```

This check must be placed before the Merkle proof reconstruction, at the start of the `and_then` closure in `verify_transaction_and_witness_proof`. [6](#0-5) 

---

### Proof of Concept

Given a block B on-chain with transactions `[tx0, tx1, ...]`:

1. Call `get_transaction_and_witness_proof([tx0_hash])` → get `proof_tx0` with `transactions_proof.indices=[i0]` and `witnesses_proof.indices=[i0]`
2. Call `get_transaction_and_witness_proof([tx1_hash])` → get `proof_tx1` with `transactions_proof.indices=[i1]` and `witnesses_proof.indices=[i1]`
3. Craft a mismatched proof:
   ```json
   {
     "block_hash": "<B hash>",
     "transactions_proof": proof_tx0.transactions_proof,
     "witnesses_proof":    proof_tx1.witnesses_proof
   }
   ```
4. Call `verify_transaction_and_witness_proof(mismatched_proof)`
5. **Expected (correct):** error — indices mismatch
6. **Actual:** `Ok([tx0_hash])` — verification succeeds; the returned hash is tx0's but the witness proof covers tx1's witness

### Citations

**File:** util/types/src/core/views.rs (L762-777)
```rust
    pub fn calc_transactions_root(&self) -> packed::Byte32 {
        merkle_root(&[
            self.calc_raw_transactions_root(),
            self.calc_witnesses_root(),
        ])
    }

    /// Calculates the merkle root for transactions without witnesses.
    pub fn calc_raw_transactions_root(&self) -> packed::Byte32 {
        merkle_root(&self.tx_hashes[..])
    }

    /// Calculates the merkle root for transaction witnesses.
    pub fn calc_witnesses_root(&self) -> packed::Byte32 {
        merkle_root(&self.tx_witness_hashes[..])
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

**File:** rpc/src/module/chain.rs (L2015-2043)
```rust
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
```

**File:** rpc/src/module/chain.rs (L2045-2065)
```rust
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
```
