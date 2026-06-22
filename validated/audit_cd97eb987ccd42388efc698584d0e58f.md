### Title
Unbounded `tokio::spawn` per TCP connection in notify HTTP server causes miner resource exhaustion — (`miner/src/client.rs`)

### Summary
`listen_block_template_notify` spawns one `tokio::spawn` task per accepted TCP connection with no concurrency cap, connection semaphore, or rate limiter. An attacker who can reach the notify port can exhaust the miner process's memory and tokio scheduler by opening connections faster than they are reaped.

### Finding Description

In `listen_block_template_notify` the accept loop is:

```rust
conn = listener.accept() => {
    // ...
    let conn = graceful.watch(conn.into_owned());
    tokio::spawn(async move {          // ← one task per connection, unbounded
        if let Err(err) = conn.await {
            info!("connection error: {}", err);
        }
    });
},
``` [1](#0-0) 

Every accepted TCP connection immediately spawns a new `tokio::spawn` task. `GracefulShutdown::watch()` only registers the connection for clean-shutdown tracking; it imposes no limit on how many connections can be active simultaneously. [2](#0-1) 

There is no semaphore, no `Arc<AtomicUsize>` connection counter, no `tokio::sync::Semaphore`, and no OS-level `SO_REUSEPORT` backlog cap applied in the application layer. The `TcpListener` OS backlog is typically 128 slots, but once connections are accepted into the loop they are immediately promoted to live tasks with no upper bound. [3](#0-2) 

The `listen` field is `Option<SocketAddr>` and is explicitly documented as a supported production feature for notify mode. [4](#0-3) 

The default template ships with it commented out pointing to `127.0.0.1:8888`, but any operator enabling notify mode activates this path. [5](#0-4) 

### Impact Explanation

Each spawned task holds a `hyper` connection object, a `GracefulShutdown` watcher slot, and tokio task metadata. At 100,000 simultaneous connections the miner process will accumulate hundreds of MB of heap and the tokio work-stealing scheduler will degrade under task-queue pressure, causing the miner to stop submitting valid blocks or OOM-crash. The CKB node itself is unaffected, but the miner ceases to function.

### Likelihood Explanation

- **Localhost binding (default example):** any local process on the same machine can open 100k loopback connections trivially.
- **Non-localhost binding:** any network-reachable attacker can do the same.
- No authentication, no TLS, no IP allowlist is enforced by the code.

### Recommendation

Wrap the accept loop with a bounded semaphore:

```rust
let sem = Arc::new(tokio::sync::Semaphore::new(MAX_CONNECTIONS));
// ...
let permit = sem.clone().acquire_owned().await.unwrap();
tokio::spawn(async move {
    let _permit = permit;   // dropped when connection closes
    if let Err(err) = conn.await { ... }
});
```

Alternatively, use `tower`'s `ConcurrencyLimit` layer or reject connections at the `accept()` call site when a counter exceeds a configured threshold.

### Proof of Concept

```bash
# miner configured with listen = "127.0.0.1:8888"
python3 -c "
import socket, time
socks = []
for i in range(100000):
    s = socket.socket()
    s.setblocking(False)
    try: s.connect_ex(('127.0.0.1', 8888))
    except: pass
    socks.append(s)
print('opened', len(socks), 'connections')
time.sleep(60)
"
# observe miner RSS grow unboundedly; miner stops submitting blocks
``` [6](#0-5)

### Citations

**File:** miner/src/client.rs (L234-240)
```rust
    async fn listen_block_template_notify(&self, addr: SocketAddr) {
        let listener = TcpListener::bind(addr).await.unwrap();
        let server = auto::Builder::new(TokioExecutor::new());
        let graceful = GracefulShutdown::new();
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        loop {
```

**File:** miner/src/client.rs (L244-261)
```rust
                conn = listener.accept() => {
                    let (stream, _) = match conn {
                        Ok(conn) => conn,
                        Err(e) => {
                            info!("accept error: {}", e);
                            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                            continue;
                        }
                    };
                    let stream = hyper_util::rt::TokioIo::new(Box::pin(stream));
                    let conn = server.serve_connection_with_upgrades(stream, handle);

                    let conn = graceful.watch(conn.into_owned());
                    tokio::spawn(async move {
                        if let Err(err) = conn.await {
                            info!("connection error: {}", err);
                        }
                    });
```

**File:** util/app-config/src/configs/miner.rs (L28-30)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```

**File:** resource/ckb-miner.toml (L59-61)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"

```
