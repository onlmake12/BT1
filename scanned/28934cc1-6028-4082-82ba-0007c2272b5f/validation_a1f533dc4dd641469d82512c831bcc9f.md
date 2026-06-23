### Title
Unbounded RecentReject RocksDB Growth Due to Missing `total_keys_num` Increment in `RecentReject::put` — (`tx-pool/src/component/recent_reject.rs`)

### Summary

`RecentReject::put` writes a rejected transaction to RocksDB and then checks whether `self.total_keys_num + 1 > count_limit` to decide whether to call `shrink()`. However, the result of `checked_add(1)` is stored only in a **local variable** and is never written back to `self.total_keys_num`. Because `self.total_keys_num` is never incremented in the non-shrink path, the threshold check always evaluates against the initial (startup) estimate — effectively 0 for a fresh DB — so `shrink()` is never triggered and the DB grows without bound.

### Finding Description

In `RecentReject::put`:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ...
    self.db.put(&shard, hash_slice, json_string)?;   // writes to RocksDB first

    if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
        if total_keys_num > self.count_limit {
            self.shrink()?;
        }
        // ← self.total_keys_num is NEVER updated here
    } else {
        self.shrink()?;
    }
    Ok(())
}
``` [1](#0-0) 

`total_keys_num` is a local binding. `self.total_keys_num` is only mutated inside `shrink()`, which re-reads the estimate from RocksDB: [2](#0-1) 

Because `self.total_keys_num` is initialized once at startup from `estimate_num_keys_cf` (0 for a fresh DB) and never incremented in the normal path, every subsequent call to `put` evaluates `0 + 1 > count_limit`, which is false for any reasonable `count_limit`. `shrink()` is therefore never called, and the DB grows without bound. [3](#0-2) 

The question frames "total_keys_num is frozen" as a precondition, but it is actually the **unconditional default behavior** of the code — no special setup is required.

### Impact Explanation

Every rejected transaction (with any reason except `Duplicated`) is recorded: [4](#0-3) 

The `put_recent_reject` call path is reached from `after_process` for remote transactions: [5](#0-4) 

And from the callback registered in `register_tx_pool_callback`: [6](#0-5) 

Each write goes directly to RocksDB with no effective upper bound. Over time this causes unbounded disk consumption by the `recent_reject` RocksDB instance, leading to disk exhaustion and node crash or severe I/O degradation affecting block/tx processing throughput.

### Likelihood Explanation

An unprivileged remote peer can reach this path via the standard P2P relay protocol. The relayer accepts `RelayTransactions` messages (up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` txs per message) at up to 30 messages/second per peer: [7](#0-6) 

Transactions are submitted via `submit_remote_tx`: [8](#0-7) 

For `LowFeeRate` rejects, the peer is **not banned** (`is_malformed_tx()` returns false for `LowFeeRate`, so `ban_malformed` is not called): [9](#0-8) [10](#0-9) 

An attacker can continuously announce new transaction hashes, receive `GetRelayTransactions` requests, and respond with transactions that fail with `LowFeeRate` (or `Resolve`, `ExceededMaximumAncestorsCount`, etc.) — all without ever being banned. Each rejection writes one entry to RocksDB permanently.

### Recommendation

In `RecentReject::put`, update `self.total_keys_num` in the non-shrink branch:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num;  // ← add this line
    }
} else {
    self.shrink()?;
}
``` [11](#0-10) 

### Proof of Concept

The existing unit test in `tx-pool/src/component/tests/recent_reject.rs` already demonstrates the bug: it inserts 160 entries (80 + 80) against a `count_limit` of 100 and asserts `total_keys_num < 100`. However, the assertion checks the in-memory counter (which stays at 0), not the actual RocksDB key count. A correct test would call `estimate_total_keys_num()` directly and assert the actual DB key count does not exceed `count_limit * (1 + 1/shard_num)`. [12](#0-11) 

To reproduce: instantiate `RecentReject::build` with `shard_num=2, count_limit=100`, insert 1000 unique rejected transactions, then call `estimate_total_keys_num()` — it will return ~1000, far exceeding `count_limit`.

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L39-52)
```rust
        let estimate_keys_num = cf_names
            .iter()
            .map(|cf| db.estimate_num_keys_cf(cf))
            .collect::<Result<Vec<_>, _>>()?;

        let total_keys_num = Self::checked_estimate_sum(&estimate_keys_num)?;

        Ok(RecentReject {
            shard_num,
            count_limit,
            ttl,
            db,
            total_keys_num,
        })
```

**File:** tx-pool/src/component/recent_reject.rs (L55-71)
```rust
    pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
        let hash_slice = hash.as_slice();
        let shard = self.get_shard(hash_slice).to_string();
        let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
        let json_string = serde_json::to_string(&reject)?;
        self.db.put(&shard, hash_slice, json_string)?;

        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
        Ok(())
    }
```

**File:** tx-pool/src/component/recent_reject.rs (L104-113)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
    }
```

**File:** util/types/src/core/tx_pool.rs (L89-97)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
    }
```

**File:** util/types/src/core/tx_pool.rs (L99-102)
```rust
    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/process.rs (L513-525)
```rust
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
```

**File:** shared/src/shared_builder.rs (L580-585)
```rust
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }
```

**File:** sync/src/relayer/mod.rs (L59-92)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;

type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/transactions_process.rs (L85-93)
```rust
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L1-42)
```rust
use ckb_hash::blake2b_256;
use ckb_types::{core::tx_pool::Reject, packed::Byte32};

use crate::component::recent_reject::RecentReject;

#[test]
fn test_basic() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 100;
    let ttl = -1;

    let mut recent_reject = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        let reject: ckb_jsonrpc_types::PoolTransactionReject =
            Reject::Malformed(i.to_string(), Default::default()).into();
        assert_eq!(
            recent_reject.get(&key).unwrap().unwrap(),
            serde_json::to_string(&reject).unwrap()
        )
    }

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    assert!(recent_reject.total_keys_num < 100);
}


```
