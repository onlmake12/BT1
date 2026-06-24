Audit Report

## Title
Unbounded DB Read Amplification via `GetLastStateProof` Binary Search with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
Any unprivileged peer connected to the light client server can send a `GetLastStateProof` message with `last_n_blocks=0` and up to 1000 difficulty entries, triggering O(N × log H) DB reads per request with no per-peer rate limiting. The `received()` handler dispatches directly to `execute()` with no throttling, and the only guard is a flat count check that permits the full 1000-entry load.

## Finding Description

**Limit check (L201–204):** The sole guard is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

With `last_n_blocks=0`, this permits `difficulties.len()` up to `GET_LAST_STATE_PROOF_LIMIT = 1000`. [1](#0-0) [2](#0-1) 

**Binary search per difficulty (L85–94):** `get_block_numbers_via_difficulties` iterates over all difficulty entries and calls `get_first_block_total_difficulty_is_not_less_than` for each, which runs a binary search loop over the block range. [3](#0-2) 

**Two DB reads per binary search step (L111–116):** Each step calls `get_block_total_difficulty`, which issues `get_block_hash` then `get_block_ext` — two synchronous DB reads. [4](#0-3) 

**Range narrowing does not eliminate O(log H) cost (L90–91):** `start_block_number = num - 1` narrows the range for subsequent searches, but in the worst case (difficulties spread across the full chain), each search still costs O(log H). [5](#0-4) 

**Additional overhead in `complete_headers` (L132–179):** After the binary search phase, `complete_headers` is called with up to 1000 block numbers. Each entry triggers `get_ancestor` (chain traversal), `get_block`, and `chain_root_mmr(...).get_root()` — multiplying the I/O cost further. [6](#0-5) 

**No rate limiting in `received()` (L55–92):** The handler dispatches directly to `try_process` with no throttle, token bucket, or per-peer counter. A grep across the entire `light-client-protocol-server` source confirms zero occurrences of `rate_limit` or `RateLimiter`, while `HolePunching` uses an explicit `governor::RateLimiter` keyed by peer. [7](#0-6) 

## Impact Explanation

For a chain of height H = 10,000,000:
- Binary search phase: 1000 × log₂(10M) × 2 ≈ **46,000 DB reads**
- `complete_headers` phase: 1000 × (get_ancestor + get_block + MMR root) adds further I/O

A single peer can send this request in a tight loop with no ban consequence (valid requests are never banned). Multiple concurrent peers amplify the effect linearly. This maps to **High — "bad designs which could cause CKB network congestion with few costs"**, and at sufficient concurrency can exhaust RocksDB I/O capacity, causing the node to become unresponsive.

## Likelihood Explanation

The attack requires only a valid P2P connection to a node with the light client server enabled. The attacker must supply monotonically increasing difficulties within the chain's range (validated at L254–288), but these are trivially obtained by querying the tip state first. No PoW, no privileged role, no key material is required. The attack is repeatable indefinitely since valid (but expensive) requests do not trigger a ban. [8](#0-7) 

## Recommendation

1. Add a per-peer rate limiter in `LightClientProtocol::received()`, analogous to the `governor::RateLimiter` used in `network/src/protocols/hole_punching/mod.rs`, keyed by `(PeerIndex, message_type)`.
2. Replace the flat `GET_LAST_STATE_PROOF_LIMIT` count check with a cost-weighted budget that accounts for the O(log H) multiplier, e.g., `difficulties.len() × log2(chain_height) ≤ MAX_COST`.
3. Consider caching `get_block_total_difficulty` results within a single request to avoid redundant DB reads when binary search ranges overlap.

## Proof of Concept

1. Connect to a CKB node with the light client server enabled.
2. Send `GetLastState` to obtain the tip hash and total difficulty `D_tip`.
3. Construct a `GetLastStateProof` message with:
   - `last_n_blocks = 0`
   - `difficulties` = 1000 U256 values evenly spaced in `(0, D_tip)`
   - `last_hash` = tip hash
   - `difficulty_boundary` = `D_tip`
   - `start_number` = 0, `start_hash` = genesis hash
4. Send the message repeatedly in a loop from multiple connections.
5. Instrument `get_block_ext` calls on the server side and assert the call count per request is approximately `1000 × log₂(chain_height)`.
6. Observe node I/O saturation and increasing response latency on other protocol handlers.

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-179)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

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

                let header = packed::VerifiableHeader::new_builder()
                    .header(ancestor_header.data())
                    .uncles_hash(uncles_hash)
                    .extension(Pack::pack(&extension))
                    .parent_chain_root(parent_chain_root)
                    .build();

                headers.push(header);
            } else {
                let errmsg = format!("failed to find ancestor header ({number})");
                return Err(errmsg);
            }
        }

        Ok(headers)
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-204)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
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
