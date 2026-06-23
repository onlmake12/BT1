The code is clear. Let me trace the exact logic.

**The bug in `update_block_template`** (`miner/src/client.rs`, line 295–301):

```rust
let updated = |id| {
    if id != work_id || id == 0 {
        Some(work_id)
    } else {
        None
    }
};
```

- `id` = current stored value in `current_work_id` (AtomicU64, initialized to 0)
- `work_id` = incoming value from the template

When attacker sends `work_id = 0`, the condition becomes:
`id != 0 || id == 0` — a **tautology**, always `true` for any `id`.

So `fetch_update` always returns `Ok(...)`, and every POST unconditionally sends to `new_work_tx`.

The channel is created with `ckb_channel::unbounded()` at `ckb-bin/src/subcommand/miner.rs` line 12. [1](#0-0) 

The consumer (`Miner::run`) processes items in a `select!` loop, but if the HTTP notify endpoint is flooded faster than the consumer drains, the unbounded channel accumulates `Works::New(work)` items, each containing a full packed `Block`. [2](#0-1) 

---

### Title
Deduplication Tautology via `work_id = 0` Floods Unbounded Channel — (`miner/src/client.rs`)

### Summary
The `update_block_template` deduplication closure contains a logic error: the `id == 0` guard creates a tautology when the incoming `work_id` is also `0`, bypassing all deduplication and unconditionally enqueuing work into an unbounded crossbeam channel.

### Finding Description
In `update_block_template`, the `fetch_update` closure is:

```rust
let updated = |id| {
    if id != work_id || id == 0 {
        Some(work_id)
    } else {
        None
    }
};
``` [3](#0-2) 

The `id == 0` branch was intended to handle the initial state (the `AtomicU64` starts at 0, so the first template always passes). However, when an attacker sends a `BlockTemplate` with `work_id = 0`, the condition `id != work_id || id == 0` simplifies to `id != 0 || id == 0`, which is always `true`. Every subsequent POST with `work_id = 0` passes the guard and enqueues a `Works::New(work)` item.

The channel is `ckb_channel::unbounded()`: [1](#0-0) 

The HTTP notify handler has no authentication and accepts any TCP connection: [4](#0-3) 

### Impact Explanation
Each enqueued `Works::New(work)` holds a full packed `Block` in memory. With an unbounded channel and a fast attacker, memory grows without limit until the miner process is OOM-killed. The CKB node itself is unaffected; only the miner process crashes.

### Likelihood Explanation
Requires notify mode to be enabled (`config.listen = Some(addr)`) and the listen address to be reachable by the attacker. If bound to `0.0.0.0`, any network peer qualifies. If bound to `127.0.0.1`, a local attacker suffices. No authentication, no rate limiting, no `work_id` validation exists on the endpoint.

### Recommendation
Fix the closure logic to avoid the tautology. The `id == 0` special case should only apply when `work_id != 0`:

```rust
let updated = |id| {
    if id != work_id {
        Some(work_id)
    } else {
        None
    }
};
```

This preserves the "always accept first real work" behavior (since `current_work_id` starts at 0 and any non-zero `work_id` satisfies `id != work_id`), while eliminating the bypass. Additionally, consider bounding the channel or adding a rate limit on the notify endpoint.

### Proof of Concept
Send 10^6 POST requests to the notify listen address with a valid `BlockTemplate` JSON body where `work_id` is `"0x0"`. Assert that the miner process RSS grows proportionally and eventually crashes or that channel depth is unbounded.

```bash
for i in $(seq 1 1000000); do
  curl -s -X POST http://<listen_addr> \
    -H 'Content-Type: application/json' \
    -d '{"work_id":"0x0","current_time":"0x...","...":"..."}' &
done
```

Each request bypasses deduplication at `miner/src/client.rs` line 296 and enqueues a `Works::New` item into the unbounded channel at line 308. [5](#0-4)

### Citations

**File:** ckb-bin/src/subcommand/miner.rs (L12-12)
```rust
    let (new_work_tx, new_work_rx) = unbounded();
```

**File:** miner/src/miner.rs (L89-125)
```rust
    pub fn run(&mut self, stop_rx: Receiver<()>) {
        loop {
            select! {
                recv(self.work_rx) -> msg => match msg {
                    Ok(work) => {
                        match work {
                            Works::FailSubmit(hash) => {
                                self.legacy_work.pop(&hash);
                            },
                            Works::New(work) => self.notify_new_work(work),
                        }
                    },
                    _ => {
                        error!("work_rx closed");
                        break;
                    },
                },
                recv(self.nonce_rx) -> msg => match msg {
                    Ok((pow_hash, work, nonce)) => {
                        self.submit_nonce(pow_hash, work, nonce);
                        if self.limit != 0 && self.nonces_found >= self.limit {
                            debug!("miner nonce limit reached, terminate ...");
                            broadcast_exit_signals();
                        }
                    },
                    _ => {
                        error!("nonce_rx closed");
                        break;
                    },
                },
                recv(stop_rx) -> _msg => {
                    info!("miner received exit signal, stopped");
                    break;
                }
            };
        }
    }
```

**File:** miner/src/client.rs (L293-312)
```rust
    fn update_block_template(&self, block_template: BlockTemplate) {
        let work_id = block_template.work_id.into();
        let updated = |id| {
            if id != work_id || id == 0 {
                Some(work_id)
            } else {
                None
            }
        };
        if self
            .current_work_id
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
            .is_ok()
        {
            let work: Work = block_template.into();
            if let Err(e) = self.new_work_tx.send(Works::New(work)) {
                error!("notify_new_block error: {:?}", e);
            }
        }
    }
```

**File:** miner/src/client.rs (L358-368)
```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
```
