Audit Report

## Title
Unbounded MMR DB Read Amplification via `GetLastStateProof` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The `GetLastStateProof` handler accepts `last_n_blocks=500` with an empty `difficulties` list because the guard `0 + 500*2 = 1000` is not strictly greater than the limit of `1000`, passing the check. This causes up to 500 individual `chain_root_mmr(n).get_root()` calls in `complete_headers` plus a `gen_proof(500 positions)` call in `reply_proof`, each performing O(log N) RocksDB reads. Because `LightClientProtocol` has no rate limiter and successful requests return `Status::ok()` (never triggering a ban), an attacker can repeat this at wire speed to exhaust the node's RocksDB I/O and CPU.

## Finding Description

**Off-by-one in the limit guard:**
The check at `get_last_state_proof.rs` lines 201–204 is:
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // 1000
```
With `difficulties=[]` and `last_n_blocks=500`: `0 + 1000 = 1000`, which is **not** `> 1000`. The request passes. The maximum allowed `last_n_blocks` is therefore 500, not 499.

**500 `get_root()` calls in `complete_headers`:**
`complete_headers` (lines 132–163) iterates over every block number and calls `self.snapshot.chain_root_mmr(*number - 1).get_root()` for each non-genesis block. `chain_root_mmr` constructs a new `ChainRootMMR` backed by `MMRStore<HeaderDigest>` whose `get_elem` calls `self.store.get_header_digest(pos)` — a direct RocksDB read. `get_root()` traverses O(log N) nodes, so 500 calls = O(500 × log N) DB reads.

**Additional O(500 × log N) work in `reply_proof`:**
`reply_proof` (lib.rs lines 199–216) calls `mmr.get_root()` once more and then `mmr.gen_proof(items_positions)` over all 500 positions, doubling the DB read cost.

**No rate limiter on `LightClientProtocol`:**
The struct (lib.rs lines 26–29) contains only `pub shared: Shared`. A grep for `rate_limiter` across the entire repo returns zero matches in `util/light-client-protocol-server/`. By contrast, `sync/src/relayer/mod.rs` lines 63–67 define a `governor::RateLimiter` type used to cap per-peer message rates.

**Valid requests are never banned:**
`should_ban()` in `status.rs` lines 95–102 returns `Some(ban_time)` only for 4xx status codes. A well-formed request that succeeds returns `Status::ok()` (200). The attacker is never disconnected.

## Impact Explanation
On a mainnet chain of ~14 million blocks (~24 MMR levels), each `GetLastStateProof` with `last_n_blocks=500` causes approximately `500 × 24 × 2 ≈ 24,000` RocksDB point reads. With no rate limiting and no ban, a single persistent TCP connection can sustain thousands of such requests per second, saturating RocksDB I/O and CPU and degrading or crashing the full node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** and **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attack requires only a TCP connection to the light-client P2P port and any valid main-chain block hash (trivially obtained from a public explorer or via `GetLastState`). No key, stake, or hashpower is needed. The attacker is never banned. Multiple coordinated peers multiply the effect linearly. The preconditions are minimal and the exploit is fully repeatable.

## Recommendation
1. **Fix the off-by-one**: change `>` to `>=` in the limit guard so `last_n_blocks=500` with zero difficulties is rejected.
2. **Add a per-peer rate limiter** to `LightClientProtocol` mirroring the `governor`-based limiter already present in `Relayer`.
3. **Cache MMR roots within a single request**: use a local `HashMap<BlockNumber, HeaderDigest>` in `complete_headers` to avoid re-traversing the MMR for the same block number multiple times.

## Proof of Concept
```
1. Attacker connects to the full node's light-client P2P port.
2. Sends GetLastState → receives tip_hash (valid main-chain hash).
3. Constructs GetLastStateProof:
     last_hash           = tip_hash
     start_hash          = hash(tip - 500)
     start_number        = tip_number - 500
     last_n_blocks       = 500
     difficulty_boundary = U256::MAX   (forces boundary_block = start)
     difficulties        = []          (empty)
4. Limit check: 0 + 500*2 = 1000, NOT > 1000 → passes.
5. complete_headers iterates 500 block numbers:
     for each n in [tip-500 .. tip):
         snapshot.chain_root_mmr(n-1).get_root()  // O(log N) DB reads each
6. reply_proof:
     mmr.get_root()                               // O(log N) DB reads
     mmr.gen_proof(500 positions)                 // O(500 * log N) DB reads
7. Status::ok() returned → no ban.
8. Attacker immediately repeats from step 3.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** util/snapshot/src/lib.rs (L293-296)
```rust
impl MMRStore<HeaderDigest> for &Snapshot {
    fn get_elem(&self, pos: u64) -> MMRResult<Option<HeaderDigest>> {
        Ok(self.store.get_header_digest(pos))
    }
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

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```
