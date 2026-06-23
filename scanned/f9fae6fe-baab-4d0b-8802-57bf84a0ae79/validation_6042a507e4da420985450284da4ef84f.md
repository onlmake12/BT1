Based on my analysis of the production code, here is my assessment:

---

### Title
Missing Index-Set Equality Check in `verify_transaction_and_witness_proof` Allows Mismatched Proof Acceptance — (`rpc/src/module/chain.rs`)

### Summary

`verify_transaction_and_witness_proof` independently verifies a `transactions_proof` and a `witnesses_proof` against the block's `transactions_root`, but never checks that both proofs reference the **same set of leaf indices**. Because each partial Merkle proof, when supplied with correct lemmas, reconstructs the full tree root regardless of which leaf it covers, an attacker can supply a `transactions_proof` for tx[0] and a `witnesses_proof` for tx[1], and the final root check still passes.

### Finding Description

The block `transactions_root` is defined as:

```
transactions_root = merkle_root([raw_transactions_root, witnesses_root])
```

where `raw_transactions_root` is the CBMT root over all tx hashes and `witnesses_root` is the CBMT root over all witness hashes. [1](#0-0) 

`verify_transaction_and_witness_proof` constructs two independent `MerkleProof` objects from the caller-supplied indices and lemmas, then:

1. Calls `CBMT::retrieve_leaves(block.tx_witness_hashes(), &witnesses_merkle_proof)` → retrieves witness hashes at `witnesses_proof.indices`.
2. Calls `witnesses_merkle_proof.root(witnesses_hashes)` → reconstructs `witnesses_proof_root`.
3. Calls `CBMT::retrieve_leaves(block.tx_hashes(), &transactions_merkle_proof)` → retrieves tx hashes at `transactions_proof.indices`.
4. Calls `transactions_merkle_proof.root(tx_hashes)` → reconstructs `raw_transactions_root`.
5. Checks `block.transactions_root() == merkle_root([raw_transactions_root, witnesses_proof_root])`. [2](#0-1) 

**There is no check that `transactions_proof.indices == witnesses_proof.indices`.**

Because a valid CBMT proof for any single leaf, when given correct sibling lemmas, reconstructs the **full tree root** (not a partial root), both `raw_transactions_root` and `witnesses_proof_root` will independently equal the block's full `raw_transactions_root` and `witnesses_root` respectively — even when they cover different leaves. The combined `merkle_root([raw_transactions_root, witnesses_proof_root])` therefore equals `block.transactions_root()` unconditionally, and the check passes.

The function then returns `tx_hashes` from the `transactions_proof` (e.g., tx[0]'s hash), falsely asserting that tx[0]'s witness commitment is proven, when in fact the `witnesses_proof` committed to tx[1]'s witness. [3](#0-2) 

### Impact Explanation

Any client relying on `verify_transaction_and_witness_proof` to confirm that a specific transaction's witness is committed to in a block can be deceived. The RPC returns `Ok([tx[0].hash])`, implying tx[0]'s witness is proven, while the witness proof actually covers tx[1]. This breaks the semantic guarantee of the API and can mislead light clients, wallets, or cross-chain bridges that use this endpoint for witness inclusion verification.

### Likelihood Explanation

The attack requires only:
1. Any known block with ≥ 2 transactions (trivially available on mainnet).
2. Constructing two valid CBMT proofs for different leaf indices — a straightforward computation using the public block data.
3. Submitting via the unauthenticated `verify_transaction_and_witness_proof` RPC.

No privileged access, no PoW, no key material is needed.

### Recommendation

Add an explicit check that both proof index sets are identical before proceeding:

```rust
if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
    return Err(RPCError::invalid_params(
        "transactions_proof and witnesses_proof must cover the same indices",
    ));
}
``` [4](#0-3) 

### Proof of Concept

1. Take any block `B` on the chain with transactions `[tx0, tx1, ...]`.
2. Build `transactions_proof` = valid CBMT proof for `tx0.hash()` at index 0 (with correct lemmas from the raw-tx tree).
3. Build `witnesses_proof` = valid CBMT proof for `tx1.witness_hash()` at index 1 (with correct lemmas from the witness tree).
4. Call `verify_transaction_and_witness_proof({ block_hash: B.hash, transactions_proof, witnesses_proof })`.
5. Observe: the RPC returns `Ok([tx0.hash])` — success — even though the witness proof covers tx1, not tx0.

The `get_transaction_and_witness_proof` builder always uses the same `leaf_indices` for both proofs, confirming the intended invariant: [5](#0-4) 

The verifier never enforces this invariant, making the mismatch exploitable.

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

**File:** rpc/src/module/chain.rs (L2045-2057)
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
```

**File:** rpc/src/module/chain.rs (L2058-2058)
```rust
                                            Some(tx_hashes.iter().map(|hash| hash.into()).collect())
```
