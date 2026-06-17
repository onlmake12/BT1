### Title
Unbounded Long-Lived Streaming Connections Allow Resource Exhaustion DoS — (`apps/hermes/server/src/api/ws.rs`, `apps/hermes/server/src/api/rest/v2/sse.rs`)

---

### Summary

Hermes exposes two publicly reachable streaming endpoints — a WebSocket handler (`/ws`) and an SSE handler (`/v2/updates/price/stream`) — that accept an unlimited number of concurrent connections per IP address. Each connection holds a tokio task, a broadcast channel receiver, and associated OS resources for up to 24 hours. The only rate-limiting mechanism governs bytes *sent* to the client, not connection establishment. An unprivileged attacker can open thousands of connections from a single IP, exhausting file descriptors, memory, and tokio task capacity, causing Hermes to become unavailable.

---

### Finding Description

**WebSocket endpoint** (`ws_route_handler`):

`ws_route_handler` upgrades any incoming HTTP connection to a WebSocket and spawns a `Subscriber` task with no check on the number of existing connections from the same IP or globally. [1](#0-0) 

Each `Subscriber` holds a `broadcast::Receiver<AggregationEvent>`, a split WebSocket sink/stream, a ping interval, and a 24-hour deadline: [2](#0-1) 

The `WsState` rate limiter is keyed by IP address and limits **bytes sent per second** (256 KiB/s). It is only checked inside `handle_price_feeds_update` when data is actually pushed to the client — not at connection establishment: [3](#0-2) [4](#0-3) 

A client that connects but never subscribes to any price feeds is never rate-limited and holds its connection open for the full 24-hour window: [5](#0-4) 

**SSE endpoint** (`price_stream_sse_handler`):

The SSE handler similarly accepts any connection, subscribes it to the broadcast channel, and streams for up to 24 hours with no per-IP or global connection count check: [6](#0-5) [7](#0-6) 

The SSE path has no rate-limiting at all — not even the bytes-per-second check present in the WebSocket path.

**No global connection cap exists** in the RPC configuration: [8](#0-7) 

---

### Impact Explanation

Each open connection consumes:
- One OS file descriptor
- One tokio async task
- One `broadcast::Receiver` slot (each lagging receiver can cause the broadcast channel to back-pressure)
- Memory for the write buffer (`ws_max_write_buffer_bytes`, default 2 MiB per connection)

An attacker opening 10,000 WebSocket connections from a single IP (connecting but not subscribing) would hold 10,000 tokio tasks and file descriptors for up to 24 hours each, with zero rate-limiting applied. This can exhaust the server's file descriptor limit (`ulimit -n`), memory, or tokio thread pool, rendering Hermes unavailable. Since Hermes is the canonical gateway for Pyth price update delivery, its unavailability prevents on-chain price updates across all supported chains.

---

### Likelihood Explanation

- No authentication is required to open a WebSocket or SSE connection to the public Hermes endpoint.
- The REST rate limit (10 requests per 10 seconds per IP) applies to HTTP requests, not to persistent WebSocket/SSE connections once established.
- A single attacker IP can open thousands of connections in a short time using standard tooling (e.g., `websocat`, `curl --no-buffer`).
- The attack is self-sustaining: connections persist for 24 hours without further attacker interaction.

---

### Recommendation

1. **Enforce a per-IP connection count limit** at the WebSocket upgrade and SSE handler level, before the connection is accepted, using a concurrent connection counter keyed by IP address.
2. **Enforce a global connection cap** to bound total resource consumption regardless of IP diversity.
3. **Apply rate limiting to connection establishment**, not only to bytes sent, so that rapid connection attempts from a single IP are rejected early.
4. **Reduce the maximum connection duration** or require periodic re-authentication for long-lived connections.

---

### Proof of Concept

```bash
# Open 5000 idle WebSocket connections from a single IP (no subscription sent)
# Each holds a tokio task + file descriptor for 24 hours with zero rate-limiting
for i in $(seq 1 5000); do
  websocat ws://hermes.pyth.network/ws &
done

# Alternatively, for SSE:
for i in $(seq 1 5000); do
  curl -sN "https://hermes.pyth.network/v2/updates/price/stream?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43" &
done

# Monitor server file descriptor exhaustion:
# lsof -p <hermes_pid> | wc -l
```

After enough connections, Hermes will fail to accept new connections (`Too many open files`) or exhaust available memory, blocking all downstream price update consumers.

### Citations

**File:** apps/hermes/server/src/api/ws.rs (L52-54)
```rust
const PING_INTERVAL_DURATION: Duration = Duration::from_secs(30);
const MAX_CLIENT_MESSAGE_SIZE: usize = 1025 * 1024; // 1 MiB
const MAX_CONNECTION_DURATION: Duration = Duration::from_secs(24 * 60 * 60); // 24 hours
```

**File:** apps/hermes/server/src/api/ws.rs (L56-58)
```rust
/// The maximum number of bytes that can be sent per second per IP address.
/// If the limit is exceeded, the connection is closed.
const BYTES_LIMIT_PER_IP_PER_SECOND: u32 = 256 * 1024; // 256 KiB
```

**File:** apps/hermes/server/src/api/ws.rs (L206-234)
```rust
pub async fn ws_route_handler<S>(
    ws: WebSocketUpgrade,
    AxumState(state): AxumState<ApiState<S>>,
    headers: HeaderMap,
    uri: axum::http::Uri,
) -> impl IntoResponse
where
    S: Aggregates,
    S: Benchmarks,
    S: Cache,
    S: PriceFeedMeta,
    S: Send + Sync + 'static,
{
    let requester_ip = headers
        .get(state.ws.requester_ip_header_name.as_str())
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next()) // Only take the first ip if there are multiple
        .and_then(|value| value.parse().ok());

    // Extract the token from the request
    let api_token = token::extract_token_from_headers_and_uri(&headers, &uri);
    let token_suffix = token::get_token_suffix(api_token.as_deref());

    let mut ws = ws.max_message_size(MAX_CLIENT_MESSAGE_SIZE);
    if state.streaming.disconnect_slow_consumers {
        ws = ws.max_write_buffer_size(state.streaming.ws_max_write_buffer_bytes);
    }

    ws.on_upgrade(move |socket| websocket_handler(socket, state, requester_ip, token_suffix))
```

**File:** apps/hermes/server/src/api/ws.rs (L327-395)
```rust
pub struct Subscriber<S> {
    id: SubscriberId,
    ip_addr: Option<IpAddr>,
    token_suffix: String,
    closed: bool,
    state: Arc<S>,
    ws_state: Arc<WsState>,
    metrics: Arc<super::metrics_middleware::ApiMetrics>,
    disconnect_slow_consumers: bool,
    ws_send_timeout: Duration,
    notify_receiver: Receiver<AggregationEvent>,
    receiver: SplitStream<WebSocket>,
    sender: SplitSink<WebSocket, Message>,
    price_feeds_with_config: HashMap<PriceIdentifier, PriceFeedClientConfig>,
    ping_interval: tokio::time::Interval,
    connection_deadline: Instant,
    exit: watch::Receiver<bool>,
    responded_to_ping: bool,
}

impl<S> Drop for Subscriber<S> {
    fn drop(&mut self) {
        self.metrics
            .stream_active_connections
            .get_or_create(&stream_protocol_label("ws"))
            .dec();
    }
}

impl<S> Subscriber<S>
where
    S: Aggregates,
{
    #[allow(
        clippy::too_many_arguments,
        reason = "constructor requires all fields for Subscriber"
    )]
    pub fn new(
        id: SubscriberId,
        ip_addr: Option<IpAddr>,
        token_suffix: String,
        state: Arc<S>,
        ws_state: Arc<WsState>,
        metrics: Arc<super::metrics_middleware::ApiMetrics>,
        disconnect_slow_consumers: bool,
        ws_send_timeout: Duration,
        notify_receiver: Receiver<AggregationEvent>,
        receiver: SplitStream<WebSocket>,
        sender: SplitSink<WebSocket, Message>,
    ) -> Self {
        Self {
            id,
            ip_addr,
            token_suffix,
            closed: false,
            state,
            ws_state,
            metrics,
            disconnect_slow_consumers,
            ws_send_timeout,
            notify_receiver,
            receiver,
            sender,
            price_feeds_with_config: HashMap::new(),
            ping_interval: tokio::time::interval(PING_INTERVAL_DURATION),
            connection_deadline: Instant::now() + MAX_CONNECTION_DURATION,
            exit: crate::EXIT.subscribe(),
            responded_to_ping: true, // We start with true so we don't close the connection immediately
        }
```

**File:** apps/hermes/server/src/api/ws.rs (L583-620)
```rust
            if let Some(ip_addr) = self.ip_addr {
                if !self
                    .ws_state
                    .bytes_limit_whitelist
                    .iter()
                    .any(|ip_net| ip_net.contains(&ip_addr))
                    && self.ws_state.rate_limiter.check_key_n(
                        &ip_addr,
                        NonZeroU32::new(message.len().try_into()?)
                            .ok_or(anyhow!("Empty message"))?,
                    ) != Ok(Ok(()))
                {
                    tracing::info!(
                        self.id,
                        ip = %ip_addr,
                        "Rate limit exceeded. Closing connection.",
                    );
                    self.ws_state
                        .metrics
                        .interactions
                        .get_or_create(&Labels {
                            interaction: Interaction::RateLimit,
                            status: Status::Error,
                            token_suffix: self.token_suffix.clone(),
                        })
                        .inc();

                    self.ws_send(
                        serde_json::to_string(&ServerResponseMessage::Err {
                            error: "Rate limit exceeded".to_string(),
                        })?
                        .into(),
                    )
                    .await?;
                    self.ws_close().await?;
                    self.closed = true;
                    return Ok(());
                }
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L40-42)
```rust
const MAX_CONNECTION_DURATION: Duration = Duration::from_secs(24 * 60 * 60); // 24 hours
const SLOW_CONSUMER_DISCONNECT_MESSAGE: &str = "Slow consumer: disconnected";
const CONNECTION_TIMEOUT_MESSAGE: &str = "Connection timeout reached (24h)";
```

**File:** apps/hermes/server/src/api/rest/v2/sse.rs (L120-145)
```rust
pub async fn price_stream_sse_handler<S>(
    State(state): State<ApiState<S>>,
    QsQuery(params): QsQuery<StreamPriceUpdatesQueryParams>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, RestError>
where
    S: Aggregates,
    S: Send + Sync + 'static,
{
    let price_id_inputs: Vec<PriceIdentifier> = params.ids.into_iter().map(Into::into).collect();
    let price_ids: Vec<PriceIdentifier> =
        validate_price_ids(&state, &price_id_inputs, params.ignore_invalid_price_ids).await?;

    // Clone the update_tx receiver to listen for new price updates
    let update_rx = Aggregates::subscribe(&*state.state);

    // Convert the broadcast receiver into a Stream
    let stream = BroadcastStream::new(update_rx);

    // Set connection start time
    let start_time = Instant::now();
    let disconnect_slow_consumers = state.streaming.disconnect_slow_consumers;
    let should_end = Arc::new(AtomicBool::new(false));
    let should_end_for_chain = should_end.clone();
    let metrics = state.metrics.clone();

    let mut inner_stream = futures::stream::StreamExt::boxed(
```

**File:** apps/hermes/server/src/config/rpc.rs (L8-50)
```rust
#[derive(Args, Clone, Debug)]
#[command(next_help_heading = "RPC Options")]
#[group(id = "RPC")]
pub struct Options {
    /// Address and port the RPC server will bind to.
    #[arg(long = "rpc-listen-addr")]
    #[arg(default_value = DEFAULT_RPC_LISTEN_ADDR)]
    #[arg(env = "RPC_LISTEN_ADDR")]
    pub listen_addr: SocketAddr,

    /// Whitelisted websocket ip network addresses (separated by comma).
    #[arg(long = "rpc-ws-whitelist")]
    #[arg(value_delimiter = ',')]
    #[arg(env = "RPC_WS_WHITELIST")]
    pub ws_whitelist: Vec<IpNet>,

    /// Header name (case insensitive) to fetch requester IP from.
    #[arg(long = "rpc-requester-ip-header-name")]
    #[arg(default_value = DEFAULT_RPC_REQUESTER_IP_HEADER_NAME)]
    #[arg(env = "RPC_REQUESTER_IP_HEADER_NAME")]
    pub requester_ip_header_name: String,

    /// When true, disconnect WebSocket and SSE clients that cannot keep up with price updates.
    #[arg(long = "rpc-disconnect-slow-consumers")]
    #[arg(default_value = "true")]
    #[arg(action = clap::ArgAction::Set)]
    #[arg(env = "RPC_DISCONNECT_SLOW_CONSUMERS")]
    pub disconnect_slow_consumers: bool,

    /// Maximum WebSocket write buffer size in bytes. Only enforced when
    /// `disconnect_slow_consumers` is enabled.
    #[arg(long = "rpc-ws-max-write-buffer-bytes")]
    #[arg(default_value = DEFAULT_RPC_WS_MAX_WRITE_BUFFER_BYTES)]
    #[arg(env = "RPC_WS_MAX_WRITE_BUFFER_BYTES")]
    pub ws_max_write_buffer_bytes: usize,

    /// Maximum time in seconds to wait for a WebSocket send, feed, or flush
    /// operation before treating the client as a slow consumer.
    #[arg(long = "rpc-ws-send-timeout-secs")]
    #[arg(default_value = DEFAULT_RPC_WS_SEND_TIMEOUT_SECS)]
    #[arg(env = "RPC_WS_SEND_TIMEOUT_SECS")]
    pub ws_send_timeout_secs: u64,
}
```
