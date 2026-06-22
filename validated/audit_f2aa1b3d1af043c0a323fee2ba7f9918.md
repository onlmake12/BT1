Looking at the actual code to verify each claim in the question.

Based on my analysis of the actual production code, here is my determination:

---

### Title
Unbounded DB Read Amplification via `GetLastStateProof` Difficulty Binary Search — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
An unprivileged remote peer can send a `GetLastStateProof` message with up to 1000 difficulty entries (the actual limit, not ~500 as stated in the question) and `last_n_blocks=0`, causing the server to perform up to O(N × log H) DB reads with no per-peer rate limiting.

### Finding Description

**Entrypoint:** Any peer connected to the light client protocol can send a `GetLastStateProof` P2P message. It is dispatched in `LightClientProtocol::try_process` with no rate limiting. [1](#0-0) 

**Limit check:** The only guard is: [2](#0-1) 

With `last_n_blocks=0`, this allows up to `GET_LAST_STATE_PROOF_LIMIT = 1000` difficulties. [3](#0-2) 

**Binary search per difficulty:** For each difficulty entry, `get_block_numbers_via_difficulties` calls `get_first_block_total_difficulty_is_not_less_than`, which performs a binary search over the block range: [4](#0-3) 

Each binary search step calls `get_block_total_difficulty`, which issues **two** DB reads — `get_block_hash` then `get_block_ext`: [5](#0-4) 

**`start_block_number` advancement** (line 90–91) narrows the range for subsequent searches but does not eliminate the O(log H) cost per difficulty — in the worst case (difficulties spread across the full chain), total cost remains O(N × log H). [6](#0-5) 

**No rate limiting:** A grep across the entire `light-client-protocol-server` source confirms zero occurrences of `rate_limit`, `rate_limiter`, or `TooManyRequests`. Compare this to `HolePunching`, which has an explicit `RateLimiter` keyed by `(PeerIndex, message_type)`. The light client handler has no equivalent. [7](#0-6) 

### Impact Explanation
For a chain of height H = 10,000,000:
- Per request: 1000 × log₂(10M) × 2 ≈ **46,000 DB reads**
- With multiple concurrent peers sending such requests, this causes DB read amplification and I/O exhaustion on the serving node.

### Likelihood Explanation
The attack requires only a valid P2P connection to a node running the light client server. The difficulties must be monotonically increasing and within the chain's difficulty range (validated at lines 254–288), but these constraints are trivially satisfiable by querying the chain state first. No PoW, no privileged role, no key material is required. [8](#0-7) 

### Recommendation
1. Add a per-peer rate limiter to `LightClientProtocol::received()`, analogous to the `governor::RateLimiter` used in `HolePunching`.
2. Reduce `GET_LAST_STATE_PROOF_LIMIT` to account for the O(log H) multiplicative cost, or replace the flat count limit with a cost-weighted budget (e.g., `difficulties.len() × log2(chain_height) ≤ MAX_COST`).

### Proof of Concept
1. Connect to a CKB node with the light client server enabled.
2. Query the tip block hash and total difficulty.
3. Construct a `GetLastStateProof` with `last_n_blocks=0`, `difficulties` = 1000 evenly-spaced U256 values spanning the chain's total difficulty range, and a valid `last_hash`/`difficulty_boundary`.
4. Send the message and instrument `get_block_ext` calls on the server side.
5. Assert the call count is approximately `1000 × log₂(chain_height)`.

### Citations

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

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L49-70)
```rust
        loop {
            if block_greater_than_min == block_less_than_min + 1 {
                return Some((block_greater_than_min, end_total_difficulty));
            }
            let next_number = (block_less_than_min + block_greater_than_min) / 2;
            if let Some(total_difficulty) = self.get_block_total_difficulty(next_number) {
                match total_difficulty.cmp(min_total_difficulty) {
                    Ordering::Equal => {
                        return Some((next_number, total_difficulty));
                    }
                    Ordering::Less => {
                        block_less_than_min = next_number;
                    }
                    Ordering::Greater => {
                        block_greater_than_min = next_number;
                        end_total_difficulty = total_difficulty;
                    }
                }
            } else {
                return None;
            }
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L85-94)
```rust
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
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L111-116)
```rust
    fn get_block_total_difficulty(&self, number: BlockNumber) -> Option<U256> {
        self.snapshot
            .get_block_hash(number)
            .and_then(|block_hash| self.snapshot.get_block_ext(&block_hash))
            .map(|block_ext| block_ext.total_difficulty)
    }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L252-289)
```rust
        {
            // The difficulties should be sorted.
            if difficulties.windows(2).any(|d| d[0] >= d[1]) {
                let errmsg = "the difficulties should be monotonically increasing";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The maximum difficulty should be less than the difficulty boundary.
            if difficulties
                .last()
                .map(|d| *d >= difficulty_boundary)
                .unwrap_or(false)
            {
                let errmsg = "the difficulty boundary should be greater than all difficulties";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The first difficulty should be greater than the total difficulty before the start block.
            if let Some(start_difficulty) = difficulties.first()
                && start_block_number > 0
            {
                let previous_block_number = start_block_number - 1;
                if let Some(total_difficulty) =
                    sampler.get_block_total_difficulty(previous_block_number)
                {
                    if total_difficulty >= *start_difficulty {
                        let errmsg = format!(
                            "the start difficulty is {start_difficulty:#x} too less than \
                                the previous block #{previous_block_number} of the start block"
                        );
                        return StatusCode::InvalidRequest.with_context(errmsg);
                    }
                } else {
                    let errmsg = format!(
                        "the total difficulty for block#{previous_block_number} is not found"
                    );
                    return StatusCode::InternalError.with_context(errmsg);
                };
            }
        }
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
