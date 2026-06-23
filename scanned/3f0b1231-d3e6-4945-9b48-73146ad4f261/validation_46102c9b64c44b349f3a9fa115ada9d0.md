### Title
Missing Index-Parity Check in `verify_transaction_and_witness_proof` Allows Cross-Linked Tx/Witness Proof Acceptance — (`rpc/src/module/chain.rs`)

---

### Summary

`verify_transaction_and_witness_proof` independently verifies the tx-tree proof and the witness-tree proof against the block's `transactions_root`, but never asserts that both proofs reference the **same** leaf index. Because the block's `transactions_root` is structurally `merkle_root([raw_tx_root, witnesses_root])`, any pair of individually-valid proofs — even for different positions — will satisfy the final equality check. The function therefore accepts a crafted `TransactionAndWitnessProof` where `transactions_proof.indices = [i]` and `witnesses_proof.indices = [j]` (i ≠ j) and returns `tx_hash[i]` as "verified", falsely binding tx[i] to witness[j].

---

### Finding Description

**Block `transactions_root` structure** (from `util/types/src/core/views.rs`):

```
transactions_root = merkle_root([
    CBMT_root(all_tx_hashes),       // raw_transactions_root
    CBMT_root(all_witness_hashes),  // witnesses_root
])
``` [1](#0-0) 

**Verification logic** in `verify_transaction_and_witness_proof` (`rpc/src/module/chain.rs` lines 2045–2065):

1. `witnesses_proof_root` ← `witnesses_merkle_proof.root([witness_hash[j]])` with valid lemmas for position j → equals the block's correct `witnesses_root`.
2. `raw_transactions_root` ← `transactions_merkle_proof.root([tx_hash[i]])` with valid lemmas for position i → equals the block's correct `raw_transactions_root`.
3. Final check: `block.transactions_root() == merkle_root([raw_transactions_root, witnesses_proof_root])`. [2](#0-1) 

Because both intermediate roots are independently correct (each proof carries valid sibling lemmas for its own position), the final equality is satisfied **regardless of whether i == j**. There is no guard comparing `transactions_proof.indices` against `witnesses_proof.indices`. [3](#0-2) 

The `get_transaction_and_witness_proof` builder always uses the **same** `leaf_indices` for both sub-proofs, so legitimate proofs always have matching indices. The verifier, however, never enforces this invariant. [4](#0-3) 

---

### Impact Explanation

Any unprivileged caller with RPC access can submit a `TransactionAndWitnessProof` where `transactions_proof.indices = [i]` and `witnesses_proof.indices = [j]` (i ≠ j), each with correct lemmas for their respective positions. The function returns `Ok([tx_hash[i]])`, signalling that tx[i] is proven together with its witness — but the witness proof actually covers tx[j]. A light client or any consumer that relies on this RPC to confirm a transaction-witness binding will accept a false pairing.

---

### Likelihood Explanation

The RPC endpoint is publicly accessible on any node that exposes the chain RPC. Constructing the crafted proof requires only two legitimate CBMT proofs from the same block (obtainable via `get_transaction_and_witness_proof` for each transaction separately). No privileged access, key material, or hashpower is needed.

---

### Recommendation

Add an explicit index-parity check before the root comparison:

```rust
// Reject immediately if the two proof index sets differ
if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
    return Err(RPCError::invalid_params(
        "transactions_proof and witnesses_proof must reference the same indices",
    ));
}
```

This mirrors the invariant already enforced by `get_transaction_and_witness_proof`, which always builds both sub-proofs from the same `leaf_indices`. [4](#0-3) 

---

### Proof of Concept

For a block containing at least two transactions (tx[0], tx[1]):

1. Call `get_transaction_and_witness_proof([tx_hash[0]])` → extract `transactions_proof` (indices=[0], lemmas for position 0).
2. Call `get_transaction_and_witness_proof([tx_hash[1]])` → extract `witnesses_proof` (indices=[1], lemmas for position 1).
3. Submit to `verify_transaction_and_witness_proof`:
   ```json
   {
     "block_hash": "<block_hash>",
     "transactions_proof": { "indices": ["0x0"], "lemmas": [...lemmas_for_tx0...] },
     "witnesses_proof":    { "indices": ["0x1"], "lemmas": [...lemmas_for_witness1...] }
   }
   ```
4. **Expected (correct) behaviour**: error — indices mismatch.
5. **Actual behaviour**: returns `[tx_hash[0]]` — accepted as valid, falsely binding tx[0] to witness[1].

The root check at line 2052–2056 passes because `raw_transactions_root` (from tx[0]'s proof) and `witnesses_proof_root` (from witness[1]'s proof) are both individually correct CBMT roots, and their combination equals `block.transactions_root()` by construction. [5](#0-4)

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

**File:** rpc/src/module/chain.rs (L2016-2043)
```rust
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
