Looking at the actual code in `miner/src/client.rs` to verify the claim precisely.

The code confirms the vulnerability. Every detail in the question maps directly to the implementation with zero mitigations present.

**Confirmed facts from the code:**

1. `listener.accept()` is called in an unbounded loop with no connection counter. [1](#0-0) 

2. Every accepted connection unconditionally spawns a new `tokio::spawn` task. [2](#0-1) 

3. `graceful.watch()` only tracks connections for graceful shutdown — it does not limit them. [3](#0-2) 

4. No idle/read timeout is configured on the hyper connection. An attacker holding a TCP connection open without sending HTTP data keeps the task alive indefinitely. [4](#0-3) 

5. The grep search for `connection_limit`, `max_connections`, `Semaphore`, `rate_limit`, and `timeout.*conn` in the entire `miner/` directory returned **zero matches** — no guard exists anywhere in the miner codebase.

---

### Title
Unbounded TCP connection acceptance in `listen_block_template_notify` enables resource exhaustion DoS — (`miner/src/client.rs`)

### Summary
`Client::listen_block_template_notify` accepts every inbound TCP connection and spawns an unconstrained `tokio::spawn` task per connection with no idle timeout, no connection count cap, and no per-source rate limit. An attacker who can reach the listen port can exhaust file descriptors and task memory by holding open a large number of idle connections.

### Finding Description
When the miner is configured with `listen = <addr>`, `spawn_background` calls `listen_block_template_notify`. Inside the accept loop, each successful `listener.accept()` immediately calls `tokio::spawn` with a hyper `serve_connection_with_upgrades` future. [5](#0-4) 

The spawned future blocks waiting for the remote side to send an HTTP request. An attacker that opens N TCP connections and sends no data causes N tasks to accumulate. There is no:
- connection counter or semaphore
- idle/read timeout on the hyper connection
- per-IP or global connection cap
- backpressure mechanism of any kind

`graceful.watch()` is a shutdown-coordination wrapper, not a limiter. [6](#0-5) 

### Impact Explanation
Each idle connection consumes one file descriptor and one live tokio task (stack + hyper connection state). At OS FD limits (typically 1024 soft / 65535 hard), the miner can no longer call `accept()` for legitimate CKB-node notifications. The miner then mines on a stale block template, wasting hashpower and potentially producing invalid or already-solved blocks. The miner cannot recover until the attacker releases connections.

### Likelihood Explanation
The `listen` feature is a documented, first-class configuration option. Any operator following the printed instructions in `spawn_background` will enable it. [7](#0-6)  The listen address is operator-chosen; if bound to anything other than `127.0.0.1`, any network-adjacent attacker can exploit this. Even on localhost, a compromised co-tenant process can exploit it.

### Recommendation
- Enforce a maximum concurrent connection count using a `tokio::sync::Semaphore` acquired before `tokio::spawn`; reject (close) connections when the limit is reached.
- Add an idle/read timeout to each hyper connection (e.g., wrap the stream with `tokio::time::timeout` or use hyper's `http1_header_read_timeout`).
- Optionally restrict accepted source IPs to the configured CKB node address.

### Proof of Concept
```rust
// Open 10_000 idle TCP connections to the miner listen addr
for _ in 0..10_000 {
    let _ = std::net::TcpStream::connect(miner_listen_addr);
    // hold open, send nothing
}
// Now attempt a legitimate POST — accept() fails with EMFILE/ENFILE
// or the miner task queue is saturated and the notification is never processed
```
State assertion: after opening the idle connections, a valid `POST /` with a `BlockTemplate` JSON body receives no response and `update_block_template` is never called, confirming the miner is stalled.

### Citations

**File:** miner/src/client.rs (L206-221)
```rust
        if let Some(addr) = self.config.listen {
            ckb_logger::info!("listen notify mode : {}", addr);
            ckb_logger::info!(
                r#"
Please note that ckb-miner runs in notify mode. \
You should configure the corresponding information in CKB block assembler, \
for example:

[block_assembler]
...
notify = ["http://{}"]

Otherwise ckb-miner will malfunction and stop submitting valid blocks after a certain period.
"#,
                addr
            );
```

**File:** miner/src/client.rs (L240-261)
```rust
        loop {
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
            tokio::select! {
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
