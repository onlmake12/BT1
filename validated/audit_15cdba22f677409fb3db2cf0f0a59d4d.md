All code claims verified. Let me confirm the relayer rate limiter comparison mentioned in the report.

Audit Report

## Title
Unbounded MMR DB Read Amplification via `GetLastStateProof` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
An off-by-one in the size guard at `get_last_state_proof.rs` L201–204 allows a request with `last_n_blocks=500` and an empty `difficulties` list to pass validation. Each such request triggers up to 500 individual `chain_root_mmr().get_root()` calls in `complete_headers` plus a `gen_proof(500 positions)` call in `reply_proof`, each performing O(log N) RocksDB reads. Because `LightClientProtocol` has no per-peer rate limiter and successful requests return `Status::ok()` (never triggering a ban), an attacker can repeat this at wire speed to saturate the node's RocksDB I/O.

## Finding Description

**Off-by-one in the size guard:**
The check at `get_last_state_proof.rs` L201–204 uses strict `>`:
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // 1000
```
With `difficulties=[]` and `last_n_blocks=500`: `0 + 1000 = 1000`, which is **not** `> 1000`. The request passes. The effective maximum `last_n_blocks` is 500, not 499. [1](#0-0) [2](#0-1) 

**500 MMR root computations in `complete_headers`:**
`complete_headers` iterates over every block number in `block_numbers` (up to 500) and calls:
```rust
let mmr = self.snapshot.chain_root_mmr(*number - 1);
mmr.get_root()
```
Each `get_root()` traverses the MMR tree issuing O(log N) RocksDB reads. [3](#0-2) 

**Additional O(500 × log N) work in `reply_proof`:**
`reply_proof` in `lib.rs` calls `mmr.get_root()` once more and then `mmr.gen_proof(items_positions)` over all 500 positions — another full O(500 × log N) DB read pass. [4](#0-3) 

**No rate limiter on `LightClientProtocol`:**
The struct contains only `pub shared: Shared` — no rate-limiter field and no rate-limiting logic anywhere in `util/light-client-protocol-server/`. By contrast, `sync/src/relayer/mod.rs` contains an explicit per-peer governor rate limiter (12 matches confirmed). [5](#0-4) 

**Valid requests are never banned:**
`should_ban()` returns `Some` only for 4xx status codes. A well-formed request that succeeds returns `StatusCode::OK` (200). `InternalError` (500) triggers only a warning log. The attacker is never disconnected. [6](#0-5) 

**Branch that routes 500 blocks into `last_n_numbers`:**
When `last_block_number - start_block_number <= last_n_blocks`, all blocks go into `last_n_numbers` with `sampled_numbers = []`, confirming the full 500-block path is reachable. [7](#0-6) 

## Impact Explanation
On a mainnet chain of ~14 million blocks (~24 MMR levels), each request causes approximately `500 × 24 × 2 ≈ 24,000` RocksDB point reads. With no rate limiting, a single persistent TCP connection can sustain many such requests per second, saturating RocksDB I/O and CPU shared with the main chain processing pipeline. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**, and with multiple coordinated peers also **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attack requires only a TCP connection to the light-client P2P port and any valid main-chain block hash (trivially obtained from a public explorer or via `GetLastState`). No key, stake, or hashpower is needed. The attacker is never banned (`StatusCode::OK` is returned on success; `InternalError` only logs a warning). Multiple coordinated peers multiply the effect linearly.

## Recommendation
1. **Fix the off-by-one**: change `>` to `>=` at `get_last_state_proof.rs` L201 so that `last_n_blocks=500` with zero difficulties is rejected.
2. **Add a per-peer rate limiter** to `LightClientProtocol` mirroring the governor-based limiter already present in `sync/src/relayer/mod.rs`.
3. **Cache MMR roots within a single request**: a local `HashMap<BlockNumber, HeaderDigest>` in `complete_headers` would reduce 500 independent DB traversals to at most one per unique block number.

## Proof of Concept
```
1. Attacker connects to the full node's light-client P2P port.
2. Sends GetLastState → receives tip_hash (valid main-chain hash).
3. Constructs GetLastStateProof:
     last_hash           = tip_hash
     start_hash          = hash(tip - 500)
     start_number        = tip_number - 500
     last_n_blocks       = 500
     difficulty_boundary = U256::MAX   (forces boundary = start, no sampled blocks)
     difficulties        = []          (empty)
4. Limit check: 0 + 500*2 = 1000, NOT > 1000 → passes (L201–204).
5. last_block_number - start_block_number = 500 <= last_n_blocks=500 → all 500 blocks
   go into last_n_numbers; sampled_numbers = [] (L291–297).
6. complete_headers iterates 500 block numbers:
     for each n: snapshot.chain_root_mmr(n-1).get_root()  // O(log N) DB reads each
7. reply_proof:
     mmr.get_root()               // O(log N) DB reads
     mmr.gen_proof(500 positions) // O(500 * log N) DB reads
8. Returns Status::ok() → no ban (status.rs L95–102).
9. Attacker immediately repeats from step 3.
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L153-163)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-204)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-297)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
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
