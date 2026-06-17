### Title
Unbounded `numHashes` Loop in `constructProviderCommitment` Can Cause OOG Revert in `reveal`/`revealWithCallback` — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `constructProviderCommitment` function in `Entropy.sol` iterates `numHashes` times in a `while` loop. `numHashes` is protocol-computed at request time as the gap between the new sequence number and the provider's last advanced commitment. When `maxNumHashes == 0` (the default for providers that do not explicitly set it), there is no upper bound on this value. An unprivileged user can flood requests to grow `numHashes` to a value that causes `reveal` and `revealWithCallback` to revert with Out of Gas (OOG), permanently bricking fulfillment of those requests.

---

### Finding Description

`constructProviderCommitment` runs a `while` loop that hashes `numHashes` times:

```solidity
function constructProviderCommitment(
    uint64 numHashes,
    bytes32 revelation
) internal pure returns (bytes32 currentHash) {
    currentHash = revelation;
    while (numHashes > 0) {
        currentHash = keccak256(bytes.concat(currentHash));
        numHashes -= 1;
    }
}
``` [1](#0-0) 

This function is called inside `revealHelper` with `req.numHashes`:

```solidity
bytes32 providerCommitment = constructProviderCommitment(
    req.numHashes,
    providerContribution
);
``` [2](#0-1) 

`req.numHashes` is set at request time as:

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
``` [3](#0-2) 

The only guard against a large `numHashes` is:

```solidity
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [4](#0-3) 

When `maxNumHashes == 0` (the default — providers must explicitly call `setMaxNumHashes` to enable this guard), the check is entirely skipped. An unprivileged user can call `requestV2` (or `request`) many times in succession without the provider advancing their commitment. Each new request records a larger `numHashes`. When the provider later calls `revealWithCallback` for a high-`numHashes` request, `constructProviderCommitment` iterates that many times, consuming gas proportional to `numHashes`. If `numHashes` is large enough, the transaction OOGs before the callback is ever reached.

`revealHelper` is called by both `reveal` and `revealWithCallback`: [5](#0-4) 

The `excessivelySafeCall` gas cap only protects the downstream callback to the requester — it does **not** protect the `constructProviderCommitment` loop, which runs unconditionally in the main execution context before the callback.

The `maxNumHashes` field exists in `EntropyStructsV2.ProviderInfo` precisely to bound this cost:

> "Maximum number of hashes to record in a request. This should be set according to the maximum gas limit the provider supports for callbacks." [6](#0-5) 

However, `maxNumHashes` defaults to `0` (disabled) and must be explicitly set by each provider. Providers that omit this configuration are fully exposed.

---

### Impact Explanation

- `reveal` and `revealWithCallback` permanently revert with OOG for any request whose stored `numHashes` exceeds the gas budget of the transaction.
- Affected users can never receive their random number; the request is stuck in the contract with no recovery path (the request cannot be re-revealed with a lower `numHashes`).
- For `revealWithCallback`, the Fortuna keeper's automated fulfillment loop will also fail, causing a service outage for all users of that provider.

---

### Likelihood Explanation

- Any unprivileged user can call `requestV2` paying only the provider fee per request. On low-fee chains (e.g., BNB, Polygon, Blast), flooding hundreds or thousands of requests is cheap.
- Providers that have not set `maxNumHashes` (i.e., `maxNumHashes == 0`) are fully unprotected. The default state of a newly registered provider has `maxNumHashes == 0`.
- Even without a malicious actor, a legitimate high-traffic burst (many users requesting simultaneously before the provider advances its commitment) can organically push `numHashes` high enough to OOG on gas-constrained chains.
- The Fortuna keeper already monitors for this condition and warns when `outstanding_requests > threshold`, confirming the protocol team is aware of the risk but relies on an off-chain mitigation. [7](#0-6) 

---

### Recommendation

1. **Enforce a non-zero `maxNumHashes` at registration time.** Either require providers to set a non-zero `maxNumHashes` during `register`, or set a protocol-level default cap.
2. **Add a hard protocol-level cap** on `numHashes` independent of the provider-set value, sized to the worst-case gas budget of `constructProviderCommitment` on the target chain.
3. **Stress-test the gas cost** of `constructProviderCommitment` at the maximum allowed `numHashes` to confirm it fits within the block gas limit with sufficient margin for the rest of `revealWithCallback`.

---

### Proof of Concept

1. Provider registers with `maxNumHashes == 0` (default).
2. Attacker calls `requestV2` N times (paying the fee each time), where N is large enough that `numHashes = N` exceeds the gas budget of `constructProviderCommitment`.
3. Provider's Fortuna keeper calls `revealWithCallback` for the N-th request.
4. `revealHelper` calls `constructProviderCommitment(N, providerContribution)`, which loops N times.
5. Transaction OOGs. The request is permanently stuck; the user never receives their random number.

The gas cost per iteration is one `keccak256` (~30 gas). At N = 200,000, the loop alone consumes ~6M gas, which exceeds the block gas limit on many chains. Even at N = 10,000 (a realistic burst), the loop consumes ~300k gas before any other logic in `revealWithCallback` runs. [8](#0-7) [9](#0-8)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L400-403)
```text
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L562-566)
```text
        (randomNumber, ) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L987-996)
```text
    function constructProviderCommitment(
        uint64 numHashes,
        bytes32 revelation
    ) internal pure returns (bytes32 currentHash) {
        currentHash = revelation;
        while (numHashes > 0) {
            currentHash = keccak256(bytes.concat(currentHash));
            numHashes -= 1;
        }
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L37-39)
```text
        // Maximum number of hashes to record in a request. This should be set according to the maximum gas limit
        // the provider supports for callbacks.
        uint32 maxNumHashes;
```

**File:** apps/fortuna/src/keeper/commitment.rs (L55-61)
```rust
    let threshold =
        ((provider_info.max_num_hashes as f64) * UPDATE_COMMITMENTS_THRESHOLD_FACTOR) as u64;
    let outstanding_requests =
        provider_info.sequence_number - provider_info.current_commitment_sequence_number;
    if outstanding_requests > threshold {
        // NOTE: This log message triggers a grafana alert. If you want to change the text, please change the alert also.
        tracing::warn!("Update commitments threshold reached -- possible outage or DDOS attack. Number of outstanding requests: {:?} Threshold: {:?}", outstanding_requests, threshold);
```
