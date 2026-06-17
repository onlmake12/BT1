### Title
Unbounded, Duplicate-Permitting `ids[]` Array in Hermes REST/SSE Endpoints Enables Per-Request Server Resource Exhaustion — (`File: apps/hermes/server/src/api/rest.rs`)

---

### Summary

The Hermes price-feed server accepts an unbounded `ids[]` query-parameter array on every public REST and SSE endpoint. The shared validation helper `validate_price_ids` checks only whether each ID exists in the known set; it enforces no maximum count and does not deduplicate entries. An unprivileged caller can therefore submit a single request containing thousands of copies of the same valid price-feed ID, forcing the server to allocate and process proportionally large data structures. On the long-lived SSE streaming endpoint this cost is paid repeatedly on every price-update event, creating sustained resource exhaustion.

---

### Finding Description

`validate_price_ids` in `apps/hermes/server/src/api/rest.rs` partitions the caller-supplied slice into valid and invalid IDs using `.partition()`:

```rust
let (valid_ids, invalid_ids): (Vec<_>, Vec<_>) = price_ids
    .iter()
    .copied()
    .partition(|id| available_ids.contains(id));
``` [1](#0-0) 

There is no check on `price_ids.len()` before or after this call, and `.partition()` preserves every duplicate. The returned `valid_ids` vector is then passed directly to `Aggregates::get_price_feeds_with_update_data`.

Every endpoint that calls this helper is affected:

| Endpoint | File |
|---|---|
| `GET /v2/updates/price/latest` | `rest/v2/latest_price_updates.rs` |
| `GET /v2/updates/price/{timestamp}` | `rest/v2/timestamp_price_updates.rs` |
| `GET /v2/updates/price/stream` (SSE) | `rest/v2/sse.rs` |
| `GET /api/latest_price_feeds` | `rest/latest_price_feeds.rs` |
| `GET /api/latest_vaas` | `rest/latest_vaas.rs` | [2](#0-1) [3](#0-2) [4](#0-3) 

The SSE handler captures the validated (duplicate-preserving) `price_ids` vector in a closure that fires on **every** `AggregationEvent`:

```rust
.then(move |message| {
    let price_ids_clone = price_ids.clone();   // N-element vec cloned per event
    async move {
        handle_aggregation_event(event, state_clone, price_ids_clone, ...).await
    }
})
``` [5](#0-4) 

`handle_aggregation_event` calls `get_price_feeds_with_update_data` with the full (duplicate) list on every event, allocating proportional output arrays each time. [6](#0-5) 

By contrast, the WebSocket handler inserts IDs into a `HashMap`, which naturally deduplicates them — the REST/SSE path has no equivalent protection.

---

### Impact Explanation

A single SSE connection opened with N copies of a known valid price-feed ID (e.g., BTC/USD, whose ID is publicly listed) causes the server to:

1. Allocate an N-element `Vec<PriceIdentifier>` at connection time.
2. On every price-update event (Pythnet produces slots roughly every 400 ms), clone that vector, call `get_price_feeds_with_update_data` with N entries, allocate an N-element response array, and serialize N copies of the same price-feed struct into the SSE event body.

With N = 10 000 and ~2.5 events/second, the server performs ~25 000 redundant lookups and allocations per second per connection. The SSE connection may remain open for up to 24 hours. Multiple such connections compound the effect. The result is CPU and memory exhaustion on the Hermes node, preventing legitimate price-update delivery to on-chain contracts and breaking any DeFi protocol that relies on Pyth pull-oracle updates.

---

### Likelihood Explanation

- All affected endpoints are publicly reachable without authentication on `hermes.pyth.network`.
- Valid price-feed IDs are publicly enumerated at `https://pyth.network/developers/price-feed-ids`.
- The attack requires only a single HTTP GET request with a long query string; no special tooling is needed.
- The per-IP rate limit (10 requests / 10 s) limits connection establishment but does not bound the per-connection processing cost once the SSE stream is open.
- No privileged role, leaked key, or governance majority is required.

---

### Recommendation

1. **Enforce a maximum array length** in `validate_price_ids` (or at each call site) before any processing:
   ```rust
   const MAX_IDS: usize = 500; // tune to operational needs
   if price_ids.len() > MAX_IDS {
       return Err(RestError::TooManyPriceIds);
   }
   ```
2. **Deduplicate** the input before validation:
   ```rust
   let price_ids: Vec<_> = price_ids.iter().copied().collect::<std::collections::HashSet<_>>().into_iter().collect();
   ```
3. Apply the same guards to the SSE endpoint's query-parameter parsing so that the long-lived stream cannot be seeded with an oversized ID list.

---

### Proof of Concept

```bash
# Construct a query string with 5000 copies of the BTC/USD feed ID
FEED=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43
QS=$(python3 -c "print('&'.join(['ids[]=' + '$FEED']*5000))")

# Open a long-lived SSE connection — server will process 5000 IDs on every slot
curl -N "https://hermes.pyth.network/v2/updates/price/stream?$QS"
```

Every Pythnet slot (~400 ms) the server allocates and serializes 5 000 copies of the BTC/USD price feed for this single connection. Opening several such connections from different IPs saturates Hermes CPU/memory, halting price-update delivery to all downstream on-chain consumers.

### Citations

**File:** apps/hermes/server/src/api/rest.rs (L109-113)
```rust
    // Partition into (valid_ids, invalid_ids)
    let (valid_ids, invalid_ids): (Vec<_>, Vec<_>) = price_ids
        .iter()
        .copied()
        .partition(|id| available_ids.contains(id));
```

**File:** apps/hermes/server/src/api/rest/v2/latest_price_updates.rs (L72-79)
```rust
    let price_id_inputs: Vec<PriceIdentifier> =
        params.ids.into_iter().map(|id| id.into()).collect();
    let price_ids: Vec<PriceIdentifier> =
        validate_price_ids(&state, &price_id_inputs, params.ignore_invalid_price_ids).await?;

    let state = &*state.state;
    let price_feeds_with_update_data =
        Aggregates::get_price_feeds_with_update_data(state, &price_ids, RequestTime::Latest)
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L128-130)
```rust
    let price_id_inputs: Vec<PriceIdentifier> = params.ids.into_iter().map(Into::into).collect();
    let price_ids: Vec<PriceIdentifier> =
        validate_price_ids(&state, &price_id_inputs, params.ignore_invalid_price_ids).await?;
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L154-170)
```rust
            .then(move |message| {
                let state_clone = state.clone();
                let price_ids_clone = price_ids.clone();
                let should_end = should_end.clone();
                async move {
                    match message {
                        Ok(event) => {
                            match handle_aggregation_event(
                                event,
                                state_clone,
                                price_ids_clone,
                                params.encoding,
                                params.parsed,
                                params.benchmarks_only,
                                params.allow_unordered,
                            )
                            .await
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

**File:** apps/hermes/server/src/api/rest/latest_vaas.rs (L56-57)
```rust
    let price_ids: Vec<PriceIdentifier> = params.ids.into_iter().map(|id| id.into()).collect();
    validate_price_ids(&state, &price_ids, false).await?;
```
