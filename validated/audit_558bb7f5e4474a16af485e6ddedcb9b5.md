### Title
Missing Main-Chain Validation in `verify_transaction_proof` Allows Fork-Block Proofs to Pass — (File: `rpc/src/module/chain.rs`)

### Summary

`verify_transaction_proof` accepts a caller-supplied `block_hash` and performs Merkle proof verification against the block it finds in the store, but never checks whether that block is on the canonical (main) chain. An unprivileged RPC caller can supply the hash of a fork/orphan block, construct a valid Merkle proof for a transaction inside it, and receive a successful verification response — making the node falsely attest that the transaction is confirmed.

### Finding Description

`verify_transaction_proof` retrieves the block using `snapshot.get_block()` with the caller-supplied `block_hash`:

```rust
snapshot
    .get_block(&(&tx_proof.block_hash).into())
    .ok_or_else(|| { ... })
    .and_then(|block| {
        let witnesses_root = tx_proof.witnesses_root.into();
        ...
        if block.transactions_root()
            == merkle_root(&[raw_transactions_root, witnesses_root])
        {
            Some(tx_hashes.iter().map(|hash| hash.into()).collect())
        } else {
            None
        }
    })
``` [1](#0-0) 

`snapshot.get_block()` returns any block stored in the database — including fork/orphan blocks — without any main-chain guard. The `block_hash` field of `TransactionProof` is fully attacker-controlled and is never validated against `snapshot.is_main_chain()`.

Compare this with `get_block_by_hash`, which correctly gates on `is_main_chain` before returning data:

```rust
fn get_block_by_hash(...) {
    if !snapshot.is_main_chain(block_hash) {
        return Ok(None);
    }
    ...
}
``` [2](#0-1) 

The `TransactionProof` struct exposes `block_hash` and `witnesses_root` as caller-supplied fields: [3](#0-2) 

The `witnesses_root` field is also taken directly from the caller (`tx_proof.witnesses_root.into()`) and used as a free variable in the equation `block.transactions_root() == merkle_root([raw_transactions_root, witnesses_root])`. Because `block.transactions_root()` is fixed by the (attacker-chosen) fork block, and `raw_transactions_root` is computed from the proof, the `witnesses_root` value is implicitly constrained — but only within the context of the attacker-chosen fork block, not the canonical chain. The root cause is the missing `is_main_chain` guard on `block_hash`.

The analog to the reported `MerkleDistributor.sol` issue is direct: just as `rewardToken` was accepted from the caller without being bound to the Merkle commitment, `block_hash` is accepted from the caller without being bound to the canonical chain, allowing the attacker to substitute a fork block as the proof context.

### Impact Explanation

Any application, exchange, payment processor, or light-client integration that calls `verify_transaction_proof` to confirm that a transaction is settled on the canonical CKB chain can be deceived. An attacker who controls a fork block (e.g., from a recent chain reorganization, or by submitting a valid but non-winning block) can:

1. Obtain the fork block hash.
2. Build a valid CBMT Merkle proof for a transaction inside that fork block (the proof math is correct — the transaction really is in that block).
3. Call `verify_transaction_proof` with the fork block hash and the crafted proof.
4. Receive a successful `200 OK` response listing the transaction hash as "verified."

The node's own RPC endpoint thus falsely attests that a transaction is confirmed when it is not on the canonical chain. This enables double-spend fraud against any off-chain system that trusts this RPC for finality decisions.

### Likelihood Explanation

- The RPC is publicly accessible to any local or remote RPC caller (no authentication required by default).
- Fork blocks are routinely stored in the CKB database during normal operation (every chain reorganization leaves orphan blocks).
- Constructing a valid Merkle proof for a transaction in a known fork block requires only the block's transaction list, which is public.
- No privileged access, key material, or majority hash power is needed.

### Recommendation

Add an `is_main_chain` check on the caller-supplied `block_hash` before proceeding with proof verification in both `verify_transaction_proof` and `verify_transaction_and_witness_proof`:

```rust
fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
    let snapshot = self.shared.snapshot();
    let block_hash: packed::Byte32 = (&tx_proof.block_hash).into();

    // Reject proofs against non-canonical blocks
    if !snapshot.is_main_chain(&block_hash) {
        return Err(RPCError::invalid_params(
            format!("Block {:#x} is not on the main chain", tx_proof.block_hash)
        ));
    }

    snapshot
        .get_block(&block_hash)
        ...
}
```

Apply the same fix to `verify_transaction_and_witness_proof`. [4](#0-3) 

### Proof of Concept

1. Mine a chain of length N on a CKB devnet node.
2. Cause a chain reorganization so that block B (at height K) becomes a fork/orphan block. Block B contains transaction T.
3. Call `get_transaction_proof` against a node that still has block B in its store (or manually construct the proof using block B's transaction list and CBMT).
4. Call `verify_transaction_proof` with `block_hash = B.hash`, the Merkle proof for T, and `witnesses_root = B.calc_witnesses_root()`.
5. Observe that the RPC returns `[T.hash]` — a successful verification — even though T is not on the canonical chain.
6. An application trusting this response would incorrectly treat T as confirmed. [5](#0-4) [6](#0-5)

### Citations

**File:** rpc/src/module/chain.rs (L1938-1979)
```rust
    fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
        let snapshot = self.shared.snapshot();

        snapshot
            .get_block(&(&tx_proof.block_hash).into())
            .ok_or_else(|| {
                RPCError::invalid_params(format!("Cannot find block {:#x}", tx_proof.block_hash))
            })
            .and_then(|block| {
                let witnesses_root = tx_proof.witnesses_root.into();
                let merkle_proof = MerkleProof::new(
                    tx_proof
                        .proof
                        .indices
                        .into_iter()
                        .map(|index| index.value())
                        .collect(),
                    tx_proof
                        .proof
                        .lemmas
                        .into_iter()
                        .map(|lemma| lemma.into())
                        .collect(),
                );

                CBMT::retrieve_leaves(block.tx_hashes(), &merkle_proof)
                    .and_then(|tx_hashes| {
                        merkle_proof
                            .root(&tx_hashes)
                            .and_then(|raw_transactions_root| {
                                if block.transactions_root()
                                    == merkle_root(&[raw_transactions_root, witnesses_root])
                                {
                                    Some(tx_hashes.iter().map(|hash| hash.into()).collect())
                                } else {
                                    None
                                }
                            })
                    })
                    .ok_or_else(|| RPCError::invalid_params("Invalid transaction proof"))
            })
    }
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

**File:** rpc/src/module/chain.rs (L2241-2243)
```rust
        if !snapshot.is_main_chain(block_hash) {
            return Ok(None);
        }
```

**File:** util/jsonrpc-types/src/blockchain.rs (L1356-1364)
```rust
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct TransactionProof {
    /// Block hash
    pub block_hash: H256,
    /// Merkle root of all transactions' witness hash
    pub witnesses_root: H256,
    /// Merkle proof of all transactions' hash
    pub proof: MerkleProof,
}
```
