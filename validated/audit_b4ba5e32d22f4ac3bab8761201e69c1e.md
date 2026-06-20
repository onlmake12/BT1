### Title
Unbounded DB I/O via Crafted `GetLastStateProof` Difficulties Array — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
An unprivileged light-client peer can send a `GetLastStateProof` message with up to ~1000 difficulty entries, each triggering an O(log N) binary-search over the chain database. With no rate limiter on the `LightClientProtocol` handler, a single peer can continuously saturate the node's I/O with O(1000 × log(chain_length)) synchronous DB reads per message.

### Finding Description

**Entry point — no rate limiter on `LightClientProtocol`**

The `LightClientProtocol` struct has no `rate_limiter` field and `try_process` performs no per-peer, per-message-type throttle check before dispatching `GetLastStateProof`. [1](#0-0) [2](#0-1) 

Compare with the `Relayer`, which gates every non-PoW message through a 30-req/sec per-peer governor before processing — no equivalent guard exists here.

**Limit allows up to ~1000 difficulty entries**

```
GET_LAST_STATE_PROOF_LIMIT = 1000
```

The only size check is:
```
difficulties.len() + (last_n_blocks as usize) * 2 > GET_LAST_STATE_PROOF_LIMIT
``` [3](#0-2) [4](#0-3) 

With `last_n_blocks = 0`, an attacker can supply up to 1000 difficulty entries.

**Each entry triggers O(log N) DB reads via binary search**

`get_block_numbers_via_difficulties` iterates every difficulty entry and calls `get_first_block_total_difficulty_is_not_less_than`, which runs a binary search loop: [5](#0-4) [6](#0-5) 

Each iteration of the binary search calls `get_block_total_difficulty`, which issues **two** DB reads (`get_block_hash` + `get_block_ext`): [7](#0-6) 

**Total DB reads per message:** `1000 entries × log₂(chain_length) iterations × 2 reads`

On a 1M-block chain: `1000 × 20 × 2 = 40,000 DB reads per message`, with no rate cap.

**Validation does not prevent the worst case**

The pre-checks (monotonically increasing, boundary < last difficulty, first difficulty > start block's difficulty) are all satisfied by a valid but adversarially crafted array where each entry is just above the previous block's cumulative difficulty, maximizing binary search depth across the full `[start_block_number, difficulty_boundary_block_number)` range. [8](#0-7) 

### Impact Explanation
A single unprivileged peer can continuously send crafted `GetLastStateProof` messages, each causing ~40,000 synchronous RocksDB reads. This saturates the node's storage I/O, degrading block processing, peer sync, and RPC responsiveness — a practical availability DoS against any CKB full node running the light-client protocol server.

### Likelihood Explanation
The attack requires only a standard P2P connection to the light-client protocol endpoint (no PoW, no stake, no privileged role). The crafted message is trivially constructable. The absence of any rate limiter means the attack can be sustained indefinitely from a single peer.

### Recommendation
1. Add a per-peer, per-message-type rate limiter to `LightClientProtocol` analogous to the one in `Relayer` (governor crate, keyed by `(PeerIndex, message_item_id)`).
2. Impose a tighter cap on `difficulties.len()` independent of `last_n_blocks`, or bound the total binary-search work (e.g., cap the search range width).
3. Consider banning peers that repeatedly send requests that fail validation, rather than only logging.

### Proof of Concept
```
1. Connect to a CKB node's light-client protocol port.
2. Obtain a valid last_hash and start_hash for the current tip.
3. Construct a GetLastStateProof with:
   - last_n_blocks = 0
   - difficulties = [d₁, d₂, ..., d₁₀₀₀] where each dᵢ is just above
     the cumulative difficulty at block (start + i*(chain_len/1000))
   - difficulty_boundary = chain_tip_total_difficulty + 1
4. Send the message in a tight loop.
5. Observe: each message triggers ~40,000 RocksDB reads; node I/O
   saturates and block/sync latency increases measurably.
```

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-36)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}

impl LightClientProtocol {
    /// Create a new light client protocol handler.
    pub fn new(shared: Shared) -> Self {
        Self { shared }
    }
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
        }
    }
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
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
