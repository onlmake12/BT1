### Title
Missing Index Equality Check in `verify_transaction_and_witness_proof` Allows Mismatched Transaction/Witness Proof Acceptance — (`rpc/src/module/chain.rs`)

### Summary

`verify_transaction_and_witness_proof` independently verifies `transactions_proof` and `witnesses_proof` against the block's respective leaf arrays, then checks only that the two computed roots combine to equal `block.transactions_root()`. Because a valid CBMT Merkle proof for **any** index in a tree always produces the same full-tree root (regardless of which leaf index was proved), an attacker can supply `transactions_proof.indices = [i]` and `witnesses_proof.indices = [j]` (where `i ≠ j`), both with correct lemmas, and the final root check passes unconditionally — **no hash collision required**. The function returns `Ok([tx_hashes[i]])`, falsely asserting that transaction `i`'s witness is committed alongside its transaction hash.

### Finding Description

The block's `transactions_root` is constructed as:

```
transactions_root = merkle_root([
    CBMT_root(all_tx_hashes),        // raw_transactions_root
    CBMT_root(all_tx_witness_hashes) // witnesses_root
])
``` [1](#0-0) 

`verify_transaction_and_witness_proof` builds two independent `MerkleProof` objects from the caller-supplied indices and lemmas: [2](#0-1) 

It then computes each root independently:

```
witnesses_proof_root  = witnesses_merkle_proof.root(retrieve_leaves(block.tx_witness_hashes(), witnesses_proof))
raw_transactions_root = transactions_merkle_proof.root(retrieve_leaves(block.tx_hashes(), transactions_proof))
``` [3](#0-2) 

The final guard is:

```rust
block.transactions_root() == merkle_root(&[raw_transactions_root, witnesses_proof_root])
``` [4](#0-3) 

**There is no check that `transactions_proof.indices == witnesses_proof.indices`.**

A CBMT Merkle proof for leaf `i` with correct sibling lemmas always reconstructs the root of the **entire** tree — the root is a global property, not a per-leaf property. Therefore:

- A valid proof for `tx[0]` in the transaction tree → `raw_transactions_root` = full tx CBMT root ✓  
- A valid proof for `witness[1]` in the witness tree → `witnesses_proof_root` = full witness CBMT root ✓  
- `merkle_root([full_tx_root, full_witness_root])` = `block.transactions_root()` ✓

The check passes, and the function returns `[tx_hashes[0]]` — claiming that transaction 0's witness is verified, when only transaction 1's witness was actually proved.

### Impact Explanation

Light clients using this RPC for SPV verification receive a false guarantee: the returned transaction hash is presented as having both its raw transaction hash **and** its witness hash committed in the block, but the witness commitment actually belongs to a different transaction index. Any consumer that relies on this RPC to confirm witness inclusion (e.g., to confirm a specific signature set is committed) can be fed a mismatched proof that passes verification. The function's stated contract — "verifies that a proof points to transactions in a block" with witness binding — is violated without any cryptographic hardness assumption. [5](#0-4) 

### Likelihood Explanation

The attack requires only:
1. Any valid block on-chain (trivially satisfied on mainnet/testnet).
2. Unprivileged RPC access (the endpoint is public).
3. Constructing two valid CBMT proofs for different indices in the same block — a straightforward operation using `get_transaction_and_witness_proof` for two different transactions and then swapping the `witnesses_proof` field.

No hash collision, no privileged access, no PoW, and no special network position is required.

### Recommendation

Add an explicit index-equality guard before the root check:

```rust
if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
    return Err(RPCError::invalid_params(
        "transactions_proof and witnesses_proof must cover the same indices"
    ));
}
```

This must be checked on the **raw index values** before constructing the `MerkleProof` objects, since the indices drive which leaves are retrieved from the block's respective hash arrays. [6](#0-5) 

### Proof of Concept

```
1. Mine a block containing at least two transactions: tx[0] and tx[1].
2. Call get_transaction_and_witness_proof([tx0_hash]) → proof_A
   (transactions_proof.indices=[0], witnesses_proof.indices=[0])
3. Call get_transaction_and_witness_proof([tx1_hash]) → proof_B
   (transactions_proof.indices=[1], witnesses_proof.indices=[1])
4. Craft a malicious proof:
   {
     block_hash: <same block>,
     transactions_proof: proof_A.transactions_proof,  // indices=[0], proves tx[0]
     witnesses_proof:    proof_B.witnesses_proof       // indices=[1], proves witness[1]
   }
5. Call verify_transaction_and_witness_proof(malicious_proof).
6. Observe: returns Ok([tx0_hash]) — tx[0]'s hash is returned as "verified"
   even though the witness proof covers tx[1], not tx[0].
7. Assert: the function MUST return Err when
   transactions_proof.indices != witnesses_proof.indices.
   This assertion fails on the current code, confirming the bug.
```

### Citations

**File:** util/types/src/extension.rs (L38-40)
```rust
        let raw_transactions_root = merkle_root(tx_hashes);
        let witnesses_root = merkle_root(tx_witness_hashes);
        let transactions_root = merkle_root(&[raw_transactions_root, witnesses_root]);
```

**File:** rpc/src/module/chain.rs (L1158-1162)
```rust
    /// Verifies that a proof points to transactions in a block, returning the transaction hashes it commits to.
    ///
    /// ## Parameters
    ///
    /// * `tx_proof` - proof generated by [`get_transaction_and_witness_proof`](#tymethod.get_transaction_and_witness_proof).
```

**File:** rpc/src/module/chain.rs (L2005-2043)
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
```

**File:** rpc/src/module/chain.rs (L2045-2056)
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
```
