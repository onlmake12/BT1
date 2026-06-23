### Title
Missing Index Consistency Check Between `transactions_proof` and `witnesses_proof` in `verify_transaction_and_witness_proof` — (File: `rpc/src/module/chain.rs`)

---

### Summary

The `verify_transaction_and_witness_proof` RPC function does not check that `transactions_proof.indices` and `witnesses_proof.indices` are identical. Because the two Merkle proofs are verified independently against the same block root, a caller can supply mismatched index sets — proving transaction T_i via the transactions proof while proving witness W_j (j ≠ i) via the witnesses proof — and the function will still return T_i's hash as "proven with witnesses," even though T_i's witness was never actually proven.

---

### Finding Description

`verify_transaction_and_witness_proof` in `rpc/src/module/chain.rs` accepts a `TransactionAndWitnessProof` containing two independent Merkle proofs:

- `transactions_proof` (indices + lemmas) — proved against `block.tx_hashes()`
- `witnesses_proof` (indices + lemmas) — proved against `block.tx_witness_hashes()` [1](#0-0) 

The function verifies each proof independently and then checks only that the combined root matches `block.transactions_root()`: [2](#0-1) 

There is **no check** that `transactions_proof.indices == witnesses_proof.indices`. The `get_transaction_and_witness_proof` builder always uses the same `leaf_indices` for both proofs: [3](#0-2) 

But the verifier imposes no such constraint on the caller-supplied proof. Because CKB's `transactions_root` is `merkle_root([raw_tx_root, witnesses_root])`, any two valid sub-proofs — even for different transaction positions — will produce the correct combined root, so the final equality check passes regardless of the index mismatch.

The `TransactionAndWitnessProof` type is defined as two independent `MerkleProof` structs with no cross-field invariant: [4](#0-3) 

---

### Impact Explanation

An RPC caller can craft a `TransactionAndWitnessProof` where `transactions_proof.indices = [i]` and `witnesses_proof.indices = [j]` with `i ≠ j`. The function will:

1. Prove that transaction T_i is in the block (via `transactions_proof`).
2. Prove that witness W_j is in the block (via `witnesses_proof`).
3. Return `[T_i.hash]` as "proven with witnesses."

T_i's own witness W_i is never proven. Any application or light client that calls this RPC to confirm that a specific transaction's witness is committed to in a block will receive a false positive. This is directly analogous to the reference bug: just as unset `merkleRoot` entries default to `bytes32(0)` and bypass the whitelist, here the missing index-consistency check allows the witness proof to silently cover a *different* transaction than the one returned.

---

### Likelihood Explanation

The `verify_transaction_and_witness_proof` RPC is publicly accessible to any local or supported RPC caller without privilege. Crafting a valid mismatched proof requires only knowledge of a block's transaction and witness Merkle trees, which are fully public on-chain data. No special access, key material, or majority hash power is needed. [5](#0-4) 

---

### Recommendation

Add an explicit check at the start of `verify_transaction_and_witness_proof` that the two index arrays are identical before proceeding:

```rust
if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
    return Err(RPCError::invalid_params(
        "transactions_proof.indices must equal witnesses_proof.indices"
    ));
}
```

This mirrors the fix recommended in the reference report (`require(merkleRoot_.length == tiers.length && merkleRoot_.length == externalURI_.length)`): enforce that the two parallel arrays are consistent before using them.

---

### Proof of Concept

Given a block B with two transactions `[T0, T1]` and witnesses `[W0, W1]`:

1. Build a valid `transactions_proof` for index `1` (proving T1 is in `raw_tx_root`).
2. Build a valid `witnesses_proof` for index `0` (proving W0 is in `witnesses_root`).
3. Call:
```json
{
  "method": "verify_transaction_and_witness_proof",
  "params": [{
    "block_hash": "<hash of B>",
    "transactions_proof": { "indices": ["0x1"], "lemmas": ["<T0.hash>"] },
    "witnesses_proof":    { "indices": ["0x0"], "lemmas": ["<W1.hash>"] }
  }]
}
```
4. The function computes `raw_transactions_root` from the transactions proof (valid, covers T1) and `witnesses_proof_root` from the witnesses proof (valid, covers W0). Their combination equals `block.transactions_root()`, so the check at line 2052 passes.
5. The function returns `[T1.hash]` — asserting T1 is "proven with witnesses" — even though T1's witness W1 was never included in the witnesses proof. [6](#0-5)

### Citations

**File:** rpc/src/module/chain.rs (L1202-1206)
```rust
    #[rpc(name = "verify_transaction_and_witness_proof")]
    fn verify_transaction_and_witness_proof(
        &self,
        tx_proof: TransactionAndWitnessProof,
    ) -> Result<Vec<H256>>;
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
