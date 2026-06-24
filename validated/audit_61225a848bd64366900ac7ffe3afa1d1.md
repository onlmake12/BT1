Audit Report

## Title
Missing Main-Chain Guard in `verify_transaction_proof` and `verify_transaction_and_witness_proof` Allows Fork-Block Proofs to Pass — (File: `rpc/src/module/chain.rs`)

## Summary

`verify_transaction_proof` and `verify_transaction_and_witness_proof` accept a caller-supplied `block_hash` and perform Merkle proof verification against whatever block the store returns, without ever checking whether that block is on the canonical chain. An unprivileged caller can supply the hash of a fork/orphan block, construct a mathematically valid Merkle proof for a transaction inside it, and receive a successful verification response — causing the node to falsely attest that the transaction is confirmed on the main chain.

## Finding Description

`verify_transaction_proof` (L1938–1979) calls `snapshot.get_block()` directly with the caller-supplied `tx_proof.block_hash`:

```rust
snapshot
    .get_block(&(&tx_proof.block_hash).into())
    .ok_or_else(|| { ... })
    .and_then(|block| { ... })
```

`ChainStore::get_block` (store/src/store.rs L42–60) retrieves any block by hash from the database or freezer with no main-chain guard. Fork and orphan blocks are stored in the same database as canonical blocks during normal chain reorganizations.

The Merkle verification that follows is mathematically correct relative to the attacker-chosen fork block — `block.transactions_root()` is fixed by the fork block, and the proof is valid within that block's context. The function returns `Ok(vec![tx_hash])` — a successful verification — for any transaction that genuinely exists in any stored block, canonical or not.

`verify_transaction_and_witness_proof` (L2005–2070) has the identical missing guard.

By contrast, `get_block_by_hash` (L2241–2243) correctly gates on `is_main_chain` before returning data:

```rust
if !snapshot.is_main_chain(block_hash) {
    return Ok(None);
}
```

There are exactly 5 uses of `is_main_chain` in `chain.rs`; none appear inside `verify_transaction_proof` or `verify_transaction_and_witness_proof`.

Exploit path:
1. Attacker identifies a fork/orphan block `B` (hash `H`) stored in the node's database — produced by any recent reorg.
2. Block `B` contains transaction `T` (e.g., a deposit to an exchange that was later reorganized out).
3. Attacker calls `get_transaction_proof` with `tx_hashes=[T.hash]` and `block_hash=H`, or manually constructs the CBMT proof from `B`'s transaction list.
4. Attacker calls `verify_transaction_proof` with the resulting `TransactionProof` (block_hash=H, witnesses_root=B.calc_witnesses_root(), valid CBMT proof).
5. Node returns `[T.hash]` — success — even though `T` is not on the canonical chain.

## Impact Explanation

Any exchange, payment processor, or light-client integration that calls `verify_transaction_proof` to confirm settlement on the canonical CKB chain can be deceived into crediting a deposit that was reorganized out. This enables double-spend fraud against CKB ecosystem participants, constituting damage to the CKB economy. The RPC is the designated API for transaction inclusion verification; its false-positive response is the direct cause of the fraud, not an external misconfiguration.

**Severity: Critical — Vulnerabilities which could easily damage CKB economy (15001–25000 points)**

## Likelihood Explanation

- The RPC is publicly accessible with no authentication required by default.
- Fork/orphan blocks are routinely stored in the CKB database during every chain reorganization; no special setup is needed.
- Constructing a valid CBMT Merkle proof for a transaction in a known block requires only the block's transaction list, which is public data.
- No privileged access, key material, or majority hash power is required.
- The attack is repeatable and scriptable.

## Recommendation

Add an `is_main_chain` check immediately after obtaining the snapshot in both functions, before calling `get_block`:

```rust
fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
    let snapshot = self.shared.snapshot();
    let block_hash: packed::Byte32 = (&tx_proof.block_hash).into();

    if !snapshot.is_main_chain(&block_hash) {
        return Err(RPCError::invalid_params(
            format!("Block {:#x} is not on the main chain", tx_proof.block_hash)
        ));
    }

    snapshot.get_block(&block_hash)
        ...
}
```

Apply the identical fix to `verify_transaction_and_witness_proof`.

## Proof of Concept

1. Start a CKB devnet node with RPC enabled.
2. Mine a chain to height N. Block B at height K contains transaction T (e.g., a transfer).
3. Trigger a chain reorganization so that block B becomes a fork/orphan block (the canonical chain no longer includes B). Verify via `get_transaction` that T's status is no longer `committed`.
4. Call `get_transaction_proof` with `tx_hashes=[T.hash]` and `block_hash=B.hash`. The node returns a valid `TransactionProof` because `get_transaction_proof` also lacks a main-chain guard.
5. Call `verify_transaction_proof` with the returned proof.
6. Observe the response returns `[T.hash]` — success — despite T not being on the canonical chain.
7. Confirm the discrepancy: `get_transaction` returns a non-`committed` status for T, while `verify_transaction_proof` returns success. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** store/src/store.rs (L42-55)
```rust
    fn get_block(&self, h: &packed::Byte32) -> Option<BlockView> {
        let header = self.get_block_header(h)?;
        if let Some(freezer) = self.freezer()
            && header.number() > 0
            && header.number() < freezer.number()
        {
            let raw_block = freezer.retrieve(header.number()).expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == h.as_slice() {
                return Some(raw_block_reader.to_entity().into_view());
            }
        }
        let body = self.get_block_body(h);
```
