### Title
`reveal_delay_blocks: 0` in Sample Config Enables Validator Chain-Reorg Manipulation of `useBlockhash=true` Entropy Requests — (`apps/fortuna/config.sample.yaml`, `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The Fortuna provider service's reference configuration sets `reveal_delay_blocks: 0`, meaning the provider reveals its random contribution immediately after a request is confirmed at the `Latest` block status. On reorg-prone chains (Polygon, BSC, etc.), a validator can reorg the request block to change `blockhash(req.blockNumber)`, which is incorporated into the final random number when `useBlockhash = true`. Because `reveal_delay_blocks: 0` causes Fortuna to expose the provider's contribution via its public API before the request block is finalized, the validator can compute the resulting random number for any candidate reorg and select the most favorable outcome.

---

### Finding Description

The Fortuna service enforces a configurable delay before revealing the provider's hash-chain value. This delay is set in `apps/fortuna/config.sample.yaml`:

```yaml
reveal_delay_blocks: 0
``` [1](#0-0) 

This value is read into `EthereumConfig.reveal_delay_blocks` and propagated into `BlockchainState`: [2](#0-1) [3](#0-2) 

The keeper uses this to compute the "safe" block ceiling up to which it processes requests:

```rust
return latest_confirmed_block - chain_state.reveal_delay_blocks;
``` [4](#0-3) 

The revelation API enforces the same delay before serving the provider's value to callers: [5](#0-4) [6](#0-5) 

When `reveal_delay_blocks = 0` and `confirmed_block_status = Latest`, the provider's contribution is immediately available from the public Fortuna API as soon as the request transaction appears in the latest block — before that block is finalized.

On the contract side, the legacy `request()` function accepts `useBlockHash = true`, which causes `revealHelper` to incorporate `blockhash(req.blockNumber)` into the final random number:

```solidity
if (req.useBlockhash) {
    bytes32 _blockHash = blockhash(req.blockNumber);
    ...
    blockHash = _blockHash;
}
randomNumber = combineRandomValues(userContribution, providerContribution, blockHash);
``` [7](#0-6) 

The request block number is stored at request time:

```solidity
req.blockNumber = SafeCast.toUint64(block.number);
req.useBlockhash = useBlockhash;
``` [8](#0-7) 

---

### Impact Explanation

With `reveal_delay_blocks: 0` and `confirmed_block_status: Latest`, a validator on a reorg-prone chain can execute the following attack against any user who calls `request(..., useBlockHash=true)`:

1. User submits `request(provider, commitment, true)` — included in block N.
2. Fortuna immediately exposes the provider's contribution `x_i` via its public API (delay = 0).
3. Validator queries the Fortuna API, obtains `x_i`, and computes `r = hash(userContribution, x_i, blockhash(N))`.
4. If `r` is unfavorable (e.g., user wins a rare NFT, lottery, etc.), the validator reorgs block N, producing a new block N′ with a different `blockhash(N′)`.
5. Validator recomputes `r′ = hash(userContribution, x_i, blockhash(N′))` — since the validator controls the new block, they know `blockhash(N′)` before committing.
6. Validator keeps the reorg if `r′` is favorable; otherwise repeats.

The result is that the validator can effectively select among multiple random outcomes, identical in structure to the MysteryBox `REQUEST_CONFIRMATIONS = 3` exploit.

---

### Likelihood Explanation

- Polygon and other EVM-compatible chains on which Pyth Entropy is deployed have documented, frequent reorgs exceeding 1–3 blocks.
- `reveal_delay_blocks: 0` is the value shown in the only reference configuration file (`config.sample.yaml`), making it the default starting point for operators.
- The `request()` function with `useBlockHash = true` is still callable on-chain (not removed, only deprecated).
- The Fortuna revelation API is public and unauthenticated, so any validator can query it.
- No special privilege beyond being a block producer on the target chain is required.

---

### Recommendation

1. **Increase `reveal_delay_blocks`** in the sample config to a chain-appropriate value (e.g., 30 for Polygon, matching the MysteryBox fix). Document chain-specific minimums. [9](#0-8) 

2. **Set `confirmed_block_status`** to `Safe` or `Finalized` for chains that support it, so the delay is measured from a finalized anchor rather than the latest (potentially reorg-able) block. [10](#0-9) 

3. **Deprecate and gate `useBlockhash = true`** at the contract level, or add an on-chain minimum block-confirmation check before `revealHelper` uses `blockhash(req.blockNumber)`. [11](#0-10) 

---

### Proof of Concept

```
Chain: Polygon (frequent reorgs, ~2-second block time)
Config: reveal_delay_blocks: 0, confirmed_block_status: Latest

1. Alice calls entropy.request{value: fee}(provider, commitment, true)
   → Included in block 1000, req.blockNumber = 1000, sequenceNumber = 42

2. Validator queries:
   GET /v1/chains/polygon/revelations/42
   → Returns providerContribution = 0xABCD...

3. Validator computes:
   r = keccak256(userContribution || providerContribution || blockhash(1000))
   → r = 0x1234... (Alice wins rare NFT)

4. Validator reorgs block 1000, producing block 1000' with different transactions
   → blockhash(1000') = 0xDEAD... (validator-controlled)

5. Validator computes:
   r' = keccak256(userContribution || providerContribution || blockhash(1000'))
   → r' = 0x9999... (Alice wins common NFT)

6. Validator publishes block 1000' — Alice receives the unfavorable outcome.
   Validator repeats until a favorable outcome for themselves is achieved.
```

The attack requires zero privileged access beyond being a Polygon block producer, and the provider's contribution is freely available from the public Fortuna API at delay = 0. [12](#0-11) [13](#0-12)

### Citations

**File:** apps/fortuna/config.sample.yaml (L6-8)
```yaml
    # Keeper configuration for the chain
    reveal_delay_blocks: 0
    gas_limit: 500000
```

**File:** apps/fortuna/src/config.rs (L134-138)
```rust
    /// reveal_delay_blocks - The difference between the block number with the
    /// confirmed_block_status(see below) and the block number of a request to
    /// Entropy should be greater than `reveal_delay_blocks` for Fortuna to reveal
    /// its commitment.
    pub reveal_delay_blocks: BlockNumber,
```

**File:** apps/fortuna/src/config.rs (L140-143)
```rust
    /// The BlockStatus of the block that is considered confirmed.
    /// For example, Finalized, Safe, Latest
    #[serde(default)]
    pub confirmed_block_status: BlockStatus,
```

**File:** apps/fortuna/src/command/run.rs (L324-332)
```rust
    let state = BlockchainState {
        id: chain_id.clone(),
        state: Arc::new(monitored_chain_state),
        network_id,
        contract,
        provider_address: *provider,
        reveal_delay_blocks: chain_config.reveal_delay_blocks,
        confirmed_block_status: chain_config.confirmed_block_status,
    };
```

**File:** apps/fortuna/src/keeper/block.rs (L61-63)
```rust
                    latest_confirmed_block - chain_state.reveal_delay_blocks
                );
                return latest_confirmed_block - chain_state.reveal_delay_blocks;
```

**File:** apps/fortuna/src/api/revelation.rs (L64-84)
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
```

**File:** apps/fortuna/src/api/revelation.rs (L101-110)
```rust
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L262-263)
```text
        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L411-430)
```text
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
