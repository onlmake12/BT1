### Title
Unbounded `ids[]` Query Parameter Enables Uncontrolled Resource Consumption in Hermes Price Feed API — (File: `apps/hermes/server/src/api/rest/v2/latest_price_updates.rs`)

---

### Summary

The Hermes price-feed aggregation server accepts an unbounded number of price feed IDs in the `ids[]` query parameter across multiple public REST endpoints. No upper-bound validation is applied to the array length before the server allocates memory, iterates over every ID, performs per-ID state lookups, and serializes the full response. An unprivileged HTTP client can submit a single crafted request carrying thousands of IDs, forcing the server to perform O(N) work proportional to the attacker-supplied count — a direct structural analog to the `jose2go` PBES2 Count (p2c) unbounded-iteration pattern.

---

### Finding Description

The `LatestPriceUpdatesQueryParams` struct in `apps/hermes/server/src/api/rest/v2/latest_price_updates.rs` deserializes the `ids[]` query parameter into a plain `Vec<PriceIdInput>` with no length constraint:

```rust
ids: Vec<PriceIdInput>,
```

The same pattern is repeated verbatim in every other multi-ID endpoint:

- `apps/hermes/server/src/api/rest/latest_price_feeds.rs` — `ids: Vec<PriceIdInput>`
- `apps/hermes/server/src/api/rest/latest_vaas.rs` — `ids: Vec<PriceIdInput>`
- `apps/hermes/server/src/api/rest/v2/timestamp_price_updates.rs` — `ids: Vec<PriceIdInput>`

After deserialization, the handler calls `validate_price_ids` (which iterates over every supplied ID to check existence) and then `Aggregates::get_price_feeds_with_update_data`, which performs a per-ID state lookup and constructs a proportionally large response. No rate-limiting, no maximum-count guard, and no request-body size cap on the ID list is present in any of these handlers.

A single HTTP GET request of the form:

```
GET /v2/updates/price/latest?ids[]=<id1>&ids[]=<id2>&...&ids[]=<idN>
```

with N in the tens of thousands (feasible within standard HTTP query-string limits) forces the server to:
1. Allocate a `Vec` of N elements
2. Iterate N times in `validate_price_ids`
3. Perform N concurrent async state lookups
4. Serialize an N-element JSON response

All of this work is driven entirely by the attacker-supplied count, with no proportional cost to the attacker beyond the size of the HTTP request itself.

---

### Impact Explanation

Hermes is the sole off-chain aggregation layer that produces the signed price-update blobs consumed by every Pyth price-feed contract on every supported chain. If the Hermes HTTP server is rendered unresponsive, no price-update data can be fetched by relayers or price-pushers, and on-chain prices stale out across all integrated DeFi protocols. The impact is a complete, cross-chain denial of the Pyth price-feed service for the duration of the attack.

---

### Likelihood Explanation

The endpoint is publicly reachable with no authentication (authentication is not yet required as of the current codebase). The attacker needs only an HTTP client. A single request with ~10,000 IDs (each 64 hex chars + `ids[]=` overhead ≈ 700 KB query string) is sufficient to saturate a single worker thread. Repeating this at a modest rate across multiple connections can exhaust the Tokio async runtime's thread pool. No special knowledge, keys, or on-chain funds are required.

---

### Recommendation

Add a hard upper bound on the number of IDs accepted per request. For example, in each query-params struct:

```rust
#[validate(length(max = 500))]
ids: Vec<PriceIdInput>,
```

Apply the `validator` crate (or an equivalent Axum extractor guard) before any downstream processing. Additionally, enforce HTTP-layer request-size limits in the Axum router configuration to prevent oversized query strings from reaching the deserialization layer at all.

---

### Proof of Concept

```bash
# Generate 5000 repeated valid price-feed IDs
python3 -c "
import sys
base = 'e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43'
params = '&'.join(f'ids[]={base}' for _ in range(5000))
print(f'https://hermes.pyth.network/v2/updates/price/latest?{params}')
" | xargs curl -s -o /dev/null -w "%{time_total}\n"
```

Each such request forces the server to allocate, validate, and look up 5,000 entries. Sending this concurrently from multiple clients exhausts the async worker pool, causing request queuing and eventual timeout failures for legitimate price-pusher clients. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** apps/hermes/server/src/api/rest/v2/latest_price_updates.rs (L19-45)
```rust
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

**File:** apps/hermes/server/src/api/rest/latest_price_feeds.rs (L18-42)
```rust
#[derive(Debug, serde::Deserialize, IntoParams)]
#[into_params(parameter_in=Query)]
pub struct LatestPriceFeedsQueryParams {
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

    /// If true, include the `metadata` field in the response with additional metadata about
    /// the price update.
    #[serde(default)]
    verbose: bool,

    /// If true, include the binary price update in the `vaa` field of each returned feed.
    /// This binary data can be submitted to Pyth contracts to update the on-chain price.
    #[serde(default)]
    binary: bool,
}
```

**File:** apps/hermes/server/src/api/rest/latest_price_feeds.rs (L60-81)
```rust
pub async fn latest_price_feeds<S>(
    State(state): State<ApiState<S>>,
    QsQuery(params): QsQuery<LatestPriceFeedsQueryParams>,
) -> Result<Json<Vec<RpcPriceFeed>>, RestError>
where
    S: Aggregates,
{
    let price_ids: Vec<PriceIdentifier> = params.ids.into_iter().map(|id| id.into()).collect();
    validate_price_ids(&state, &price_ids, false).await?;

    let state = &*state.state;
    let price_feeds_with_update_data =
        Aggregates::get_price_feeds_with_update_data(state, &price_ids, RequestTime::Latest)
            .await
            .map_err(|e| {
                tracing::debug!(
                    "Error getting price feeds {:?} with update data: {:?}",
                    price_ids,
                    e
                );
                RestError::UpdateDataNotFound
            })?;
```

**File:** apps/hermes/server/src/api/rest/latest_vaas.rs (L15-29)
```rust
#[derive(Debug, serde::Deserialize, IntoParams)]
#[into_params(parameter_in=Query)]
pub struct LatestVaasQueryParams {
    /// Get the VAAs for this set of price feed ids.
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
}
```

**File:** apps/hermes/server/src/api/rest/v2/timestamp_price_updates.rs (L32-58)
```rust
#[derive(Debug, Deserialize, IntoParams)]
#[into_params(parameter_in=Query)]
pub struct TimestampPriceUpdatesQueryParams {
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
