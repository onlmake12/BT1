Looking at the `start_tcp_server` function in full:

The code is fully visible. Here is my analysis:

**Root cause (confirmed):** `start_tcp_server` in `rpc/src/server.rs` lines 176–193 loops unconditionally on `listener.accept().await` and calls `tokio::spawn` for every accepted stream. There is no connection counter, semaphore, `Arc<Semaphore>`, or any backpressure mechanism anywhere in the function.

**Deployment context:**
- `tcp_listen_address` is `Option<String>` with `#[serde(default)]` — it is `None` by default and must be explicitly set to activate the TCP RPC server.
- The canonical example binds to `127.0.0.1:18114` (localhost only).
- For a network attacker to reach it, the operator must both enable it AND bind it to a non-loopback address. For a local attacker, only enabling it suffices.

**Scope rule analysis:**
- The TCP RPC is a **supported production feature** (documented in `rpc/README.md`, used in integration tests), not an "unsupported local configuration."
- The flaw is in the production code itself, not in any configuration choice.
- The impact (FD exhaustion + Tokio task heap exhaustion → RPC DoS) is concrete and local-testable, not theoretical.
- No privileged role, leaked key, or majority hashpower is required.

---

### Title
Unbounded TCP Connection Acceptance in `start_tcp_server` Enables Resource Exhaustion DoS — (`rpc/src/server.rs`)

### Summary
`RpcServer::start_tcp_server` accepts an unlimited number of concurrent TCP connections, spawning an unbounded number of Tokio tasks and holding an unbounded number of file descriptors, with no connection cap or semaphore guard.

### Finding Description
In `rpc/src/server.rs`, the `start_tcp_server` function binds a `TcpListener` and enters an infinite `while let Ok((stream, _)) = listener.accept().await` loop. For every accepted connection it immediately calls `tokio::spawn(async move { … })` with no guard: [1](#0-0) 

There is no `Arc<tokio::sync::Semaphore>`, no connection counter, no `accept_many` rate limit, and no idle-connection timeout before the task is spawned. The `StreamServerConfig` pipeline/channel sizes of 4 apply only to per-connection request pipelining, not to the total number of concurrent connections. [2](#0-1) 

### Impact Explanation
Each accepted TCP connection consumes:
- One OS file descriptor (socket)
- One Tokio task allocation on the heap (stack + future state machine)

An attacker who opens N idle connections forces the node to hold N tasks and N FDs. At the OS default soft limit (commonly 1024 FDs per process, up to ~65536 with `ulimit -n`), subsequent `accept()` calls return `EMFILE`/`ENFILE`, causing the accept loop to exit (`while let Ok(…)` drops on `Err`). The Tokio runtime also degrades under tens of thousands of live tasks. The result is that legitimate RPC callers — including snapshot reads, tx-pool queries, and subscription clients — time out or are refused.

### Likelihood Explanation
The TCP RPC is an opt-in feature (`tcp_listen_address` is `None` by default): [3](#0-2) 

However, it is a fully supported production feature documented for subscription use: [4](#0-3) 

Any operator who enables it — even bound to localhost — is vulnerable to a local attacker. If bound to `0.0.0.0`, any network peer can exploit it. No authentication, no PoW, no privileged role is required.

### Recommendation
Introduce a connection semaphore before spawning:

```rust
let semaphore = Arc::new(tokio::sync::Semaphore::new(MAX_TCP_CONNECTIONS)); // e.g. 1000
while let Ok((stream, _)) = listener.accept().await {
    let permit = match semaphore.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => { /* log and drop stream */ continue; }
    };
    tokio::spawn(async move {
        let _permit = permit; // released when task ends
        // … existing handler …
    });
}
```

Additionally, add an idle-read timeout inside the per-connection task so that connections that never send data are reaped promptly.

### Proof of Concept
```python
import socket, time

HOST, PORT = "127.0.0.1", 18114  # tcp_listen_address
sockets = []
for i in range(50_000):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((HOST, PORT))
        sockets.append(s)
    except OSError:
        print(f"OS refused at connection {i}")
        break

# Now attempt a legitimate RPC call — it will time out or be refused
import requests
try:
    r = requests.post(f"http://127.0.0.1:8114/", json={"id":1,"jsonrpc":"2.0","method":"get_tip_block_number","params":[]}, timeout=5)
    print(r.text)
except requests.exceptions.Timeout:
    print("CONFIRMED: RPC timed out — node starved of FDs/tasks")
```

### Citations

**File:** rpc/src/server.rs (L166-171)
```rust
            let stream_config = StreamServerConfig::default()
                .with_channel_size(4)
                .with_pipeline_size(4)
                .with_shutdown(async move {
                    new_tokio_exit_rx().cancelled().await;
                });
```

**File:** rpc/src/server.rs (L176-193)
```rust
                        while let Ok((stream, _)) = listener.accept().await {
                            let rpc = Arc::clone(&rpc);
                            let stream_config = stream_config.clone();
                            let codec = codec.clone();
                            tokio::spawn(async move {
                                let (r, w) = stream.into_split();
                                let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
                                let w = FramedWrite::new(w, codec).with(|msg| async move {
                                    Ok::<_, LinesCodecError>(match msg {
                                        StreamMsg::Str(msg) => msg,
                                        _ => "".into(),
                                    })
                                });
                                tokio::pin!(w);
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
                                }
                            });
```

**File:** util/app-config/src/configs/rpc.rs (L32-33)
```rust
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
```

**File:** rpc/README.md (L11-11)
```markdown
Subscriptions require a full duplex connection. CKB offers such connections in the form of TCP (enable with `rpc.tcp_listen_address` configuration option) and WebSockets (enable with `rpc.ws_listen_address`).
```
