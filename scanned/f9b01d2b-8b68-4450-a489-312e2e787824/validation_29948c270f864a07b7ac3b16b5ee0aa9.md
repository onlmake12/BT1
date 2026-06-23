The attack path is concrete and fully traceable. Here is the complete analysis:

---

### Title
Missing Ancestor Validation in `GetBlocksProofProcess::execute` Enables Unbanned DoS via Out-of-Range MMR Proof Requests — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary

`GetBlocksProofProcess::execute` validates that requested `block_hashes` are on the main chain, but does **not** validate that their block numbers are ≤ `last_block.number()`. An attacker can supply `last_hash = block[N]` and `block_hashes = [block[N+1], ..., block[N+K]]` (all valid main-chain hashes). The server performs O(K) storage lookups, then calls `chain_root_mmr(N-1).gen_proof(positions_for_N+1..N+K)`, which fails because those positions are outside the MMR's range. The resulting `InternalError` (5xx) does **not** trigger a peer ban, enabling infinite repetition.

### Finding Description

**Step 1 — Entry point:** Any unprivileged P2P peer sends a `GetBlocksProof` message.

**Step 2 — Validation in `execute()`:** [1](#0-0) 

`last_block_hash` is checked with `is_main_chain` — block N passes. No check is made that `block_hashes` are ancestors of `last_hash`. [2](#0-1) 

`partition(is_main_chain)` places blocks N+1..N+K into `found` (they are on the main chain). `leaf_index_to_pos(header.number())` is called for each, producing positions for leaves N+1..N+K.

**Step 3 — MMR proof generation in `reply_proof()`:** [3](#0-2) 

The MMR is constructed as `chain_root_mmr(last_block.number() - 1)`, which covers only leaves 0..N-1: [4](#0-3) 

`gen_proof` is called with positions for blocks N+1..N+K, which are outside the MMR's size. This returns `Err`, and the function returns `StatusCode::InternalError` (500).

**Step 4 — No ban for 5xx:** [5](#0-4) 

`should_ban()` only bans for codes in `400..500`. `InternalError = 500` is excluded. The peer receives only a `warn!` log. [6](#0-5) 

### Impact Explanation

Each malicious request with K=1000 (the limit) causes:
- 1000 `get_block_header` RocksDB lookups
- 1000 `get_block_uncles` RocksDB lookups
- 1000 `get_block_extension` RocksDB lookups
- One MMR root computation + one failed `gen_proof` [7](#0-6) 

Since the peer is never banned, this loop can be repeated at network speed, causing sustained amplified I/O load on the light-client server with zero cost to the attacker beyond sending small fixed-size messages.

### Likelihood Explanation

Any peer that has synced the chain knows all block hashes. Constructing the malicious message requires no special privilege, no PoW, and no key material. The attack is locally testable and requires only a standard P2P connection to the light-client port.

### Recommendation

In `execute()`, after resolving `last_block`, add a check that each block hash in `found` has `header.number() <= last_block.number()`. Blocks with numbers exceeding `last_block.number()` cannot be ancestors of `last_hash` and should be moved to `missing` (or the request should be rejected as malformed with a 4xx code that triggers a ban).

```rust
// After resolving header in the for loop:
if header.number() > last_block.number() {
    // treat as missing or return MalformedProtocolMessage
}
```

Alternatively, classify this input as a 4xx `MalformedProtocolMessage` so `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` and the peer is banned after the first such request.

### Proof of Concept

```
1. Server has main chain of height M.
2. Attacker learns block hashes for heights N and N+1..N+1000 (via normal sync).
3. Attacker sends GetBlocksProof {
       last_hash: block[N].hash,
       block_hashes: [block[N+1].hash, ..., block[N+1000].hash]
   }
4. Server: all hashes pass is_main_chain → found = [N+1..N+1000]
5. Server: chain_root_mmr(N-1).gen_proof([pos(N+1)..pos(N+1000)]) → Err
6. Server returns InternalError(500), peer not banned.
7. Attacker repeats from step 3 indefinitely.
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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-85)
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L81-91)
```rust
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L199-216)
```rust
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
```

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```
