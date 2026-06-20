I have now read all the relevant code. Let me trace the full path carefully before rendering a verdict.

### Title
Unbounded per-request MMR work with no rate limiting in `GetLastStateProof` handler — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged peer connected to the light client protocol can craft a single `GetLastStateProof` message that forces the server to perform O(1000 × log N) RocksDB reads and an O(K log N) MMR proof generation, where N is the chain height and K ≤ 1000. Because the `LightClientProtocol` handler has **no rate limiter** (unlike every other CKB protocol handler), this can be repeated at wire speed to cause sustained CPU/IO exhaustion.

---

### Finding Description

**Step 1 — Limit check passes with 1000 block numbers.** [1](#0-0) 

The guard at line 201 is:

```
difficulties.len() + last_n_blocks * 2 <= GET_LAST_STATE_PROOF_LIMIT (1000)
``` [2](#0-1) 

Setting `difficulties = []` and `last_n_blocks = 500` satisfies `0 + 500×2 = 1000 ≤ 1000`.

**Step 2 — `block_numbers` reaches 1000 entries.**

`reorg_last_n_numbers` is populated (up to `last_n_blocks` = 500 entries) whenever `start_hash` is not the ancestor at `start_block_number` on the current main chain — the attacker supplies any hash not on the main chain (e.g., a stale fork hash or a hash that simply does not exist, causing `unwrap_or(false)` to return false): [3](#0-2) 

`last_n_numbers` independently contributes another 500 entries: [4](#0-3) 

Combined: [5](#0-4) 

**Step 3 — `complete_headers` calls `chain_root_mmr(*number - 1).get_root()` for every entry.** [6](#0-5) 

`chain_root_mmr(N)` constructs an MMR of size `leaf_index_to_mmr_size(N)` backed by RocksDB: [7](#0-6) 

`get_root()` reads all O(log N) MMR peaks from the store. Each peak is a separate `get_header_digest` call: [8](#0-7) 

On a 10 M-block chain (log₂(10 M) ≈ 23), this is **23 DB reads × 1000 entries = ~23 000 DB reads** just in `complete_headers`.

**Step 4 — `reply_proof` calls `mmr.gen_proof(1000 positions)`.** [9](#0-8) 

`gen_proof` over 1000 positions on an MMR of height 10 M is O(K log N) ≈ another **~23 000 DB reads**.

**Step 5 — No rate limiter exists on the light client protocol.**

A grep for `rate_limiter` / `RateLimiter` across the entire `util/light-client-protocol-server/` tree returns **zero matches**. The `received` handler dispatches directly to `try_process` with no throttle: [10](#0-9) 

By contrast, both `Relayer` and `HolePunching` install per-peer, per-message-type rate limiters before processing any message. The light client protocol has no equivalent protection.

---

### Impact Explanation

Each maximally-crafted `GetLastStateProof` request causes ~46 000 synchronous RocksDB reads and a full 1000-position MMR proof generation. With no rate limiting, a single attacker connection can saturate the server's I/O and CPU at the cost of sending a few-hundred-byte message per request. The work ratio (attacker bytes sent : server DB reads) scales as O(log N) with chain height, making the attack more effective as the chain grows.

---

### Likelihood Explanation

The light client protocol is a production feature reachable by any peer that connects on the light client port. No authentication, PoW, or stake is required. The crafted message is trivially constructable. The absence of a rate limiter is confirmed by code inspection.

---

### Recommendation

1. Add a per-peer rate limiter to `LightClientProtocol::received`, mirroring the pattern used in `Relayer` and `HolePunching`.
2. Cache the MMR root for a given block number within a single request so that `complete_headers` does not recompute it for every entry independently.
3. Consider reducing `GET_LAST_STATE_PROOF_LIMIT` or splitting the limit so that `reorg_last_n_numbers + last_n_numbers` is bounded separately from `difficulties`.

---

### Proof of Concept

```
last_hash        = <current tip hash>          # valid, on main chain
start_hash       = <any hash not on main chain> # triggers reorg_last_n_numbers
start_number     = 500
last_n_blocks    = 500
difficulties     = []                           # empty → sampled_numbers = 0
difficulty_boundary = <any value above tip difficulty>
```

Limit check: `0 + 500×2 = 1000 ≤ 1000` → passes.
`reorg_last_n_numbers` = blocks [0..500) = 500 entries.
`last_n_numbers` = blocks [tip-500..tip) = 500 entries.
`block_numbers.len()` = 1000.
`complete_headers` issues 1000 × O(log N) DB reads.
`reply_proof` issues O(1000 × log N) more DB reads.
Repeat at wire speed; no server-side throttle fires.

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L150-163)
```rust
                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-247)
```rust
        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-354)
```rust
        let block_numbers = reorg_last_n_numbers
            .into_iter()
            .chain(sampled_numbers)
            .chain(last_n_numbers)
            .collect::<Vec<_>>();
```

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** store/src/store.rs (L554-561)
```rust
    fn get_header_digest(&self, position_u64: u64) -> Option<packed::HeaderDigest> {
        let position: packed::Uint64 = position_u64.into();
        self.get(COLUMN_CHAIN_ROOT_MMR, position.as_slice())
            .map(|slice| {
                let reader = packed::HeaderDigestReader::from_slice_should_be_ok(slice.as_ref());
                reader.to_entity()
            })
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

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
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
            };
```
