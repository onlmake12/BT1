### Title
Missing Block-Number Bounds Check Before MMR Proof Generation Allows Unbanned Repeated InternalError — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

---

### Summary

`GetBlocksProofProcess::execute()` accepts block hashes that are on the main chain but have block numbers **greater than** `last_block.number()`. Their MMR leaf positions exceed the size of the MMR built for `last_block`, causing `mmr.gen_proof()` to return `Err`. This error is mapped to `StatusCode::InternalError` (500), which never triggers a peer ban. An attacker can repeat this at zero cost indefinitely.

---

### Finding Description

**Entry point:** A remote peer sends a `GetBlocksProof` P2P message with:
- `last_hash` = hash of a valid main-chain block at height N (e.g., block 10)
- `block_hashes` = hashes of valid main-chain blocks at heights > N (e.g., blocks 11–20)

**Trace through `GetBlocksProofProcess::execute()`:**

The only guards applied are:
- Non-empty check [1](#0-0) 
- Count limit (`GET_BLOCKS_PROOF_LIMIT = 1000`) [2](#0-1) 
- `last_hash` must be on main chain [3](#0-2) 
- No duplicate hashes [4](#0-3) 

The `block_hashes` are then partitioned into `found`/`missing` purely by `is_main_chain()` — **no check that `header.number() <= last_block.number()`**: [5](#0-4) 

For every hash in `found`, the position is computed directly from the block's number with no bounds validation: [6](#0-5) 

These positions (for blocks 11–20) are passed to `reply_proof` with `last_block` = block 10.

**Inside `reply_proof`:**

The MMR is constructed for `last_block.number() - 1` = block 9, covering only positions for blocks 0–9: [7](#0-6) 

`gen_proof` is then called with positions that exceed this MMR's size: [8](#0-7) 

The `Err` is mapped to `StatusCode::InternalError` (500) and returned.

**Ban logic:**

`should_ban()` only returns a ban duration for HTTP-style 4xx codes (400–499). `InternalError = 500` falls outside this range, so the peer is **never banned**: [9](#0-8) 

The `received` handler only logs a warning for 5xx and takes no punitive action: [10](#0-9) 

---

### Impact Explanation

Each crafted message causes the server to:
1. Look up up to 1000 block headers from the store
2. Compute MMR positions
3. Attempt (and fail) `gen_proof` against the MMR

All of this is server-side work with no cost to the attacker and no ban penalty. With `GET_BLOCKS_PROOF_LIMIT = 1000`, each message maximally exercises the code path. [11](#0-10) 

The attacker can sustain this indefinitely, degrading light-client serving capacity.

---

### Likelihood Explanation

The attack requires only a valid P2P connection and knowledge of any two main-chain block hashes where one has a higher number than the other — trivially obtainable from public chain data. No special privileges, keys, or hashpower are needed.

---

### Recommendation

In `GetBlocksProofProcess::execute()`, after resolving each found block header, validate that `header.number() <= last_block.number()`. Any block hash whose number exceeds `last_block.number()` should be treated as a protocol violation and return `StatusCode::MalformedProtocolMessage` (4xx), which triggers a peer ban.

```rust
// After: let header = snapshot.get_block_header(&block_hash)...
if header.number() > last_block.number() {
    return StatusCode::MalformedProtocolMessage
        .with_context("block hash exceeds last_block boundary");
}
```

---

### Proof of Concept

1. Mine a chain to height 20.
2. Connect as a light-client peer.
3. Send `GetBlocksProof` with `last_hash = block_10.hash()` and `block_hashes = [block_11.hash(), ..., block_20.hash()]`.
4. Observe: server returns no response (hits `InternalError` path), peer is **not** banned.
5. Repeat step 3 in a loop — peer remains connected and unbanned indefinitely.
6. Confirm via logs: repeated `"failed to generate a proof since ..."` warnings with no ban action.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L34-36)
```rust
        if self.message.block_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no block");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L45-50)
```rust
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendBlocksProof>(self.peer, self.nc)
                .await;
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L62-70)
```rust
        let mut uniq = HashSet::new();
        if !block_hashes
            .iter()
            .chain([last_block_hash].iter())
            .all(|hash| uniq.insert(hash))
        {
            return StatusCode::MalformedProtocolMessage
                .with_context("duplicate block hash exists");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-74)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L85-85)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```
