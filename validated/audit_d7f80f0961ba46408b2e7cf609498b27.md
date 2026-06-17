### Title
Fortuna Provider Reveals Entropy Before Request Finalization Due to `reveal_delay_blocks: 0` and `BlockStatus::Latest` Default, Enabling Chain Reorgs to Alter Random Number Outcomes — (`apps/fortuna/src/chain/reader.rs`, `apps/fortuna/config.sample.yaml`)

---

### Summary

The Fortuna entropy provider defaults `confirmed_block_status` to `BlockStatus::Latest` and the canonical sample configuration sets `reveal_delay_blocks: 0`. Together, these cause the provider to reveal its committed random value immediately after observing a request on the latest (unfinalized) block. On chains with frequent reorganizations (e.g., Polygon), a reorg after the provider's reveal causes the request's stored `blockNumber` to change. For requests made via the legacy `request()` path with `useBlockhash = true`, the final random number incorporates `blockhash(req.blockNumber)`, so the reorg directly changes the delivered random number. For all request types, the provider's already-submitted reveal transaction reverts, leaving the user's request stuck until a retry.

---

### Finding Description

**Root cause 1 — Code-level default:**

`BlockStatus` is defined with `#[default]` on the `Latest` variant:

```rust
// apps/fortuna/src/chain/reader.rs
#[derive(Copy, Clone, Debug, Default, ...)]
pub enum BlockStatus {
    #[default]
    Latest,
    Finalized,
    Safe,
}
```

