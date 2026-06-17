### Title
Unbounded `ids[]` Array in Hermes SSE Streaming Endpoint Enables Sustained Resource Exhaustion — (`File: apps/hermes/server/src/api/rest/v2/sse.rs`)

---

### Summary

The Hermes price data availability server accepts an unbounded `ids[]` query parameter array across all REST and SSE endpoints. The `validate_price_ids` helper only checks whether IDs exist in the known set — it enforces no upper count limit. The SSE streaming endpoint (`/v2/updates/price/stream`) is the most impactful target: a single connection subscribing to all known price feeds causes `get_price_feeds_with_update_data` to be invoked for the full ID list on every aggregation event (~every 400 ms). Multiple concurrent such connections can sustain high CPU and memory pressure on the Hermes node, degrading or halting price update availability for all downstream consumers.

---

### Finding Description

**Root cause — no count limit in `validate_price_ids`:**

`validate_price_ids` in `apps/hermes/server/src/api/rest.rs` partitions the caller-supplied slice into valid/invalid IDs against the known set, but imposes no maximum on how many IDs may be requested:

```rust
// apps/hermes/server/src/api/rest.rs  lines 98-124
pub async fn validate_price_ids<S>(
    state: &ApiState<S>,
    price_ids: &[PriceIdentifier],   // ← no length check
    remove_invalid: bool,
) -> Result<Vec<PriceIdentifier>, RestError> {
    let available_ids = Aggregates::get_price_feed_ids(state).await;
    let (valid_ids, invalid_ids): (Vec<_>, Vec<_>) = price_ids
        .iter()
        .copied()
        .partition(|id| available_ids.contains(id));
    ...
}
``` [1](#0-0) 

**SSE endpoint — repeated per-event processing of the full ID list:**

`price_stream_sse_handler` in `sse.rs` accepts `ids: Vec<PriceIdInput>` with no bound, calls `validate_price_ids`, then on every `AggregationEvent` (fired per Pythnet slot, ~400 ms) invokes `get_price_feeds_with_update_data` for the entire validated list:

```rust
// apps/hermes/server/src/api/rest/v2/sse.rs  lines 80, 128-130
ids: Vec<PriceIdInput>,   // no max
...
let price_ids: Vec<PriceIdentifier> =
    validate_price_ids(&state, &price_id_inputs, params.ignore_invalid_price_ids).await?;
``` [2](#0-1) [3](#0-2) 

Inside `handle_aggregation_event`, for every slot event the server fetches, constructs Merkle proofs for, and encodes all subscribed IDs:

```rust
// sse.rs  lines 243-248
let mut price_feeds_with_update_data = Aggregates::get_price_feeds_with_update_data(
    &*state.state,
    &price_ids,          // ← full unbounded list, every slot
    RequestTime::AtSlot(event.slot()),
)
.await?;
``` [4](#0-3) 

`get_verified_price_feeds` in `aggregate.rs` calls `fetch_message_states` for every ID in the list, then constructs per-feed update data (Merkle proof serialization): [5](#0-4) 

**Same pattern in REST endpoints:**

`latest_price_updates`, `timestamp_price_updates`, `latest_price_feeds`, and `latest_vaas` all accept `ids: Vec<PriceIdInput>` with no count limit and pass the full list to `get_price_feeds_with_update_data`: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

Hermes is the sole data availability layer between Pythnet and all target-chain consumers. If Hermes is degraded, no price updates can be fetched and submitted to on-chain Pyth contracts, halting all pull-oracle consumers.

An attacker opens many concurrent SSE connections, each subscribing to all ~500 known price feeds. Every ~400 ms, each connection triggers a full cache scan, Merkle proof construction, and encoding pass for 500 feeds. With N concurrent connections this multiplies linearly. The server has no per-connection ID count limit and no visible per-IP SSE connection cap in the reviewed code. Sustained load can exhaust CPU and memory, causing Hermes to become unresponsive.

---

### Likelihood Explanation

The SSE endpoint (`/v2/updates/price/stream`) is publicly reachable (optionally token-gated, but tokens are available to developers). The attack requires only an HTTP client capable of opening concurrent connections — no privileged access, no key compromise, no on-chain interaction. The full list of valid price feed IDs is publicly enumerable via `/v2/price_feeds`. The attack is trivially scriptable.

---

### Recommendation

1. **Enforce a maximum `ids[]` count early** — before `validate_price_ids` is called — in all handlers that accept `ids: Vec<PriceIdInput>`. A reasonable limit (e.g., 100 IDs per request/connection) should be enforced and return HTTP 400 immediately if exceeded.
2. **Add per-IP SSE connection limits** to prevent a single source from opening many concurrent streaming connections.
3. Consider a global cap on total active SSE connections to bound worst-case server load.

---

### Proof of Concept

```bash
# Enumerate all known price feed IDs
IDS=$(curl -s https://hermes.pyth.network/v2/price_feeds | jq -r '.[].id' | head -500)

# Build query string with all IDs
QUERY=$(echo "$IDS" | awk '{printf "&ids[]=%s", $1}')

# Open 50 concurrent SSE connections, each subscribing to all 500 feeds
for i in $(seq 1 50); do
  curl -sN "https://hermes.pyth.network/v2/updates/price/stream?$QUERY" &
done
wait
```

Each connection causes the server to execute `get_price_feeds_with_update_data` for 500 feeds on every Pythnet slot (~400 ms). With 50 connections this is 50 × 500 = 25,000 feed-lookups per 400 ms, sustained indefinitely for up to 24 hours per connection.

### Citations

**File:** apps/hermes/server/src/api/rest.rs (L98-113)
```rust
pub async fn validate_price_ids<S>(
    state: &ApiState<S>,
    price_ids: &[PriceIdentifier],
    remove_invalid: bool,
) -> Result<Vec<PriceIdentifier>, RestError>
where
    S: Aggregates,
{
    let state = &*state.state;
    let available_ids = Aggregates::get_price_feed_ids(state).await;

    // Partition into (valid_ids, invalid_ids)
    let (valid_ids, invalid_ids): (Vec<_>, Vec<_>) = price_ids
        .iter()
        .copied()
        .partition(|id| available_ids.contains(id));
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L80-80)
```rust
    ids: Vec<PriceIdInput>,
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L128-130)
```rust
    let price_id_inputs: Vec<PriceIdentifier> = params.ids.into_iter().map(Into::into).collect();
    let price_ids: Vec<PriceIdentifier> =
        validate_price_ids(&state, &price_id_inputs, params.ignore_invalid_price_ids).await?;
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L243-248)
```rust
    let mut price_feeds_with_update_data = Aggregates::get_price_feeds_with_update_data(
        &*state.state,
        &price_ids,
        RequestTime::AtSlot(event.slot()),
    )
    .await?;
```

**File:** apps/hermes/server/src/state/aggregate.rs (L552-609)
```rust
async fn get_verified_price_feeds<S>(
    state: &S,
    price_ids: &[PriceIdentifier],
    request_time: RequestTime,
) -> Result<PriceFeedsWithUpdateData>
where
    S: Cache,
{
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

    let price_feeds = messages
        .iter()
        .map(|message_state| match message_state.message {
            Message::PriceFeedMessage(price_feed) => Ok(PriceFeedUpdate {
                price_feed: PriceFeed::new(
                    PriceIdentifier::new(price_feed.feed_id),
                    Price {
                        price: price_feed.price,
                        conf: price_feed.conf,
                        expo: price_feed.exponent,
                        publish_time: price_feed.publish_time,
                    },
                    Price {
                        price: price_feed.ema_price,
                        conf: price_feed.ema_conf,
                        expo: price_feed.exponent,
                        publish_time: price_feed.publish_time,
                    },
                ),
                received_at: Some(message_state.received_at),
                slot: Some(message_state.slot),
                update_data: Some(
                    construct_update_data(vec![message_state.clone().into()])?
                        .into_iter()
                        .next()
                        .ok_or(anyhow!("Missing update data for message"))?,
                ),
                prev_publish_time: Some(price_feed.prev_publish_time),
            }),
            _ => Err(anyhow!("Invalid message state type")),
        })
        .collect::<Result<Vec<_>>>()?;

    let update_data = construct_update_data(messages.into_iter().map(|m| m.into()).collect())?;

    Ok(PriceFeedsWithUpdateData {
        price_feeds,
        update_data,
    })
```

**File:** apps/hermes/server/src/api/rest/v2/latest_price_updates.rs (L32-32)
```rust
    ids: Vec<PriceIdInput>,
```

**File:** apps/hermes/server/src/api/rest/v2/timestamp_price_updates.rs (L45-45)
```rust
    ids: Vec<PriceIdInput>,
```
