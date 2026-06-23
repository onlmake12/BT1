### Title
`verify_transaction_proof` and `verify_transaction_and_witness_proof` Do Not Check Canonical Chain Membership Before Returning Verified Transaction Hashes — (`File: rpc/src/module/chain.rs`)

### Summary

The `verify_transaction_proof` and `verify_transaction_and_witness_proof` RPC methods accept a caller-supplied block hash and verify a Merkle proof against whatever block is stored locally under that hash — including fork (non-canonical) blocks — without ever checking whether the block is part of the canonical chain. An unprivileged RPC caller can therefore supply a proof rooted in a fork block and receive back a list of "verified" transaction hashes that are not confirmed in the canonical chain, misleading any application that relies on this RPC to confirm finality.

---

### Finding Description

`verify_transaction_proof` (and its sibling `verify_transaction_and_witness_proof`) retrieve the block from the database using `snapshot.get_block()`, which returns any block stored in `COLUMN_BLOCK_HEADER` / `COLUMN_BLOCK_BODY` — including fork blocks that were never part of the canonical chain.

```rust
// rpc/src/module/chain.rs  (verify_transaction_proof)
fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
    let snapshot = self.shared.snapshot();

    snapshot
        .get_block(&(&tx_proof.block_hash).into())   // ← no is_main_chain() guard
        .ok_or_else(|| {
            RPCError::invalid_params(format!("Cannot find block {:#x}", tx_proof.block_hash))
        })
        .and_then(|block| {
            // … pure Merkle math, returns tx hashes on success
        })
}
``` [1](#0-0) 

The identical omission exists in `verify_transaction_and_witness_proof`:

```rust
fn verify_transaction_and_witness_proof(
    &self,
    tx_proof: TransactionAndWitnessProof,
) -> Result<Vec<H256>> {
    let snapshot = self.shared.snapshot();
    snapshot
        .get_block(&(&tx_proof.block_hash).into())   // ← no is_main_chain() guard
        .ok_or_else(|| { … })
        .and_then(|block| { … })
}
``` [2](#0-1) 

Every other query method that must restrict results to the canonical chain calls `snapshot.is_main_chain()` first:

```rust
// get_header — correct pattern
if !snapshot.is_main_chain(&block_hash) {
    return Ok(None);
}
``` [3](#0-2) 

```rust
// get_block_by_hash — correct pattern
if !snapshot.is_main_chain(block_hash) {
    return Ok(None);
}
``` [4](#0-3) 

`is_main_chain` queries `COLUMN_INDEX`, which only contains canonical-chain entries, while `get_block` queries `COLUMN_BLOCK_HEADER` / `COLUMN_BLOCK_BODY`, which stores all blocks including forks:

```rust
fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
    self.get(COLUMN_INDEX, hash.as_slice()).is_some()
}
``` [5](#0-4) 

The asymmetry is also visible in `get_transaction_proof`, which generates proofs only for canonical-chain transactions (via `get_transaction_info` → `COLUMN_TRANSACTION_INFO`), while `verify_transaction_proof` imposes no such restriction on the block it verifies against. [6](#0-5) 

---

### Impact Explanation

An application that calls `verify_transaction_proof` to confirm a payment or state transition will receive a successful response (a list of transaction hashes) even when the referenced block is a fork block that was never committed to the canonical chain. The response is cryptographically correct with respect to the fork block's Merkle root, but the transactions are not finalized. Any downstream logic that treats a successful `verify_transaction_proof` response as proof of canonical confirmation is vulnerable to being deceived.

---

### Likelihood Explanation

Fork blocks are a normal artifact of CKB's operation and are retained in the node's local database (they are explicitly queryable via `get_fork_block`). An attacker who can relay a crafted competing block to a target node — a standard P2P capability requiring no special privilege — can then construct a valid Merkle proof for any transaction they placed in that fork block and call `verify_transaction_proof` over the public RPC. No key material, operator access, or majority hash power is required.

---

### Recommendation

Add a canonical-chain membership check at the top of both methods, consistent with every other block-scoped query in the same file:

```rust
fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
    let snapshot = self.shared.snapshot();
    let block_hash: packed::Byte32 = (&tx_proof.block_hash).into();

    // Add this guard:
    if !snapshot.is_main_chain(&block_hash) {
        return Err(RPCError::invalid_params(format!(
            "Block {:#x} is not in the canonical chain",
            tx_proof.block_hash
        )));
    }

    snapshot
        .get_block(&block_hash)
        .ok_or_else(|| { … })
        .and_then(|block| { … })
}
```

Apply the same guard to `verify_transaction_and_witness_proof`.

---

### Proof of Concept

1. **Attacker relays a fork block** containing a transaction `T` (e.g., a payment to a victim address) to the target CKB node via the P2P relay protocol. The node stores the fork block in `COLUMN_BLOCK_HEADER` / `COLUMN_BLOCK_BODY` but does not add it to `COLUMN_INDEX` because it does not extend the canonical chain.

2. **Attacker constructs a valid Merkle proof** for `T` within the fork block. Because the fork block's `transactions_root` is a deterministic commitment over its transaction set, the proof is cryptographically valid.

3. **Attacker calls `verify_transaction_proof`** with the fork block hash and the proof:
   ```json
   {
     "method": "verify_transaction_proof",
     "params": [{
       "block_hash": "<fork_block_hash>",
       "witnesses_root": "<witnesses_root>",
       "proof": { "indices": [...], "lemmas": [...] }
     }]
   }
   ```

4. **Node responds with `[<tx_hash_of_T>]`** — a successful verification — because `snapshot.get_block()` finds the fork block and the Merkle math checks out. No canonical-chain check is performed.

5. **Victim's application** interprets the successful response as confirmation that `T` is finalized in the canonical chain and releases goods or services. The transaction `T` is never actually committed.

### Citations

**File:** rpc/src/module/chain.rs (L1677-1679)
```rust
        if !snapshot.is_main_chain(&block_hash) {
            return Ok(None);
        }
```

**File:** rpc/src/module/chain.rs (L1916-1936)
```rust
    fn get_transaction_proof(
        &self,
        tx_hashes: Vec<H256>,
        block_hash: Option<H256>,
    ) -> Result<TransactionProof> {
        let (block, leaf_indices) = self.get_tx_indices(tx_hashes, block_hash)?;
        Ok(TransactionProof {
            block_hash: block.hash().into(),
            witnesses_root: block.calc_witnesses_root().into(),
            proof: CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &leaf_indices,
            )
            .expect("build proof with verified inputs should be OK")
            .into(),
        })
    }
```

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

**File:** store/src/store.rs (L279-281)
```rust
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```
