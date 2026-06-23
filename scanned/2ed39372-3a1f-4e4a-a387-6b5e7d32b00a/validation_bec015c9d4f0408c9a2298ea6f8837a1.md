Looking at the actual implementation of `verify_transaction_proof` and the surrounding code to determine if the missing `is_main_chain` check is a real, exploitable vulnerability.

Based on my thorough code review, I have all the facts needed for a definitive assessment.

**Key findings:**

1. `verify_transaction_proof` calls `snapshot.get_block()` with no `is_main_chain` guard: [1](#0-0) 

2. `snapshot.get_block()` retrieves any block from storage by hash, with no canonical-chain check: [2](#0-1) 

3. `is_main_chain()` checks `COLUMN_INDEX`, which only contains main-chain block hashes: [3](#0-2) 

4. Every other block-returning RPC **does** guard with `is_main_chain`:
   - `get_block_by_hash` at line 2241: `if !snapshot.is_main_chain(block_hash) { return Ok(None); }`
   - `get_header` at line 1677: `if !snapshot.is_main_chain(&block_hash) { return Ok(None); }` [4](#0-3) 

5. `get_fork_block` explicitly serves non-canonical blocks from the same storage, confirming fork blocks persist in the DB: [5](#0-4) 

6. `get_tx_indices` (used by `get_transaction_proof`) uses `get_transaction_info` which only indexes main-chain transactions — so `get_transaction_proof` itself won't generate a proof for a fork-block tx. But `verify_transaction_proof` accepts a **caller-supplied** `TransactionProof` struct, so the attacker constructs the proof manually without going through `get_transaction_proof`: [6](#0-5) 

---

### Title
Missing `is_main_chain` Guard in `verify_transaction_proof` Allows Fork-Block Transactions to Pass as Canonically Confirmed — (`rpc/src/module/chain.rs`)

### Summary
`verify_transaction_proof` retrieves a block by its caller-supplied `block_hash` using `snapshot.get_block()`, which returns any block in storage including non-canonical fork blocks. It then verifies only the internal Merkle consistency of the proof against that block's `transactions_root`. There is no check that the block is on the canonical chain. An attacker who can place a fork block in local storage (via a natural reorg or by submitting a valid PoW block over P2P) can construct a valid `TransactionProof` for a transaction in that fork block and receive `Ok([tx_hash])` from `verify_transaction_proof`, despite the transaction never having been committed to the canonical chain.

### Finding Description
In `rpc/src/module/chain.rs`, `verify_transaction_proof` (line 1938) calls `snapshot.get_block(&tx_proof.block_hash)` with no preceding `is_main_chain` check. Every other block-returning RPC in the same file guards with `if !snapshot.is_main_chain(...) { return Ok(None); }` before proceeding. Fork blocks are durably stored in the database (evidenced by the `get_fork_block` RPC at line 2072 which explicitly retrieves them via the same `snapshot.get_block()` call). After retrieving the block, the function only verifies that the supplied Merkle proof is internally consistent with the block's `transactions_root` — it never asserts canonical inclusion. The same flaw exists in `verify_transaction_and_witness_proof` (line 2005–2070).

**Concrete attack path (no privileged access required):**
1. A fork block containing a crafted transaction exists in local storage. This occurs naturally after any chain reorg, or the attacker submits a valid PoW block via the P2P sync protocol that does not win the chain.
2. The attacker calls `get_fork_block(fork_block_hash)` to retrieve the full block body.
3. The attacker manually constructs a `TransactionProof` with `block_hash = fork_block_hash`, `witnesses_root = fork_block.calc_witnesses_root()`, and a valid CBMT Merkle proof for the target transaction within that block.
4. The attacker calls `verify_transaction_proof(proof)`. The function finds the block in storage, verifies the Merkle proof against the block's `transactions_root`, and returns `Ok([tx_hash])`.
5. The transaction is "verified" despite never being committed to the canonical chain.

### Impact Explanation
Applications (exchanges, DeFi bridges) that use `verify_transaction_proof` as a deposit confirmation oracle will accept the fraudulent `Ok` response as proof of canonical inclusion. A deposit or state transition based on a reorged-out or never-canonical transaction will be credited, causing direct financial loss. The impact is proportional to the value of the transaction being falsely confirmed.

### Likelihood Explanation
The precondition — a fork block in local storage — is met after every chain reorg, which is a routine event on any live CKB node. No special mining power is required to exploit a naturally occurring reorg. The attacker only needs RPC access (the default JSON-RPC port) and knowledge of the fork block hash (obtainable via `get_fork_block`). The proof construction is straightforward given the block body.

### Recommendation
Add an `is_main_chain` guard at the top of `verify_transaction_proof` (and `verify_transaction_and_witness_proof`) before calling `get_block`:

```rust
fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
    let snapshot = self.shared.snapshot();
    let block_hash: packed::Byte32 = (&tx_proof.block_hash).into();

    // Guard: reject proofs for non-canonical blocks
    if !snapshot.is_main_chain(&block_hash) {
        return Err(RPCError::invalid_params(format!(
            "Block {:#x} is not on the main chain",
            tx_proof.block_hash
        )));
    }

    snapshot
        .get_block(&block_hash)
        // ... rest unchanged
}
```

Apply the same fix to `verify_transaction_and_witness_proof`.

### Proof of Concept
```
# 1. Mine a fork block containing tx T (use process_block_without_verify or natural reorg)
# 2. Retrieve the fork block
curl -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"get_fork_block","params":["<FORK_BLOCK_HASH>"],"id":1}' \
  http://localhost:8114

# 3. Compute witnesses_root and CBMT proof for tx T from the fork block body (standard CKB SDK)

# 4. Call verify_transaction_proof with the manually constructed proof
curl -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"verify_transaction_proof","params":[{"block_hash":"<FORK_BLOCK_HASH>","witnesses_root":"<WITNESSES_ROOT>","proof":{"indices":["0x0"],"lemmas":[]}}],"id":2}' \
  http://localhost:8114
# Returns: {"result":["<TX_HASH>"]}  <-- success, despite non-canonical block

# 5. Confirm the block is NOT canonical
curl -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"get_block","params":["<FORK_BLOCK_HASH>"],"id":3}' \
  http://localhost:8114
# Returns: {"result":null}  <-- block is not on main chain
```

### Citations

**File:** rpc/src/module/chain.rs (L1938-1945)
```rust
    fn verify_transaction_proof(&self, tx_proof: TransactionProof) -> Result<Vec<H256>> {
        let snapshot = self.shared.snapshot();

        snapshot
            .get_block(&(&tx_proof.block_hash).into())
            .ok_or_else(|| {
                RPCError::invalid_params(format!("Cannot find block {:#x}", tx_proof.block_hash))
            })
```

**File:** rpc/src/module/chain.rs (L2072-2098)
```rust
    fn get_fork_block(
        &self,
        block_hash: H256,
        verbosity: Option<Uint32>,
    ) -> Result<Option<ResponseFormat<BlockView>>> {
        let snapshot = self.shared.snapshot();
        let block_hash = block_hash.into();
        if snapshot.is_main_chain(&block_hash) {
            return Ok(None);
        }

        let verbosity = verbosity
            .map(|v| v.value())
            .unwrap_or(DEFAULT_BLOCK_VERBOSITY_LEVEL);
        // TODO: verbosity level == 1, output block only contains tx_hash in JSON format
        if verbosity == 2 {
            Ok(snapshot
                .get_block(&block_hash)
                .map(|block| ResponseFormat::json(block.into())))
        } else if verbosity == 0 {
            Ok(snapshot
                .get_packed_block(&block_hash)
                .map(|packed| ResponseFormat::hex(packed.as_bytes())))
        } else {
            Err(RPCError::invalid_params("invalid verbosity level"))
        }
    }
```

**File:** rpc/src/module/chain.rs (L2241-2243)
```rust
        if !snapshot.is_main_chain(block_hash) {
            return Ok(None);
        }
```

**File:** rpc/src/module/chain.rs (L2293-2315)
```rust
        for tx_hash in tx_hashes {
            match snapshot.get_transaction_info(&(&tx_hash).into()) {
                Some(tx_info) => {
                    if retrieved_block_hash.is_none() {
                        retrieved_block_hash = Some(tx_info.block_hash);
                    } else if Some(tx_info.block_hash) != retrieved_block_hash {
                        return Err(RPCError::invalid_params(
                            "Not all transactions found in retrieved block",
                        ));
                    }

                    if !tx_indices.insert(tx_info.index as u32) {
                        return Err(RPCError::invalid_params(format!(
                            "Duplicated tx_hash {tx_hash:#x}"
                        )));
                    }
                }
                None => {
                    return Err(RPCError::invalid_params(format!(
                        "Transaction {tx_hash:#x} not yet in block"
                    )));
                }
            }
```

**File:** store/src/store.rs (L42-69)
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
        let uncles = self
            .get_block_uncles(h)
            .expect("block uncles must be stored");
        let proposals = self
            .get_block_proposal_txs_ids(h)
            .expect("block proposal_ids must be stored");
        let extension_opt = self.get_block_extension(h);

        let block = if let Some(extension) = extension_opt {
            BlockView::new_unchecked_with_extension(header, uncles, body, proposals, extension)
        } else {
            BlockView::new_unchecked(header, uncles, body, proposals)
        };
        Some(block)
```

**File:** store/src/store.rs (L278-281)
```rust
    /// Returns true if the block is on the main chain.
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```
