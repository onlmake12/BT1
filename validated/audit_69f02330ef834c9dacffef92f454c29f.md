### Title
Unbounded `ids[]` Parameter Causes Read-Lock Contention on Shared `message_cache` RwLock, Blocking Price-Update Ingestion in Hermes - (File: apps/hermes/server/src/state/cache.rs, apps/hermes/server/src/api/rest/v2/latest_price_updates.rs)

---

### Summary

Hermes REST endpoints (`/v2/updates/price/latest`, `/v2/updates/price/{publish_time}`, `/api/latest_price_feeds`, `/api/latest_vaas`) accept an unbounded `ids[]` query-parameter array with no server-side cardinality limit. Each request acquires concurrent read locks on the shared `message_cache` `RwLock`. The same `RwLock` write path is used by the price-update ingestion pipeline (`store_message_states`). Flooding the endpoints with large ID lists from many concurrent connections starves the write lock, delaying or blocking the storage of fresh price-feed updates and degrading Hermes's ability to serve current price data.

---

### Finding Description

**Entry point — no bounds on `ids[]`:**

`LatestPriceUpdatesQueryParams` and `TimestampPriceUpdatesQueryParams` declare `ids: Vec<PriceIdInput>` with no `#[validate(length(max = ...))]` or equivalent guard. [1](#0-0) [2](#0-1) 

The same pattern exists in the deprecated v1 endpoints: [3](#0-2) [4](#0-3) 

**Shared `message_cache` RwLock — read path (API):**

`validate_price_ids` acquires a read lock on `message_cache` (via `get_price_feed_ids` → `message_state_keys`) to partition valid/invalid IDs: [5](#0-4) 

Then `get_verified_price_feeds` → `fetch_message_states` spawns one `retrieve_message_state` future **per ID** via `join_all`, each acquiring its own read lock on `message_cache`: [6](#0-5) [7](#0-6) 

**Shared `message_cache` RwLock — write path (price-update ingestion):**

`store_message_states` acquires an exclusive write lock on the same `message_cache` for the entire duration of storing all incoming price-feed updates: [8](#0-7) 

Tokio's `RwLock` is fair/write-preferring: a pending write lock is queued behind active readers, and new readers arriving after the write is queued are also blocked. Many concurrent API requests each holding read locks therefore delay `store_message_states`, which is called on every completed Pythnet slot update.

---

### Impact Explanation

Delaying `store_message_states` means Hermes cannot commit fresh price-feed `MessageState` entries. Downstream effects:

- API consumers receive stale price data (or `UpdateDataNotFound` errors).
- WebSocket subscribers (`handle_price_feeds_update`) also call `get_price_feeds_with_update_data` and are affected.
- On-chain price-update transactions submitted by price pushers are based on stale VAAs, reducing oracle freshness guarantees. [9](#0-8) 

---

### Likelihood Explanation

- The public Hermes endpoint has a rate limit of 10 req/10 s per IP, but self-hosted Hermes instances (used by many integrators and price pushers) have no such limit by default.
- An attacker can use many source IPs or target self-hosted instances directly.
- With `ignore_invalid_price_ids=true`, the attacker can send arbitrarily large lists; valid IDs are filtered but the parsing and `validate_price_ids` iteration over the full input list still consumes CPU and holds the read lock.
- Even with all-invalid IDs and `ignore_invalid_price_ids=false`, `validate_price_ids` still acquires the read lock to obtain the known-ID set before returning an error, so each request contributes to read-lock pressure.
- Concurrent flooding from multiple connections is straightforward with standard HTTP tooling.

---

### Recommendation

1. **Add a hard cap on `ids[]` cardinality** at the handler level (e.g., 500 IDs, matching the realistic number of Pyth price feeds):
   ```rust
   if params.ids.len() > MAX_IDS_PER_REQUEST {
       return Err(RestError::TooManyIds);
   }
   ```
2. **Decouple the read and write lock scopes**: consider using `DashMap` for `message_cache` (already noted in the cache comments for other caches) so per-key reads do not block global writes.
3. **Apply rate limiting at the infrastructure level** for self-hosted deployments (document this requirement).

---

### Proof of Concept

```bash
# Flood /v2/updates/price/latest with all known valid IDs repeated across many concurrent connections
# Step 1: collect all known feed IDs
FEEDS=$(curl -s https://hermes.pyth.network/api/price_feed_ids | jq -r '.[]' | \
  awk '{printf "ids[]=%s&", $1}')

# Step 2: send many concurrent requests
for i in $(seq 1 200); do
  curl -s "http://<self-hosted-hermes>/v2/updates/price/latest?${FEEDS}" &
done
wait
```

Each concurrent request acquires read locks on `message_cache` via `fetch_message_states`. With 200 concurrent connections each requesting all ~500 feeds, 100,000 concurrent read-lock acquisitions are in flight. The `store_message_states` write lock (triggered every ~400 ms by a new Pythnet slot) is starved, causing Hermes to serve increasingly stale price data until the flood subsides. [10](#0-9) [11](#0-10)

### Citations

**File:** apps/hermes/server/src/api/rest/v2/latest_price_updates.rs (L30-32)
```rust
    #[param(rename = "ids[]")]
    #[param(example = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43")]
    ids: Vec<PriceIdInput>,
```

**File:** apps/hermes/server/src/api/rest/v2/timestamp_price_updates.rs (L43-45)
```rust
    #[param(rename = "ids[]")]
    #[param(example = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43")]
    ids: Vec<PriceIdInput>,
```

**File:** apps/hermes/server/src/api/rest/latest_price_feeds.rs (L29-31)
```rust
    #[param(rename = "ids[]")]
    #[param(example = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43")]
    ids: Vec<PriceIdInput>,
```

**File:** apps/hermes/server/src/api/rest/latest_vaas.rs (L26-28)
```rust
    #[param(rename = "ids[]")]
    #[param(example = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43")]
    ids: Vec<PriceIdInput>,
```

**File:** apps/hermes/server/src/api/rest.rs (L106-113)
```rust
    let state = &*state.state;
    let available_ids = Aggregates::get_price_feed_ids(state).await;

    // Partition into (valid_ids, invalid_ids)
    let (valid_ids, invalid_ids): (Vec<_>, Vec<_>) = price_ids
        .iter()
        .copied()
        .partition(|id| available_ids.contains(id));
```

**File:** apps/hermes/server/src/state/cache.rs (L83-98)
```rust
/// A Cache of AccumulatorMessage by slot. We do not write to this cache much, so we can use a simple RwLock instead of a DashMap.
type AccumulatorMessagesCache = Arc<RwLock<BTreeMap<Slot, AccumulatorMessages>>>;

/// A Cache of WormholeMerkleState by slot. We do not write to this cache much, so we can use a simple RwLock instead of a DashMap.
type WormholeMerkleStateCache = Arc<RwLock<BTreeMap<Slot, WormholeMerkleState>>>;

/// A Cache of `Time<->MessageState` by feed id.
type MessageCache = Arc<RwLock<HashMap<MessageStateKey, BTreeMap<MessageStateTime, MessageState>>>>;

/// A collection of caches for various program state.
pub struct CacheState {
    accumulator_messages_cache: AccumulatorMessagesCache,
    wormhole_merkle_state_cache: WormholeMerkleStateCache,
    message_cache: MessageCache,
    cache_size: usize,
}
```

**File:** apps/hermes/server/src/state/cache.rs (L157-173)
```rust
    async fn store_message_states(&self, message_states: Vec<MessageState>) -> Result<()> {
        let mut message_cache = self.into().message_cache.write().await;

        for message_state in message_states {
            let key = message_state.key();
            let time = message_state.time();
            let cache = message_cache.entry(key).or_insert_with(BTreeMap::new);
            cache.insert(time, message_state);

            // Remove the earliest message states if the cache size is exceeded
            while cache.len() > self.into().cache_size {
                cache.pop_first();
            }
        }

        Ok(())
    }
```

**File:** apps/hermes/server/src/state/cache.rs (L196-221)
```rust
    async fn fetch_message_states(
        &self,
        ids: Vec<FeedId>,
        request_time: RequestTime,
        filter: MessageStateFilter,
    ) -> Result<Vec<MessageState>> {
        join_all(ids.into_iter().flat_map(|id| {
            let request_time = request_time.clone();
            let message_types: Vec<MessageType> = match filter {
                MessageStateFilter::All => MessageType::iter().collect(),
                MessageStateFilter::Only(t) => vec![t],
            };

            message_types.into_iter().map(move |message_type| {
                let key = MessageStateKey {
                    feed_id: id,
                    type_: message_type,
                };
                retrieve_message_state(self.into(), key, request_time.clone())
            })
        }))
        .await
        .into_iter()
        .collect::<Option<Vec<_>>>()
        .ok_or(anyhow!("Message not found"))
    }
```

**File:** apps/hermes/server/src/state/aggregate.rs (L399-414)
```rust
    async fn get_price_feeds_with_update_data(
        &self,
        price_ids: &[PriceIdentifier],
        request_time: RequestTime,
    ) -> Result<PriceFeedsWithUpdateData> {
        match get_verified_price_feeds(self, price_ids, request_time.clone()).await {
            Ok(price_feeds_with_update_data) => Ok(price_feeds_with_update_data),
            Err(e) => {
                if let RequestTime::FirstAfter(publish_time) = request_time {
                    return Benchmarks::get_verified_price_feeds(self, price_ids, publish_time)
                        .await;
                }
                Err(e)
            }
        }
    }
```

**File:** apps/hermes/server/src/state/aggregate.rs (L560-569)
```rust
    let messages = state
        .fetch_message_states(
            price_ids
                .iter()
                .map(|price_id| price_id.to_bytes())
                .collect(),
            request_time,
            MessageStateFilter::Only(MessageType::PriceFeedMessage),
        )
        .await?;
```

**File:** apps/hermes/server/src/api/ws.rs (L510-524)
```rust
    async fn handle_price_feeds_update(&mut self, event: AggregationEvent) -> Result<()> {
        let price_feed_ids = self
            .price_feeds_with_config
            .keys()
            .cloned()
            .collect::<Vec<_>>();

        let state = &*self.state;
        let updates = match Aggregates::get_price_feeds_with_update_data(
            state,
            &price_feed_ids,
            RequestTime::AtSlot(event.slot()),
        )
        .await
        {
```
