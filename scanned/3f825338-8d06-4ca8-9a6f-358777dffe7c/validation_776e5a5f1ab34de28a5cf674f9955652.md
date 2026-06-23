Let me look at the `retrieve_leaves` implementation in the external `merkle_cbt` crate to understand exactly how it selects leaves.

I now have all the information needed. Let me trace the full execution path precisely.

### Title
Missing Cross-Index Binding Check in `verify_transaction_and_witness_proof` Allows Decoupled Tx/Witness Proof Acceptance — (`rpc/src/module/chain.rs`)

### Summary

`verify_transaction_and_witness_proof` verifies the `transactions_proof` and `witnesses_proof` sub-proofs **completely independently** against the block's actual leaf data, then checks only that their two computed roots combine to `block.transactions_root()`. Because each valid individual proof always reconstructs the full tree root regardless of which leaf index it covers, an attacker can supply `transactions_proof.indices = [i]` and `witnesses_proof.indices = [j]` (i ≠ j) with valid lemmas for each, and the final equality check will always pass. The function then returns `tx_hash[i]` as "verified" while the witness proof actually covers `tx_hash[j]`'s witness — violating the invariant that a successful proof binds the same position in both trees.

---

### Finding Description

**How `transactions_root` is constructed in a block:** [1](#0-0) 

```
transactions_root = merkle_root([merkle_root(all_tx_hashes), merkle_root(all_witness_hashes)])
```

**What the verifier actually does** (`rpc/src/module/chain.rs` lines 2045–2056): [2](#0-1) 

Step-by-step with attacker-supplied indices `i` (tx) and `j` (witness), `i ≠ j`:

1. `CBMT::retrieve_leaves(block.tx_witness_hashes(), &witnesses_merkle_proof)` — uses index `j` to select `witness_hash[j]` from the block's real witness list.
2. `witnesses_merkle_proof.root(&[witness_hash[j]])` — with valid lemmas for position `j`, this reconstructs the **full** witness tree root = `merkle_root(all_witness_hashes)`.
3. `CBMT::retrieve_leaves(block.tx_hashes(), &transactions_merkle_proof)` — uses index `i` to select `tx_hash[i]` from the block's real tx list.
4. `transactions_merkle_proof.root(&[tx_hash[i]])` — with valid lemmas for position `i`, this reconstructs the **full** tx tree root = `merkle_root(all_tx_hashes)`.
5. Final check: `block.transactions_root() == merkle_root([full_tx_root, full_witness_root])` — **this is always true** because both roots are the actual full-tree roots, regardless of whether `i == j`.

There is **no check** that `transactions_proof.indices == witnesses_proof.indices`. The two proofs are verified in isolation; their roots are always the full tree roots when the lemmas are valid, so the combined check is a tautology for any pair of valid individual proofs. [3](#0-2) 

The function then returns `tx_hashes` (the hashes at position `i`), certifying that `tx_hash[i]` has a valid witness proof — when the witness proof actually covers `tx_hash[j]`. [4](#0-3) 

---

### Impact Explanation

Any unprivileged caller with RPC access can craft a `TransactionAndWitnessProof` where `transactions_proof` is a valid proof for tx at position `i` and `witnesses_proof` is a valid proof for the witness at position `j ≠ i`. The RPC returns success and emits `tx_hash[i]` as "proven." Downstream consumers that rely on `verify_transaction_and_witness_proof` to confirm that a specific transaction is bound to a specific witness (e.g., light clients, bridges, or off-chain verifiers) will accept a proof that actually decouples the two, enabling witness substitution: an attacker can present `tx_hash[i]` as having `witness_hash[j]`'s witness without detection by this verifier.

---

### Likelihood Explanation

The exploit requires no special privilege, no hash collision, no PoW, and no key material. Any caller who can read two valid proofs from the same block (e.g., by calling `get_transaction_and_witness_proof` for two different transactions) can immediately construct the mismatched proof. The precondition — a block with N ≥ 2 transactions — is satisfied by virtually every non-genesis block on mainnet.

---

### Recommendation

Add an explicit check that the indices sets are identical before proceeding:

```rust
if tx_proof.transactions_proof.indices != tx_proof.witnesses_proof.indices {
    return Err(RPCError::invalid_params(
        "transactions_proof and witnesses_proof must cover the same indices",
    ));
}
```

This should be inserted at the top of `verify_transaction_and_witness_proof`, before constructing either `MerkleProof`. [5](#0-4) 

---

### Proof of Concept

Given a block `B` with transactions `[tx0, tx1, ...]` on a live node:

```bash
# 1. Obtain a valid proof for tx0 (index 0)
proof0=$(curl -s -X POST ... -d '{"method":"get_transaction_and_witness_proof","params":[[TX0_HASH],null]}')

# 2. Obtain a valid proof for tx1 (index 1)
proof1=$(curl -s -X POST ... -d '{"method":"get_transaction_and_witness_proof","params":[[TX1_HASH],null]}')

# 3. Craft a mismatched proof: tx from proof0 (index 0), witness from proof1 (index 1)
crafted={
  "block_hash": <same block>,
  "transactions_proof": <proof0.transactions_proof>,   # indices=[0]
  "witnesses_proof":    <proof1.witnesses_proof>        # indices=[1]
}

# 4. Submit
curl -X POST ... -d '{"method":"verify_transaction_and_witness_proof","params":[crafted]}'
# Returns: [TX0_HASH]  ← success, despite witness proof being for TX1
```

The call succeeds and returns `TX0_HASH` as verified, even though the witness proof covers `TX1`'s witness. No hash collision is required; the structural property of independent root reconstruction makes this unconditionally exploitable. [6](#0-5)

### Citations

**File:** util/types/src/extension.rs (L38-40)
```rust
        let raw_transactions_root = merkle_root(tx_hashes);
        let witnesses_root = merkle_root(tx_witness_hashes);
        let transactions_root = merkle_root(&[raw_transactions_root, witnesses_root]);
```

**File:** rpc/src/module/chain.rs (L2005-2015)
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

**File:** rpc/src/module/chain.rs (L2044-2070)
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
                    .ok_or_else(|| {
                        RPCError::invalid_params("Invalid transaction_and_witness proof")
                    })
            })
    }
```
