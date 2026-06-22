Based on my investigation of the CKB codebase, I found a valid analog to the missing access control vulnerability described in the external report.

---

### Title
Unauthenticated Miner HTTP Notification Endpoint Allows Block Template Injection to Redirect Mining Rewards — (`miner/src/client.rs`)

### Summary

The CKB miner client runs an HTTP server to receive block template push notifications from the CKB node. The `handle` function that processes these incoming HTTP requests performs no authentication or authorization check whatsoever. Any local or network-reachable process can POST a crafted `BlockTemplate` payload to this endpoint, causing the miner to immediately begin working on an attacker-controlled block template — including one with a different cellbase lock script (reward address) — redirecting all future mining rewards to the attacker.

### Finding Description

The miner client in `miner/src/client.rs` exposes an HTTP server to receive block template push notifications. The `handle` function that processes all incoming HTTP requests is:

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
}
``` [1](#0-0) 

There is no check on the source of the request, no token, no IP allowlist, and no signature verification. The function unconditionally deserializes the body as a `BlockTemplate` and calls `client.update_block_template(template)`, replacing whatever the miner was previously working on.

Separately, the file does contain a `parse_authorization` function:

```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> { ... }
``` [2](#0-1) 

However, this function is used exclusively for **outgoing** requests from the miner client to the CKB node's RPC — it is never applied to **incoming** requests arriving at the `handle` endpoint. The incoming handler has zero access control.

In CKB's reward model, the cellbase output lock script (the address that receives mining rewards) is determined by the block template's cellbase transaction, not enforced by consensus to a specific address. The network enforces the reward **amount** but not the reward **recipient**. An attacker who injects a crafted `BlockTemplate` with a different cellbase lock script causes the miner to produce valid blocks that pay all rewards (primary issuance + secondary issuance + transaction fees) to the attacker's address. [3](#0-2) 

The `RewardCalculator` computes the total reward amount that must appear in the cellbase output capacity, but the lock script (recipient) is taken directly from the cellbase witness of the block being finalized — which in the injected template is attacker-controlled. [4](#0-3) 

### Impact Explanation

An attacker who can reach the miner's HTTP notification port can:

1. POST a crafted `BlockTemplate` JSON payload with a cellbase transaction whose output lock script is the attacker's own address.
2. The miner immediately calls `update_block_template` and all worker threads begin hashing the attacker's template.
3. Every block the miner successfully mines pays its full reward (primary + secondary issuance + all transaction fees) to the attacker's address instead of the legitimate operator's address.
4. The legitimate miner operator receives zero rewards for all work performed after the injection.

This is a direct theft of mining rewards — a high-severity financial impact equivalent to the `distributeAssets()` drain in the reference report.

### Likelihood Explanation

- The miner's HTTP notification server is a standard feature used in production mining setups.
- If the server binds to `0.0.0.0` (or any non-loopback interface), any network peer can exploit this with a single HTTP POST.
- Even if bound to `127.0.0.1`, any unprivileged local process on the same machine (e.g., a malicious dependency, a co-located service) can exploit it.
- The attack requires no special knowledge beyond the port number and the `BlockTemplate` JSON schema, which is publicly documented in the CKB RPC README.
- The attack is silent — the miner continues operating normally and produces valid blocks; only the reward recipient changes.

### Recommendation

Add authentication to the incoming `handle` function. The simplest fix consistent with the existing `parse_authorization` pattern is to require a shared secret (Bearer token or Basic auth) in the `Authorization` header of incoming push notifications, validated against a configured secret before calling `update_block_template`. Alternatively, bind the notification server exclusively to `127.0.0.1` and enforce that only the local CKB node process can connect (e.g., via a Unix domain socket instead of TCP).

### Proof of Concept

**Attacker preconditions:** Network or local access to the miner's HTTP notification port. No credentials required.

**Steps:**

1. Identify the miner's HTTP notification port (configured in the miner's config file, default typically `18114` or similar).
2. Craft a valid `BlockTemplate` JSON payload (schema is publicly documented) with the cellbase output lock script set to the attacker's CKB address.
3. POST the payload:
   ```bash
   curl -X POST http://<miner-host>:<notify-port>/ \
     -H "Content-Type: application/json" \
     -d '{"version":"0x0","compact_target":"0x...","current_time":"0x...","number":"0x...","epoch":"0x...","parent_hash":"0x...","cycles_limit":"0x...","bytes_limit":"0x...","uncles_count_limit":"0x...","uncles":[],"transactions":[],"proposals":[],"cellbase":{"cycles":null,"data":{"cell_deps":[],"header_deps":[],"inputs":[{"previous_output":{"index":"0xffffffff","tx_hash":"0x0000..."},"since":"0x..."}],"outputs":[{"capacity":"0x...","lock":{"args":"0x<ATTACKER_ARGS>","code_hash":"0x<ATTACKER_CODE_HASH>","hash_type":"type"},"type":null}],"outputs_data":["0x"],"version":"0x0","witnesses":["0x..."]}},"dao":"0x..."}'
   ```
4. The miner's `handle` function deserializes the payload and calls `client.update_block_template(template)` with no authentication check.
5. All miner worker threads immediately switch to hashing the attacker's block template.
6. The next block the miner finds pays all rewards to the attacker's lock script. [1](#0-0)

### Citations

**File:** miner/src/client.rs (L358-369)
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
}
```

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
    }
}
```

**File:** util/types/src/core/reward.rs (L13-46)
```rust
pub struct BlockReward {
    /// The total block reward.
    pub total: Capacity,
    /// The primary block reward.
    pub primary: Capacity,
    /// The secondary block reward.
    ///
    /// # Notice
    ///
    /// - A part of the secondary issuance goes to the miners, the ratio depends on how many CKB
    ///   are used to store state.
    /// - And a part of the secondary issuance goes to the NervosDAO, the ratio depends on how many
    ///   CKB are deposited and locked in the NervosDAO.
    /// - The rest of the secondary issuance is determined by the community through the governance
    ///   mechanism.
    ///   Before the community can reach agreement, this part of the secondary issuance is going to
    ///   be burned.
    pub secondary: Capacity,
    /// The transaction fees that are rewarded to miners because the transaction is committed in
    /// the block.
    ///
    /// # Notice
    ///
    /// Miners only get 60% of the transaction fee for each transaction committed in the block.
    pub tx_fee: Capacity,
    /// The transaction fees that are rewarded to miners because the transaction is proposed in the
    /// block or its uncles.
    ///
    /// # Notice
    ///
    /// Miners only get 40% of the transaction fee for each transaction proposed in the block
    /// and committed later in its active commit window.
    pub proposal_reward: Capacity,
}
```

**File:** util/reward-calculator/src/lib.rs (L85-133)
```rust
    fn block_reward_internal(
        &self,
        target: &HeaderView,
        parent: &HeaderView,
    ) -> Result<(Script, BlockReward), DaoError> {
        let target_lock = CellbaseWitness::from_slice(
            &self
                .store
                .get_cellbase(&target.hash())
                .expect("target cellbase exist")
                .witnesses()
                .get(0)
                .expect("target witness exist")
                .raw_data(),
        )
        .expect("cellbase loaded from store should has non-empty witness")
        .lock();

        let txs_fees = self.txs_fees(target)?;
        let proposal_reward = self.proposal_reward(parent, target)?;
        let (primary, secondary) = self.base_block_reward(target)?;

        let total = txs_fees
            .safe_add(proposal_reward)?
            .safe_add(primary)?
            .safe_add(secondary)?;

        debug!(
            "[RewardCalculator] target {} {}\n
             txs_fees {:?}, proposal_reward {:?}, primary {:?}, secondary: {:?}, total_reward {:?}",
            target.number(),
            target.hash(),
            txs_fees,
            proposal_reward,
            primary,
            secondary,
            total,
        );

        let block_reward = BlockReward {
            total,
            primary,
            secondary,
            tx_fee: txs_fees,
            proposal_reward,
        };

        Ok((target_lock, block_reward))
    }
```
