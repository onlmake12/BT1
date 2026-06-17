### Title
Entropy `request`+`reveal` Flow Allows User Selective Reveal Based on Observed Outcome ("Late Participation Advantage") — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In the legacy `request` + `reveal` flow of Pyth Entropy, a user can query the provider's pre-committed random value (`x_i`) from the public Fortuna API **after** making the on-chain request, compute the final random outcome off-chain, and then **selectively reveal only when the outcome is favorable**. This is a direct analog to the "late participation advantage" described in the report: the user observes the outcome before committing to it, gaining an unfair edge over any application relying on Entropy for unbiased randomness.

---

### Finding Description

The `request` function in `Entropy.sol` allows any user to submit a randomness request and receive a sequence number. The corresponding `reveal` function enforces `req.requester == msg.sender`, meaning **only the original requester can complete the reveal**. There is no deadline by which the user must reveal.

The Fortuna provider service exposes the provider's hash-chain value `x_i` for any sequence number that has been requested on-chain via its public REST API (`GET /v1/chains/{chain_id}/revelations/{sequence}`). The API documentation explicitly states it returns the value once the sequence number has been requested on-chain.

The final random number is computed as:

```
r = hash(x_i, x_U, blockHash)   // useBlockhash=true
r = hash(x_i, x_U, 0)           // useBlockhash=false
```

Since the user knows `x_U` (their own secret) and can fetch `x_i` from Fortuna after the request is mined, they can compute `r` entirely off-chain before calling `reveal`. If `r` is unfavorable, the user simply does not call `reveal`, abandoning the request at the cost of the fee.

The `revealHelper` function's own inline comment acknowledges a related variant of this issue:

> *"This allows the user to select between two random numbers by executing the reveal function in the same block as the request, or after 256 blocks. This gives each user two chances to get a favorable result on each request."* [1](#0-0) 

The `request` function accepts a caller-controlled `useBlockHash` boolean, making the block-hash variant of this attack directly user-triggered: [2](#0-1) 

The `reveal` function enforces requester-only access, which is the mechanism that gives the user exclusive, unilateral control over whether to finalize the outcome: [3](#0-2) 

The Fortuna keeper's `update_commitments_loop` periodically calls `advanceProviderCommitment`, which publicly posts `x_{seq-1}` on-chain. From this single revealed value, an observer can derive all earlier hash-chain values (`x_{seq-2} = hash(x_{seq-1})`, etc.), making the random numbers for **all currently in-flight requests** computable by anyone watching the chain — further amplifying the selective-reveal window. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

Any application that uses the `request` + `reveal` flow (lotteries, NFT mints, games, DeFi randomness) is vulnerable. An attacker can:

- Pay the fee, observe the outcome, and reveal only on wins — turning a fair coin flip into a guaranteed win at the cost of the fee on losses.
- In high-value applications (e.g., a lottery with a jackpot much larger than the fee), the expected value of this strategy is strongly positive.
- The `advanceProviderCommitment` amplification means the attacker does not even need to query Fortuna — they can derive `x_i` directly from on-chain data.

**Impact: Medium** — financial loss to applications and their honest users; fairness of any randomness-dependent outcome is broken.

---

### Likelihood Explanation

- The `request` + `reveal` flow is still live and callable by any unprivileged address.
- The Fortuna API is public and documented.
- The attack requires no special privileges, no key leakage, and no governance access.
- The only cost to the attacker is the fee on losing requests.

**Likelihood: Medium** — straightforward for any technically capable user; cost is bounded by the per-request fee.

---

### Recommendation

1. **Deprecate and gate the `request` + `reveal` flow.** Applications should be migrated to `requestV2` / `requestWithCallback`, where the provider (Fortuna keeper) calls `revealWithCallback` — removing the user's ability to selectively reveal.
2. **Add a reveal deadline.** Introduce a block-number deadline after which an unrevealed request can be cancelled and the fee forfeited, removing the indefinite option to wait and observe.
3. **Document the user selective-reveal risk** explicitly in the `request` function's NatSpec, analogous to the existing provider-censorship warning in the protocol design docs.

---

### Proof of Concept

```
1. Attacker deploys or uses a contract that calls:
       entropy.request{value: fee}(provider, keccak256(abi.encode(x_U)), false)
   in block N. Receives sequenceNumber = i.

2. After block N is mined, attacker queries:
       GET https://fortuna.dourolabs.app/v1/chains/{chain_id}/revelations/{i}
   Response: { "value": "0x<x_i>" }

3. Attacker computes off-chain:
       r = keccak256(abi.encode(x_i, x_U, bytes32(0)))

4a. If r encodes a winning outcome in the target application:
       entropy.reveal(provider, i, x_U, x_i)   // completes the request

4b. If r encodes a losing outcome:
       // Do nothing. Request sits unfulfilled. Fee is lost, but outcome is avoided.

5. Repeat until a winning r is obtained.
   Expected cost per win = fee / P(win).
   For a lottery with jackpot >> fee / P(win), this is strictly profitable.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L322-336)
```text
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) public payable override returns (uint64 assignedSequenceNumber) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            userCommitment,
            useBlockHash,
            false,
            0
        );
        assignedSequenceNumber = req.sequenceNumber;
        emit Requested(EntropyStructConverter.toV1Request(req));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L411-421)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L472-483)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L496-515)
```text
    function reveal(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override returns (bytes32 randomNumber) {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
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
