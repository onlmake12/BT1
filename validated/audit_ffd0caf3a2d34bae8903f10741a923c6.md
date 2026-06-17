### Title
Unbounded `ids[]` Parameter in Hermes REST API Enables DoS via Excessive Proof Serialization — (File: `apps/hermes/server/src/api/rest/v2/latest_price_updates.rs`)

### Summary
The Hermes REST API endpoints that serve price update proofs accept an unbounded `ids[]` array parameter with no maximum count validation. An unprivileged user can submit a single request containing all valid price feed IDs (or, on v2 endpoints with `ignore_invalid_price_ids=true`, an arbitrarily large list of IDs), forcing the server to acquire a read lock on the message cache, perform per-ID BTreeMap lookups, serialize Merkle proof data, and encode the full response for every ID. With no rate limiting and no cap on the number of IDs, this is a direct DoS vector against public Hermes instances.

---

### Finding Description

The following Hermes REST API endpoints all accept a `Vec<PriceIdInput>` for the `ids[]` query parameter with no enforced maximum:

- `GET /v2/updates/price/latest` — `LatestPriceUpdatesQueryParams.ids`
- `GET /v2/updates/price/{publish_time}` — `TimestampPriceUpdatesQueryParams.ids`
- `GET /api/latest_price_feeds` — `LatestPriceFeedsQueryParams.ids`
- `GET /api/latest_vaas` — `LatestVaasQueryParams.ids` [1](#0-0) 

Every request, regardless of the number of IDs, passes through `validate_price_ids`, which unconditionally acquires a read lock on the entire message cache and iterates over all stored keys to build a `HashSet<PriceIdentifier>`: [2](#0-1) 

`get_price_feed_ids` is called inside `validate_price_ids` and acquires the read lock on every invocation: [3](#0-2) 

`message_state_keys` holds the read lock while iterating all cache entries: [4](#0-3) 

After validation, `get_price_feeds_with_update_data` is called, which calls `fetch_message_states` for every requested ID, then calls `construct_update_data` to serialize Merkle proof data for all messages: [5](#0-4) 

On the v2 endpoints, the `ignore_invalid_price_ids` flag (defaulting to `false` but settable by the caller) allows an attacker to submit an arbitrarily large list of random IDs. The server will parse every ID, acquire the cache read lock, and perform a `HashSet::contains` check for each one before filtering them out — all at zero cost to the attacker: [6](#0-5) 

---

### Impact Explanation

Public Hermes instances are critical infrastructure for the Pyth pull oracle model. If Hermes is unavailable, downstream users cannot fetch price update proofs to submit to on-chain contracts, breaking all dependent DeFi protocols. An attacker can:

1. Send a single request with all ~500+ valid price feed IDs, forcing the server to serialize Merkle proofs and encode a large response for every ID.
2. Flood the server with such requests concurrently, causing read-lock contention on the message cache and CPU exhaustion from proof serialization and base64/hex encoding.
3. On v2 endpoints with `ignore_invalid_price_ids=true`, send requests with millions of random IDs, forcing the server to parse and hash-check each one with no useful work done.

---

### Likelihood Explanation

The Hermes REST API is publicly accessible with no authentication required (authentication is only being phased in as of July 31, 2026 per documentation). Any unprivileged user can craft such a request with a standard HTTP client. The attack requires no special knowledge, no on-chain transaction, and no funds. The cost to the attacker is negligible (a single HTTP GET request), while the cost to the server scales linearly with the number of IDs.

---

### Recommendation

1. Enforce a maximum count on the `ids[]` parameter (e.g., 100 IDs per request) in all affected query parameter structs.
2. Add per-IP or per-connection rate limiting at the API layer.
3. Cache the result of `get_price_feed_ids` with a short TTL instead of recomputing it on every request.
4. For `ignore_invalid_price_ids=true` requests, validate the count of input IDs before performing any cache operations.

---

### Proof of Concept

```bash
# Collect all valid price feed IDs from Hermes
IDS=$(curl -s 'https://hermes.pyth.network/api/price_feed_ids' | jq -r '.[] | "ids[]=" + .' | tr '\n' '&')

# Single request with all ~500 valid IDs — forces full proof serialization
curl -s "https://hermes.pyth.network/v2/updates/price/latest?${IDS}" > /dev/null

# With ignore_invalid_price_ids=true, send millions of random IDs
python3 -c "
import requests, os
ids = '&'.join(f'ids[]={os.urandom(32).hex()}' for _ in range(100000))
requests.get(f'https://hermes.pyth.network/v2/updates/price/latest?{ids}&ignore_invalid_price_ids=true')
"
```

The first variant forces the server to serialize Merkle proofs for all available feeds. The second variant forces the server to parse and hash-check 100,000 random IDs per request, with no rate limiting to prevent repeated calls.

### Citations

**File:** apps/hermes/server/src/api/rest/v2/latest_price_updates.rs (L1-46)
```rust
use {
    crate::{
        api::{
            rest::{validate_price_ids, RestError},
            types::{BinaryUpdate, EncodingType, ParsedPriceUpdate, PriceIdInput, PriceUpdate},
            ApiState,
        },
        state::aggregate::{Aggregates, RequestTime},
    },
    anyhow::Result,
    axum::{extract::State, Json},
    base64::{engine::general_purpose::STANDARD as base64_standard_engine, Engine as _},
    pyth_sdk::PriceIdentifier,
    serde::Deserialize,
    serde_qs::axum::QsQuery,
    utoipa::IntoParams,
};

#[derive(Debug, Deserialize, IntoParams)]
#[into_params(parameter_in=Query)]
pub struct LatestPriceUpdatesQueryParams {
    /// Get the most recent price update for this set of price feed ids.
    ///
    /// This parameter can be provided multiple times to retrieve multiple price updates,
    /// for example see the following query string:
    ///
    /// ```
    /// ?ids[]=a12...&ids[]=b4c...
    /// ```
    #[param(rename = "ids[]")]
    #[param(example = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43")]
    ids: Vec<PriceIdInput>,

    /// Optional encoding type. If true, return the price update in the encoding specified by the encoding parameter. Default is `hex`.
    #[serde(default)]
    encoding: EncodingType,

    /// If true, include the parsed price update in the `parsed` field of each returned feed. Default is `true`.
    #[serde(default = "default_true")]
    parsed: bool,

    /// If true, invalid price IDs in the `ids` parameter are ignored. Only applicable to the v2 APIs. Default is `false`.
    #[serde(default)]
    ignore_invalid_price_ids: bool,
}

```

**File:** apps/hermes/server/src/api/rest.rs (L98-124)
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

    if invalid_ids.is_empty() || remove_invalid {
        // All IDs are valid
        Ok(valid_ids)
    } else {
        // Return error with list of missing IDs
        Err(RestError::PriceIdsNotFound {
            missing_ids: invalid_ids,
        })
    }
}
```

**File:** apps/hermes/server/src/state/aggregate.rs (L451-458)
```rust
    async fn get_price_feed_ids(&self) -> HashSet<PriceIdentifier> {
        Cache::message_state_keys(self)
            .await
            .iter()
            .filter(|key| key.feed_id != PUBLISHER_STAKE_CAPS_MESSAGE_FEED_ID)
            .map(|key| PriceIdentifier::new(key.feed_id))
            .collect()
    }
```

**File:** apps/hermes/server/src/state/aggregate.rs (L552-610)
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
}
```

**File:** apps/hermes/server/src/state/cache.rs (L147-155)
```rust
    async fn message_state_keys(&self) -> Vec<MessageStateKey> {
        self.into()
            .message_cache
            .read()
            .await
            .iter()
            .map(|entry| entry.0.clone())
            .collect::<Vec<_>>()
    }
```

**File:** apps/hermes/server/src/api/rest/v2/timestamp_price_updates.rs (L55-94)
```rust
    /// If true, invalid price IDs in the `ids` parameter are ignored. Only applicable to the v2 APIs. Default is `false`.
    #[serde(default)]
    ignore_invalid_price_ids: bool,
}

fn default_true() -> bool {
    true
}

/// Get the latest price updates by price feed id.
///
/// Given a collection of price feed ids, retrieve the latest Pyth price for each price feed.
#[utoipa::path(
    get,
    path = "/v2/updates/price/{publish_time}",
    responses(
        (status = 200, description = "Price updates retrieved successfully", body = PriceUpdate),
        (status = 404, description = "Price ids not found", body = String)
    ),
    params(
        TimestampPriceUpdatesPathParams,
        TimestampPriceUpdatesQueryParams
    )
)]
pub async fn timestamp_price_updates<S>(
    State(state): State<ApiState<S>>,
    Path(path_params): Path<TimestampPriceUpdatesPathParams>,
    QsQuery(query_params): QsQuery<TimestampPriceUpdatesQueryParams>,
) -> Result<Json<PriceUpdate>, RestError>
where
    S: Aggregates,
{
    let price_id_inputs: Vec<PriceIdentifier> =
        query_params.ids.into_iter().map(|id| id.into()).collect();
    let price_ids: Vec<PriceIdentifier> = validate_price_ids(
        &state,
        &price_id_inputs,
        query_params.ignore_invalid_price_ids,
    )
    .await?;
```
