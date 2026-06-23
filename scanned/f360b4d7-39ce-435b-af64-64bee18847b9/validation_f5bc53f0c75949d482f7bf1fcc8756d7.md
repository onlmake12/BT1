### Title
Unbounded DB Reads and MMR Computations per `GetLastStateProof` Request via Off-by-One in Limit Check — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can send a `GetLastStateProof` message with exactly 1000 distinct, monotonically increasing difficulties and `last_n_blocks=0`. The guard check uses strict `>` instead of `>=`, so 1000 passes. The server then performs 1000 binary-search DB lookups, 1000 MMR root computations, and a 1000-position MMR proof generation — all in a single request — with no rate limiting and no peer ban on success.

---

### Finding Description

**Off-by-one in the limit guard:**

```rust
// constant.rs
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;

// get_last_state_proof.rs line 201-205
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `difficulties.len() = 1000` and `last_n_blocks = 0`: `1000 + 0 = 1000`, which is **not** `> 1000`. The check passes. The intended bound is `>=`. [1](#0-0) [2](#0-1) 

**Work performed per request:**

1. `get_block_numbers_via_difficulties` iterates all 1000 difficulties, calling `get_first_block_total_difficulty_is_not_less_than` for each — a binary search doing O(log N) DB reads per call → **1000 × O(log N) DB reads**. [3](#0-2) 

2. `complete_headers` iterates up to 1000 block numbers. For each it calls `snapshot.get_ancestor(...)`, `snapshot.get_block(...)`, and `snapshot.chain_root_mmr(*number - 1).get_root()` — an MMR root computation → **1000 MMR root computations + 1000 block reads**. [4](#0-3) 

3. `reply_proof` calls `mmr.gen_proof(items_positions)` with 1000 positions → **O(1000 × log N) MMR proof generation**. [5](#0-4) 

**No banning on success:**

`should_ban()` only fires for status codes in `400..500`. A fully processed (expensive) request returns `StatusCode::OK` (200), so the peer is never banned. [6](#0-5) 

There is no per-peer request rate limiting anywhere in the handler path. [7](#0-6) 

---

### Impact Explanation

A single crafted request forces ~20,000+ DB reads (for a 1M-block chain) and 1000 MMR root computations. Ten concurrent connections each sending this request at full speed saturate the server's I/O and CPU, starving legitimate peers of responses. The server cannot distinguish this from a legitimate request and will never ban the attacker.

---

### Likelihood Explanation

The attack requires only a P2P connection to a node running the light-client protocol server. No PoW, no keys, no privileged role. The message is structurally valid and passes all semantic checks. Any node with the light-client protocol enabled is reachable.

---

### Recommendation

1. **Fix the off-by-one**: change `>` to `>=` in the limit guard so that exactly 1000 difficulties is rejected.
2. **Add per-peer rate limiting** on `GetLastStateProof` processing (e.g., one in-flight request per peer, or a token-bucket limiter).
3. **Cap `complete_headers` independently** from the difficulty count, since `last_n_numbers` can also contribute block numbers beyond the difficulty-sampled set.

---

### Proof of Concept

```
chain length N = 1,000,000 blocks
attacker sends GetLastStateProof {
    last_hash:           <valid tip hash>,
    start_hash:          <genesis hash>,
    start_number:        0,
    last_n_blocks:       0,
    difficulty_boundary: <total_difficulty[N-1]>,
    difficulties:        [D1, D2, ..., D1000]  // 1000 distinct values, monotonically
                                                // increasing, all < difficulty_boundary
}
```

Limit check: `1000 + 0*2 = 1000`, not `> 1000` → passes.
Server performs: 1000 binary searches × ~20 DB reads = ~20,000 DB reads, then 1000 `chain_root_mmr(n-1).get_root()` calls, then `gen_proof(1000 positions)`.
Repeat from 10 concurrent peers → sustained I/O and CPU exhaustion with no ban. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L73-103)
```rust
    fn get_block_numbers_via_difficulties(
        &self,
        mut start_block_number: BlockNumber,
        end_block_number: BlockNumber,
        difficulties: &[U256],
    ) -> Result<Vec<BlockNumber>, String> {
        let mut numbers = Vec::new();
        let mut current_difficulty = U256::zero();
        for difficulty in difficulties {
            if current_difficulty >= *difficulty {
                continue;
            }
            if let Some((num, diff)) = self.get_first_block_total_difficulty_is_not_less_than(
                start_block_number,
                end_block_number,
                difficulty,
            ) {
                if num > start_block_number {
                    start_block_number = num - 1;
                }
                numbers.push(num);
                current_difficulty = diff;
            } else {
                let errmsg = format!(
                    "the difficulty ({difficulty:#x}) is not in the block range [{start_block_number}, {end_block_number})"
                );
                return Err(errmsg);
            }
        }
        Ok(numbers)
    }
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L198-205)
```rust
    pub(crate) async fn execute(self) -> Status {
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L335-344)
```rust
                match sampler.get_block_numbers_via_difficulties(
                    start_block_number,
                    difficulty_boundary_block_number,
                    &difficulties,
                ) {
                    Ok(sampled_numbers) => (sampled_numbers, last_n_numbers),
                    Err(errmsg) => {
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L356-366)
```rust
        let (positions, headers) = {
            let mut positions: Vec<u64> = Vec::new();
            let headers =
                match sampler.complete_headers(&mut positions, &last_block_hash, &block_numbers) {
                    Ok(headers) => headers,
                    Err(errmsg) => {
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                };
            (positions, headers)
        };
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
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
