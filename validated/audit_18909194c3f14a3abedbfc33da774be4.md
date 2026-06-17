### Title
Attacker Can Exhaust Provider's `maxNumHashes` Limit to Block All New Entropy Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `maxNumHashes` guard in the Entropy contract, introduced to protect against unbounded gas costs during reveal, creates an analogous attack vector to the Paraspace M-10 finding: any unprivileged caller can make many requests against a provider without ever revealing them, driving the outstanding-request gap above `maxNumHashes` and causing every subsequent legitimate request to revert with `LastRevealedTooOld`. On L2 networks where provider fees are low, the attack cost is minimal.

---

### Finding Description

The `maxNumHashes` field in `ProviderInfo` is set by a provider to cap the number of hash iterations required in `constructProviderCommitment`. The check is enforced inside `requestHelper`:

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

`numHashes` grows monotonically with every new request that is not yet revealed, because `assignedSequenceNumber` increments on every call while `currentCommitmentSequenceNumber` only advances when a reveal or `advanceProviderCommitment` is processed.

Any address can call `request()` / `requestWithCallback()` / `requestV2()` for any registered provider. Each call increments `providerInfo.sequenceNumber` by 1. An attacker who submits `maxNumHashes` requests without triggering reveals pushes the gap to exactly `maxNumHashes + 1`, causing every subsequent call from any user to revert with `LastRevealedTooOld`. The provider must call `advanceProviderCommitment` to recover, but the attacker can immediately re-fill the gap in the same block or the next one.

The structural parallel to Paraspace M-10 is exact:

| Paraspace M-10 | Pyth Entropy analog |
|---|---|
| `balanceLimit` on NTokens protects against gas exhaustion in `calculateUserAccountData` | `maxNumHashes` protects against gas exhaustion in `constructProviderCommitment` |
| Attacker mints zero-liquidity NFTs and supplies them to victim | Attacker makes `maxNumHashes` requests to provider, never revealing |
| Victim's balance hits the cap; their own supply tx reverts | Provider's gap hits the cap; all new requests revert `LastRevealedTooOld` |
| Attack cost: only gas (zero-liquidity NFTs are free) | Attack cost: provider fee × `maxNumHashes` (can be near-zero on L2) |

---

### Impact Explanation

All users of the targeted provider are denied randomness service for as long as the attacker keeps the outstanding-request gap above `maxNumHashes`. Any contract that calls `requestWithCallback` in a critical path (e.g., NFT mints, lottery draws, game mechanics) will have its transactions revert. The provider must call `advanceProviderCommitment` to recover, but the attacker can immediately re-fill the gap, making recovery a continuous race. If the provider's `feeInWei` is zero or near-zero (common on L2 deployments), the attacker's only cost is gas.

---

### Likelihood Explanation

- `request()` and `requestWithCallback()` are fully permissionless; no role or whitelist check exists.
- The attacker does not need to know the provider's hash chain secret; they only need to pay the fee.
- On L2 networks (Arbitrum, Base, Optimism, Blast — all listed Entropy deployments), gas is cheap and provider fees are often set to small amounts, making the attack economically viable.
- The off-chain Fortuna keeper (`update_commitments_loop`) monitors the gap and calls `advanceProviderCommitment` when it exceeds 95 % of `maxNumHashes`, but this is an off-chain mitigation that can be outpaced by a determined attacker submitting requests in rapid succession.

---

### Recommendation

1. **Rate-limit requests per block or per address** at the contract level, so a single address cannot fill the entire `maxNumHashes` gap in one transaction or one block.
2. **Alternatively, track per-address outstanding requests** and cap them, analogous to the Paraspace recommendation of disallowing zero-liquidity NFTs.
3. **Require a minimum fee** that makes exhausting `maxNumHashes` economically infeasible (e.g., fee ≥ cost of one `advanceProviderCommitment` call × `maxNumHashes`).
4. Consider making `advanceProviderCommitment` callable permissionlessly (it already is) and ensuring the Fortuna keeper's response latency is lower than the time needed to re-fill the gap.

---

### Proof of Concept

```solidity
// Attacker exhausts provider1's maxNumHashes (e.g., 10) in one go.
// After this loop, every subsequent request to provider1 reverts LastRevealedTooOld.
for (uint i = 0; i < provider1MaxNumHashes; i++) {
    vm.deal(attacker, fee);
    vm.prank(attacker);
    random.requestWithCallback{value: fee}(provider1, bytes32(uint(i)));
}

// Legitimate user's request now reverts.
vm.deal(user1, fee);
vm.prank(user1);
vm.expectRevert(EntropyErrors.LastRevealedTooOld.selector);
random.requestWithCallback{value: fee}(provider1, bytes32(uint(999)));

// Provider calls advanceProviderCommitment to recover...
// Attacker immediately re-fills the gap in the same block.
```

The root cause is in `requestHelper`: [1](#0-0) 

The `maxNumHashes` guard itself: [2](#0-1) 

The provider state fields that define the exploitable gap: [3](#0-2) 

The permissionless entry points any attacker can call: [4](#0-3) 

The off-chain keeper that already acknowledges this as a known DoS vector but cannot prevent it on-chain: [5](#0-4)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L346-390)
```text
    function requestWithCallback(
        address provider,
        bytes32 userContribution
    ) public payable override returns (uint64) {
        return
            requestV2(
                provider,
                userContribution,
                0 // Passing 0 will assign the request the provider's default gas limit
            );
    }

    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );

        emit RequestedWithCallback(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            EntropyStructConverter.toV1Request(req)
        );
        emit EntropyEventsV2.Requested(
            provider,
            req.requester,
            req.sequenceNumber,
            userContribution,
            uint32(req.gasLimit10k) * TEN_THOUSAND,
            bytes("")
        );
        return req.sequenceNumber;
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L41-43)
```text
    // The last random number revealed from the provider is too old. Therefore, too many hashes
    // are required for any new reveal. Please update the currentCommitment before making more requests.
    error LastRevealedTooOld();
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L22-42)
```text
        // The contract maintains the invariant that sequenceNumber <= endSequenceNumber.
        // If sequenceNumber == endSequenceNumber, the provider must rotate their commitment to add additional random values.
        uint64 endSequenceNumber;
        // The sequence number that will be assigned to the next inbound user request.
        uint64 sequenceNumber;
        // The current commitment represents an index/value in the provider's hash chain.
        // These values are used to verify requests for future sequence numbers. Note that
        // currentCommitmentSequenceNumber < sequenceNumber.
        //
        // The currentCommitment advances forward through the provider's hash chain as values
        // are revealed on-chain.
        bytes32 currentCommitment;
        uint64 currentCommitmentSequenceNumber;
        // An address that is authorized to set / withdraw fees on behalf of this provider.
        address feeManager;
        // Maximum number of hashes to record in a request. This should be set according to the maximum gas limit
        // the provider supports for callbacks.
        uint32 maxNumHashes;
        // Default gas limit to use for callbacks.
        uint32 defaultGasLimit;
    }
```

**File:** apps/fortuna/src/keeper/commitment.rs (L55-70)
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
    }
```
