### Title
Unbounded `constructProviderCommitment` Hash Loop Enables Gas-Exhaustion DoS on Entropy Reveal — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
The `constructProviderCommitment` function in `Entropy.sol` iterates `numHashes` times in an unbounded `while` loop. `numHashes` is stored per-request as a `uint32` (max ~4.3 billion) and is set at request time as `assignedSequenceNumber − currentCommitmentSequenceNumber`. When a provider has not set `maxNumHashes` (the default is 0, meaning no limit), an unprivileged user can submit enough sequential requests before any reveal occurs to inflate `numHashes` to a value that causes the provider's `revealWithCallback` or `reveal` transaction to exceed the block gas limit, permanently locking those requests.

### Finding Description

**Root cause — `constructProviderCommitment`:**

```solidity
// Entropy.sol lines 987-996
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

`numHashes` is a `uint32` field in `EntropyStructsV2.Request`, set at request time:

```solidity
// Entropy.sol lines 247-250
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
```

The only guard against a large value is:

```solidity
// Entropy.sol lines 251-256
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
```

`maxNumHashes` defaults to `0`, which **disables the check entirely**. Any provider that has not explicitly called `setProviderMaxNumHashes` is unprotected.

**Attack path:**

1. Attacker calls `requestV2()` (or `requestWithCallback`) N times in rapid succession, paying the required fee each time, before the provider's Fortuna keeper reveals any request.
2. Each call increments `providerInfo.sequenceNumber` by 1 while `currentCommitmentSequenceNumber` stays fixed.
3. The N-th request is stored with `numHashes = N`.
4. When the provider's keeper calls `revealWithCallback(provider, seqN, ...)`, `revealHelper` calls `constructProviderCommitment(N, providerContribution)`, which loops N times executing `keccak256`.
5. At ~30 gas per `keccak256`, N ≈ 1,000,000 consumes ~30 million gas — at or above the Ethereum mainnet block gas limit — causing the transaction to revert with out-of-gas.
6. The request is permanently stuck: `callbackStatus` remains `CALLBACK_NOT_STARTED` (reset on revert at line 599), but the provider cannot fulfill it without exceeding the gas limit. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

Requests with large `numHashes` become permanently unfulfillable: the provider's reveal transaction always runs out of gas. If the attacker fills enough in-flight request slots with high-`numHashes` requests, the provider cannot service any of those slots, effectively halting the Entropy service for affected users. Funds paid as fees for stuck requests are not refunded. This is a service-availability impact on the Entropy protocol. [4](#0-3) 

### Likelihood Explanation

**Medium-low.** The attacker must pay the provider fee for each request. To reach N ≈ 1,000,000 (the threshold for ~30M gas on Ethereum), the cost is N × fee. At a typical fee of ~$0.001–$0.01 per request, the attack costs $1,000–$10,000. This is feasible for a motivated attacker targeting a high-value protocol. The risk is higher on chains with lower fees (e.g., BNB Chain, Polygon) where the same N can be reached for cents. Providers that have not set `maxNumHashes` (the default) are fully exposed. [5](#0-4) 

### Recommendation

1. **Enforce `maxNumHashes` unconditionally.** Remove the `maxNumHashes != 0` guard and require all providers to set a non-zero value during `register`. Alternatively, set a protocol-level hard cap (e.g., 10,000) applied regardless of `maxNumHashes`.
2. **Bound `constructProviderCommitment` at the call site.** Add a revert if `numHashes` exceeds a safe constant before entering the loop.
3. **Require providers to call `advanceProviderCommitment` regularly** to keep `currentCommitmentSequenceNumber` close to `sequenceNumber`, minimizing the gap. [6](#0-5) 

### Proof of Concept

```
Setup:
- Provider P registered with maxNumHashes = 0 (default), chainLength = 2,000,000
- currentCommitmentSequenceNumber = 0

Attack:
1. Attacker calls requestV2(P, ...) 1,000,001 times, paying fee each time.
   After each call, providerInfo.sequenceNumber increments.
   Request #1,000,001 is stored with numHashes = 1,000,001.

2. Provider keeper calls revealWithCallback(P, seqNum=1000001, userContrib, providerContrib).
   revealHelper calls constructProviderCommitment(1000001, providerContrib).
   Loop executes 1,000,001 × keccak256 ≈ 30,000,030 gas → exceeds block gas limit → OOG revert.

3. callbackStatus is reset to CALLBACK_NOT_STARTED (line 599), but the transaction
   can never succeed. The request is permanently stuck.

Expected result:
- All 1,000,001 attacker requests are permanently unfulfillable.
- Provider's keeper is unable to clear these slots.
- New users cannot use those request slots until the provider rotates commitment
  and the stuck requests are manually cleared (if possible).
``` [7](#0-6) [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-408)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-600)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
            bool success;
            bytes memory ret;
            uint256 startingGas = gasleft();
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());
            // Reset status to not started here in case the transaction reverts.
            req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L985-996)
```text
    // Construct a provider's commitment given their revealed random number and the distance in the hash chain
    // between the commitment and the revealed random number.
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

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L37-42)
```text
        // Maximum number of hashes to record in a request. This should be set according to the maximum gas limit
        // the provider supports for callbacks.
        uint32 maxNumHashes;
        // Default gas limit to use for callbacks.
        uint32 defaultGasLimit;
    }
```