`EthereumConfig.confirmed_block_status` uses `#[serde(default)]`, so any deployment that omits this field silently inherits `BlockStatus::Latest`. [1](#0-0) [2](#0-1) 

**Root cause 2 — Sample config sets `reveal_delay_blocks: 0`:**

The only provided configuration template explicitly sets:

```yaml
reveal_delay_blocks: 0
``` [3](#0-2) 

**Root cause 3 — No validation in `Config::load()`:**

`Config::load()` validates profit percentages and replica settings but performs no check that `reveal_delay_blocks` is non-zero or that `confirmed_block_status` is not `Latest` for reorg-prone chains. [4](#0-3) 

**How the reveal delay is enforced (and bypassed):**

In `revelation.rs` and `keeper/block.rs`, the provider only reveals when:

```
current_block_number - reveal_delay_blocks >= request.block_number
```

With `reveal_delay_blocks = 0` and `confirmed_block_status = Latest`, this condition is satisfied the moment the request appears on the latest block — before any finality. [5](#0-4) [6](#0-5) 

**How the random number is affected by a reorg:**

For the legacy `request()` path with `useBlockhash = true`, `revealHelper` reads:

```solidity
bytes32 _blockHash = blockhash(req.blockNumber);
...
randomNumber = combineRandomValues(userContribution, providerContribution, blockHash);
```

`req.blockNumber` is set to `block.number` at request time. A reorg that moves the request transaction to a different block changes `req.blockNumber`, which changes `blockhash(req.blockNumber)`, which changes the final `randomNumber`. [7](#0-6) [8](#0-7) 

---

### Impact Explanation

**High.** For any application using the legacy `request()` API with `useBlockhash = true` on a reorg-prone chain:

- The provider reveals based on the pre-reorg block number.
- After the reorg, the request's `blockNumber` changes.
- The provider's already-submitted reveal transaction reverts (request no longer exists at that sequence number in the new chain).
- When the provider retries, the random number is computed with a different `blockhash`, producing a different outcome.
- A user who was a winner under the original random number may lose, and vice versa — directly analogous to the original report.

For `requestV2()` (callback path, `useBlockhash = false`), the random number itself does not change, but the provider's reveal transaction reverts, leaving the user's request stuck until the keeper's retry logic (`block_delays`) eventually re-processes it. This is a liveness/DoS impact.

---

### Likelihood Explanation

**High.** Polygon (a primary deployment target for Entropy, as listed in the chainlist docs) experiences multiple reorgs per day with depth exceeding 3 blocks, and has historically seen reorgs of 100+ blocks. With `reveal_delay_blocks: 0` and `confirmed_block_status: Latest`, the provider reveals after 0 confirmations, making every request on Polygon vulnerable to reorg-induced outcome changes. [9](#0-8) 

---

### Recommendation

1. **Change the `BlockStatus` default** from `Latest` to `Safe` or `Finalized` in `apps/fortuna/src/chain/reader.rs`. This ensures that any deployment omitting the field gets a reorg-resistant baseline.

2. **Add validation in `Config::load()`** (`apps/fortuna/src/config.rs`) that warns or errors when `confirmed_block_status == Latest && reveal_delay_blocks < SAFE_MINIMUM` for known reorg-prone chains.

3. **Update `config.sample.yaml`** to set a chain-appropriate `reveal_delay_blocks` (e.g., ≥ 30 for Polygon) and `confirmed_block_status: safe` or `confirmed_block_status: finalized`.

4. **Document the security implication** of `reveal_delay_blocks: 0` prominently in the sample config and operator guide. [10](#0-9) [11](#0-10) 

---

### Proof of Concept

**Setup:** Deploy Entropy on Polygon. Configure Fortuna with `reveal_delay_blocks: 0` and `confirmed_block_status` omitted (defaults to `Latest`).

**Steps:**

1. User calls `request(provider, userCommitment, true)` (legacy path, `useBlockhash = true`). Request lands in block N. `req.blockNumber = N`.

2. Fortuna keeper observes the event on the latest block. Since `current_block - 0 >= N`, the reveal condition is immediately satisfied. Fortuna submits `reveal(provider, seqNum, userContribution, providerContribution)`.

3. A chain reorg of depth > 0 occurs. The request transaction is reorganized into block N'. `req.blockNumber` is now N' (different from N).

4. Fortuna's reveal transaction from step 2 reverts (the request at the old sequence number no longer exists, or `req.blockNumber` has changed).

5. Fortuna retries. The new random number is `hash(userContribution, providerContribution, blockhash(N'))` ≠ `hash(userContribution, providerContribution, blockhash(N))`.

6. A user who won under `blockhash(N)` may lose under `blockhash(N')`. [12](#0-11) [13](#0-12)

### Citations

**File:** apps/fortuna/src/chain/reader.rs (L12-23)
```rust
#[derive(
    Copy, Clone, Debug, Default, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize,
)]
pub enum BlockStatus {
    /// Latest block
    #[default]
    Latest,
    /// Finalized block accepted as canonical
    Finalized,
    /// Safe head block
    Safe,
}
```

**File:** apps/fortuna/src/config.rs (L81-115)
```rust
impl Config {
    pub fn load(path: &str) -> Result<Config> {
        // Open and read the YAML file
        // TODO: the default serde deserialization doesn't enforce unique keys
        let yaml_content = fs::read_to_string(path)?;
        let config: Config = serde_yaml::from_str(&yaml_content)?;

        // Run correctness checks for the config and fail if there are any issues.
        for (chain_id, config) in config.chains.iter() {
            if !(config.min_profit_pct <= config.target_profit_pct
                && config.target_profit_pct <= config.max_profit_pct)
            {
                return Err(anyhow!("chain id {:?} configuration is invalid. Config must satisfy min_profit_pct <= target_profit_pct <= max_profit_pct.", chain_id));
            }
        }

        if let Some(replica_config) = &config.keeper.replica_config {
            if replica_config.total_replicas == 0 {
                return Err(anyhow!("Keeper replica configuration is invalid. total_replicas must be greater than 0."));
            }
            if config.keeper.private_key.load()?.is_none() {
                return Err(anyhow!(
                    "Keeper replica configuration requires a keeper private key to be specified."
                ));
            }
            if replica_config.replica_id >= replica_config.total_replicas {
                return Err(anyhow!("Keeper replica configuration is invalid. replica_id must be less than total_replicas."));
            }
            if replica_config.backup_delay_seconds == 0 {
                return Err(anyhow!("Keeper replica configuration is invalid. backup_delay_seconds must be greater than 0 to prevent race conditions."));
            }
        }

        Ok(config)
    }
```

**File:** apps/fortuna/src/config.rs (L134-143)
```rust
    /// reveal_delay_blocks - The difference between the block number with the
    /// confirmed_block_status(see below) and the block number of a request to
    /// Entropy should be greater than `reveal_delay_blocks` for Fortuna to reveal
    /// its commitment.
    pub reveal_delay_blocks: BlockNumber,

    /// The BlockStatus of the block that is considered confirmed.
    /// For example, Finalized, Safe, Latest
    #[serde(default)]
    pub confirmed_block_status: BlockStatus,
```

**File:** apps/fortuna/config.sample.yaml (L1-10)
```yaml
chains:
  lightlink_pegasus:
    geth_rpc_addr: https://replicator.pegasus.lightlink.io/rpc/v1
    contract_addr: 0x8250f4aF4B972684F7b336503E2D6dFeDeB1487a

    # Keeper configuration for the chain
    reveal_delay_blocks: 0
    gas_limit: 500000

    # Multiplier for the priority fee estimate, as a percentage (i.e., 100 = no change).
```

**File:** apps/fortuna/src/api/revelation.rs (L64-112)
```rust
    let current_block_number_fut = state
        .contract
        .get_block_number(state.confirmed_block_status);

    match block_number {
        Some(block_number) => {
            let maybe_request_fut = state.contract.get_request_with_callback_events(
                block_number,
                block_number,
                state.provider_address,
            );

            let (maybe_request, current_block_number) =
                try_join!(maybe_request_fut, current_block_number_fut).map_err(|e| {
                    tracing::error!(chain_id = chain_id, "RPC request failed {}", e);
                    RestError::TemporarilyUnavailable
                })?;

            if current_block_number.saturating_sub(state.reveal_delay_blocks) < block_number {
                return Err(RestError::PendingConfirmation);
            }

            maybe_request
                .iter()
                .find(|r| r.sequence_number == sequence)
                .ok_or(RestError::NoPendingRequest)?;
        }
        None => {
            let maybe_request_fut = state
                .contract
                .get_request_v2(state.provider_address, sequence);
            let (maybe_request, current_block_number) =
                try_join!(maybe_request_fut, current_block_number_fut).map_err(|e| {
                    tracing::error!(chain_id = chain_id, "RPC request failed {}", e);
                    RestError::TemporarilyUnavailable
                })?;

            match maybe_request {
                Some(r)
                    if current_block_number.saturating_sub(state.reveal_delay_blocks)
                        >= r.block_number =>
                {
                    Ok(())
                }
                Some(_) => Err(RestError::PendingConfirmation),
                None => Err(RestError::NoPendingRequest),
            }?;
        }
    }
```

**File:** apps/fortuna/src/keeper/block.rs (L51-63)
```rust
pub async fn get_latest_safe_block(chain_state: &BlockchainState) -> BlockNumber {
    loop {
        match chain_state
            .contract
            .get_block_number(chain_state.confirmed_block_status)
            .await
        {
            Ok(latest_confirmed_block) => {
                tracing::info!(
                    "Fetched latest safe block {}",
                    latest_confirmed_block - chain_state.reveal_delay_blocks
                );
                return latest_confirmed_block - chain_state.reveal_delay_blocks;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L260-263)
```text
        req.requester = msg.sender;

        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-430)
```text
    function revealHelper(
        EntropyStructsV2.Request storage req,
        bytes32 userContribution,
        bytes32 providerContribution
    ) internal returns (bytes32 randomNumber, bytes32 blockHash) {
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();

        blockHash = bytes32(uint256(0));
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();

            blockHash = _blockHash;
        }

        randomNumber = combineRandomValues(
            userContribution,
            providerContribution,
            blockHash
        );
```

**File:** apps/developer-hub/content/docs/entropy/chainlist.mdx (L27-28)
```text
The default provider on mainnet has a reveal delay to avoid changes on the outcome of the Entropy request because of block reorgs.
The reveal delay shows how many blocks should be produced after the block including the request transaction in order to reveal and submit a callback transaction.
```
