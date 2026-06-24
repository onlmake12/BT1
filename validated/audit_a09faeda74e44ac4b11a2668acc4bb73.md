Audit Report

## Title
Missing Ancestor Validation in `GetBlocksProofProcess::execute` Enables Unbanned DoS via Out-of-Range MMR Proof Requests — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary

`GetBlocksProofProcess::execute` verifies that `last_hash` is on the main chain but never enforces that requested `block_hashes` have block numbers ≤ `last_block.number()`. An attacker supplies `last_hash = block[N]` and `block_hashes = [block[N+1]..block[N+1000]]` — all valid main-chain hashes — causing 3000 RocksDB lookups followed by a failed `gen_proof` call that returns `InternalError` (500). Because `should_ban()` only bans on codes 400–499, the peer is never disconnected, enabling infinite repetition at network speed.

## Finding Description

**Validation gap in `execute()`:** `last_block_hash` is checked with `is_main_chain` at L45, but no check is made that each hash in `block_hashes` has a block number ≤ `last_block.number()`. [1](#0-0) 

**Future blocks enter `found`:** `partition(is_main_chain)` at L72–74 places blocks N+1..N+1000 into `found` because they are legitimately on the main chain. `leaf_index_to_pos(header.number())` is then called for each, producing positions for leaves beyond the MMR's range. [2](#0-1) 

**MMR proof generation fails in `reply_proof()`:** The MMR is constructed as `chain_root_mmr(last_block.number() - 1)`, covering only leaves 0..N-1. [3](#0-2) 

`gen_proof` is then called with positions for blocks N+1..N+1000, which are outside the MMR's size. This returns `Err`, and the function returns `StatusCode::InternalError` (500). [4](#0-3) 

**No ban for 5xx:** `should_ban()` only bans for codes in `400..500` (exclusive). `InternalError = 500` falls outside this range, so the peer receives only a `warn!` log and is never disconnected or banned. [5](#0-4) 

**Per-request cost:** For each of the 1000 blocks in `found`, the server performs `get_block_header`, `get_block_uncles`, and `get_block_extension` — 3000 RocksDB lookups total — before the MMR call fails. [6](#0-5) 

**Enforced limit:** `GET_BLOCKS_PROOF_LIMIT = 1000` is the only bound on per-request cost, and it is fully exploitable. [7](#0-6) 

## Impact Explanation

Each malicious request with K=1000 causes 3000 RocksDB lookups plus one failed MMR computation, with zero cost to the attacker beyond a standard P2P connection. Since the peer is never banned, this loop can be repeated at network speed, causing sustained amplified I/O load on the light-client server. This maps to **High (10001–15000 points) — "Vulnerabilities or bad designs which could cause CKB network congestion with few costs"**: the attacker sends small fixed-size messages and receives amplified RocksDB I/O in return.

## Likelihood Explanation

Any peer that has synced the chain knows all block hashes. Constructing the malicious message requires no special privilege, no PoW, and no key material. The attack is locally testable and requires only a standard P2P connection to the light-client port. The `GET_BLOCKS_PROOF_LIMIT = 1000` cap is the only bound on per-request cost, and it is fully exploitable. [7](#0-6) 

## Recommendation

In `execute()`, after resolving `last_block`, add a check that each block hash in `found` has `header.number() <= last_block.number()`. Blocks with numbers exceeding `last_block.number()` cannot be ancestors of `last_hash` and should either be moved to `missing` or cause the request to be rejected with `StatusCode::MalformedProtocolMessage` (400), which triggers a ban via `should_ban()`.

```rust
// In the for loop over `found` (around L81-85 of get_blocks_proof.rs):
if header.number() > last_block.number() {
    return StatusCode::MalformedProtocolMessage
        .with_context("block hash is not an ancestor of last_hash");
}
```

Alternatively, filter such hashes into `missing` before computing positions, so the request is served gracefully without triggering the MMR error path. [8](#0-7) 

## Proof of Concept

```
1. Server has main chain of height M (M > 1000).
2. Attacker learns block hashes for heights N and N+1..N+1000 via normal sync (N < M-1000).
3. Attacker sends GetBlocksProof {
       last_hash: block[N].hash,
       block_hashes: [block[N+1].hash, ..., block[N+1000].hash]
   }
4. Server: all 1000 hashes pass is_main_chain → found = [N+1..N+1000]
5. Server: performs 3000 RocksDB lookups (header, uncles, extension per block)
6. Server: chain_root_mmr(N-1).gen_proof([pos(N+1)..pos(N+1000)]) → Err
7. Server returns InternalError(500); should_ban() returns None; peer not banned.
8. Attacker repeats from step 3 indefinitely at network speed.
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L44-50)
```rust
        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendBlocksProof>(self.peer, self.nc)
                .await;
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-95)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/lib.rs (L199-199)
```rust
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
```

**File:** util/light-client-protocol-server/src/lib.rs (L210-215)
```rust
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
```

**File:** util/light-client-protocol-server/src/status.rs (L95-101)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```
