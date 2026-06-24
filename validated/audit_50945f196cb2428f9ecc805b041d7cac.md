All code references check out exactly. Let me confirm the key facts:

- `GET_BLOCKS_PROOF_LIMIT = 1000` and the guard is `> 1000` (strictly greater), so exactly 1000 entries pass. [1](#0-0) 
- The `execute()` loop performs 3 DB reads per found block with no number-range check against `last_block.number()`. [2](#0-1) 
- `reply_proof` builds `chain_root_mmr(last_block.number() - 1)`, which for block 1 is `chain_root_mmr(0)` — an MMR of size 1 covering only genesis. [3](#0-2) 
- `gen_proof` with out-of-range positions returns `Err` → `StatusCode::InternalError` (500). [4](#0-3) 
- `should_ban()` only triggers for `400..500`; code 500 is excluded → no ban. [5](#0-4) 
- No rate-limiting logic exists in `received`. [6](#0-5) 

---

Audit Report

## Title
Unbounded Unauthenticated DoS via `GetBlocksProof` with Mismatched `last_hash`/`block_hashes` Triggering Unpenalized `InternalError` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`, `util/light-client-protocol-server/src/lib.rs`)

## Summary
An unprivileged remote peer can send a `GetBlocksProof` message with `last_hash` pointing to a low-height main-chain block (e.g., block 1) and `block_hashes` containing up to 1000 hashes of higher-numbered main-chain blocks (e.g., blocks 2–1001). All validation passes, the server performs ~3000 synchronous database reads, then `reply_proof` fails with `InternalError` (500) because the MMR anchored at `last_block.number() - 1` is too small to cover the requested positions. The 500 status code is explicitly excluded from the ban predicate, so the peer is never penalized and can repeat the attack indefinitely with no rate limiting.

## Finding Description
In `GetBlocksProofProcess::execute`, the guard `block_hashes.len() > GET_BLOCKS_PROOF_LIMIT` uses strict inequality, so exactly 1000 entries pass (`GET_BLOCKS_PROOF_LIMIT = 1000`). The code validates that `last_hash` and each entry in `block_hashes` are individually on the main chain via `snapshot.is_main_chain`, but it never checks that each requested block's number is ≤ `last_block.number()`. All 1000 hashes for blocks 2–1001 pass `is_main_chain` and land in `found`. The loop at lines 81–95 then performs `get_block_header`, `get_block_uncles`, and `get_block_extension` for each — 3000 DB reads total.

In `reply_proof` (lib.rs line 199), the MMR is constructed as `snapshot.chain_root_mmr(last_block.number() - 1)`. With `last_block` = block 1, this is `chain_root_mmr(0)`, an MMR of size 1 containing only the genesis leaf. Calling `mmr.gen_proof` with positions for leaves 2–1001 returns `Err`, and the function returns `StatusCode::InternalError` (500).

Back in `received`, `status.should_ban()` checks `(400..500).contains(&code)`. Since 500 ∉ [400, 500), no ban is issued. `should_warn()` fires (500..600), so only a warning is logged. There is no per-peer rate limiter anywhere in the `received` handler.

## Impact Explanation
Each malicious request forces ~3000 synchronous database reads and an MMR proof attempt before failing with no cost to the attacker. With no ban and no rate limit, a single persistent connection can saturate the node's I/O and CPU, degrading or crashing a CKB node running the light-client protocol server. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a valid P2P connection to a node running the light-client protocol server. No keys, no PoW, no privileged access. The attacker needs only two publicly available main-chain block hashes at different heights. The attack is trivially scriptable and repeatable without any cooldown.

## Recommendation
1. **Add a number-range check in `execute`**: after resolving each found block's header, reject with `StatusCode::MalformedProtocolMessage` (4xx) any block whose `header.number() > last_block.number()`. This converts the attacker-controlled error path from 5xx to 4xx, triggering the existing ban logic.
2. **Add per-peer rate limiting** to `LightClientProtocol::received`, analogous to rate limiters present in other protocol handlers, to bound the request rate per peer regardless of response code.

## Proof of Concept
```
// Precondition: chain has ≥ 1001 blocks on main chain.
// block_1_hash  = hash of block at height 1
// block_N_hash  = hash of block at height N, for N in 2..=1001

let msg = GetBlocksProof {
    last_hash:    block_1_hash,
    block_hashes: [block_2_hash, block_3_hash, ..., block_1001_hash],  // exactly 1000 entries
};

// Server path:
// 1. block_hashes.len() == 1000 == GET_BLOCKS_PROOF_LIMIT  → passes (check is strictly >)
// 2. is_main_chain(block_1_hash)                           → true
// 3. all block_hashes pass is_main_chain                   → all 1000 in `found`
// 4. 3000 DB reads (get_block_header + get_block_uncles + get_block_extension × 1000)
// 5. reply_proof: mmr = chain_root_mmr(0), size=1
// 6. gen_proof([pos(2)..pos(1001)])                        → Err → InternalError(500)
// 7. should_ban(): 500 ∉ 400..500                         → None  ← no ban
// Repeat indefinitely with no penalty.
```

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
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

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
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

**File:** util/light-client-protocol-server/src/status.rs (L95-102)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
    }
```
