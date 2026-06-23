### Title
Unbounded `indices`/`lemmas` Vectors in `verify_transaction_proof` Enable Unauthenticated CPU/Memory Exhaustion — (`rpc/src/module/chain.rs`)

### Summary

`verify_transaction_proof` (and identically `verify_transaction_and_witness_proof`) accepts caller-controlled `proof.indices` and `proof.lemmas` vectors of arbitrary length with no size validation before passing them to `CBMT::retrieve_leaves` and `merkle_proof.root()`. An unauthenticated caller can submit a single JSON-RPC request with millions of entries, forcing O(N) heap allocation during deserialization and O(N) CPU work in the CBMT proof routines, before any error is returned.

---

### Finding Description

In `verify_transaction_proof`, after confirming the block exists, the function unconditionally constructs a `MerkleProof` from the raw caller-supplied vectors: [1](#0-0) 

Both `indices` (deserialized as `Vec<Uint32>`) and `lemmas` (deserialized as `Vec<H256>`) are collected in full before any validation. They are then passed directly to: [2](#0-1) 

`CBMT` and `MerkleProof` are thin type aliases over the external `merkle_cbt` crate: [3](#0-2) 

There is no length cap on `indices` or `lemmas` anywhere in the CKB RPC layer. No `max_payload_size`, `body_limit`, or equivalent guard was found in the codebase. The same pattern is repeated verbatim in `verify_transaction_and_witness_proof` with **two** unchecked proof vectors: [4](#0-3) 

---

### Impact Explanation

For N = 10,000,000 entries:
- **Memory**: `indices` allocates ~40 MB; `lemmas` allocates ~320 MB — ~360 MB per request, allocated during JSON deserialization before any logic runs.
- **CPU**: `retrieve_leaves` and `root()` iterate over all N entries performing index arithmetic and, in `root()`, repeated Blake2b hashes per lemma.
- A small number of concurrent requests can exhaust available heap and saturate CPU threads, causing the node to become unresponsive to legitimate peers and RPC callers.

The RPC endpoint requires no authentication by default, so any network-reachable caller can trigger this.

---

### Likelihood Explanation

The attack requires only a valid `block_hash` (trivially obtained from `get_tip_header` or any block explorer) and a single HTTP POST. No PoW, no keys, no privileged access. The cost to the attacker is negligible (one TCP connection, one large JSON body). The cost to the victim is proportional to N.

---

### Recommendation

Add an explicit length guard before constructing `MerkleProof`:

```rust
const MAX_PROOF_ENTRIES: usize = 1024; // bound to realistic block tx count
if tx_proof.proof.indices.len() > MAX_PROOF_ENTRIES
    || tx_proof.proof.lemmas.len() > MAX_PROOF_ENTRIES
{
    return Err(RPCError::invalid_params("proof vectors exceed maximum allowed length"));
}
```

Apply the same guard in `verify_transaction_and_witness_proof` for both `transactions_proof` and `witnesses_proof`. Additionally, configure an HTTP request body size limit at the RPC server layer to prevent oversized payloads from reaching deserialization at all.

---

### Proof of Concept

```python
import json, socket, time

# Obtain any valid block_hash from the node first
block_hash = "0xa5f5c85987a15de25661e5a214f2c1449cd803f071acc7999820f25246471f40"

N = 1_000_000
payload = json.dumps({
    "id": 1,
    "jsonrpc": "2.0",
    "method": "verify_transaction_proof",
    "params": [{
        "block_hash": block_hash,
        "witnesses_root": "0x" + "00" * 32,
        "proof": {
            "indices": ["0x0"] * N,
            "lemmas":  ["0x" + "00" * 32] * N
        }
    }]
})

start = time.time()
# send to node RPC port (default 8114)
# ... (standard HTTP POST)
elapsed = time.time() - start
print(f"N={N}: elapsed={elapsed:.2f}s")
# Assert elapsed grows linearly with N, not O(1)
```

Expected: processing time and peak RSS scale linearly with N, confirming unbounded resource consumption before the `RPCError` is returned.

### Citations

**File:** rpc/src/module/chain.rs (L1948-1961)
```rust
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
```

**File:** rpc/src/module/chain.rs (L1963-1966)
```rust
                CBMT::retrieve_leaves(block.tx_hashes(), &merkle_proof)
                    .and_then(|tx_hashes| {
                        merkle_proof
                            .root(&tx_hashes)
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

**File:** util/types/src/utilities/merkle_tree.rs (L23-25)
```rust
pub type CBMT = ExCBMT<Byte32, MergeByte32>;
/// Merkle proof for `Byte32` values.
pub type MerkleProof = ExMerkleProof<Byte32, MergeByte32>;
```
