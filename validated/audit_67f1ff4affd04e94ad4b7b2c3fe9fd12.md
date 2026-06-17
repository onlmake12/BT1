### Title
Unbounded Hash Loop in `constructProviderCommitment` Enables Permanent DoS of Entropy Request Fulfillment — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `constructProviderCommitment` function in `Entropy.sol` contains an unbounded `while` loop that iterates `numHashes` times. For providers that have not set `maxNumHashes` (the default is `0`, meaning no cap), an unprivileged attacker can make many cheap `requestWithCallback` calls to inflate `numHashes` for later requests to a value whose gas cost exceeds the block gas limit. Those requests become permanently unfulfillable, and the fees paid by users are locked in the contract with no refund path.

---

### Finding Description

**Root cause — unbounded loop:**

`constructProviderCommitment` at lines 987–996 of `Entropy.sol` iterates `numHashes` times with no upper bound enforced inside the function itself:

```solidity
function constructProviderCommitment(
    uint64 numHashes,
    bytes32 revelation
) internal pure returns (bytes32 currentHash) {
    currentHash = revelation;
    while (numHashes > 0) {                          // ← unbounded
        currentHash = keccak256(bytes.concat(currentHash));
        numHashes -= 1;
    }
}
``` [1](#0-0) 

**How `numHashes` is set:**

At request time, `numHashes` is stored in the request as:

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
``` [2](#0-1) 

This value equals the number of outstanding requests since the provider last advanced their commitment. It is fixed at request creation time and never updated.

**The guard is opt-in and defaults to disabled:**

The only protection is `maxNumHashes`, which defaults to `0` (no cap) because `ProviderInfo` is zero-initialized:

```solidity
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [3](#0-2) 

Any provider that has not explicitly called `setMaxNumHashes` is unprotected.

**Attack path:**

1. Attacker identifies a provider with `maxNumHashes == 0` and a low (or zero) `feeInWei`.
2. Attacker calls `requestWithCallback` (or `requestV2`) N times in rapid succession, paying only the fee per call. On low-gas-cost chains (Polygon, BNB Chain, etc.) with a provider fee of 0 and a small `pythFeeInWei`, the total cost is negligible.
3. Each call increments `providerInfo.sequenceNumber` by 1 while `currentCommitmentSequenceNumber` stays fixed. The Nth request stores `numHashes = N`.
4. When the provider calls `revealWithCallback` for the Nth request, `constructProviderCommitment` must compute N keccak256 hashes. At ~30 gas each, N ≈ 1,000,000 exceeds Ethereum's ~30 M gas block limit.
5. The Nth request can never be fulfilled. The user's fee is permanently locked in the contract; there is no refund function.

`revealWithCallback` calls `revealHelper`, which calls `constructProviderCommitment`: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

- **Permanent DoS on Entropy requests**: Any request whose stored `numHashes` exceeds `blockGasLimit / 30` (~1 M on Ethereum, far fewer on chains with lower limits) can never be fulfilled by any caller.
- **User funds locked**: The fee paid by the user at request time is credited to the provider and Pyth fee pool; there is no refund path for an unfulfillable request.
- **Provider service disruption**: The provider's Fortuna keeper will repeatedly fail to fulfill the affected requests, degrading the reliability of the randomness service.

---

### Likelihood Explanation

- **Attacker entry point is fully permissionless**: `requestWithCallback` / `requestV2` require only paying the fee; no privileged role is needed.
- **Default state is vulnerable**: `maxNumHashes` defaults to `0` for every newly registered provider. Any provider that has not explicitly called `setMaxNumHashes` is exposed.
- **Cost scales with chain and provider fee**: On Ethereum mainnet with a non-trivial provider fee the attack is expensive (~1000 ETH for 1 M requests). On low-fee chains (Polygon, BNB Chain, Arbitrum) with a provider fee of 0 and a small `pythFeeInWei`, the cost is only transaction gas, making the attack practical.

---

### Recommendation

1. **Require `maxNumHashes > 0` before a provider can accept requests.** Add a check in `requestHelper`:
   ```solidity
   if (providerInfo.maxNumHashes == 0) revert EntropyErrors.MaxNumHashesNotSet();
   ```
2. **Alternatively, enforce a protocol-level hard cap** on `numHashes` in `requestHelper` regardless of the provider's setting, e.g., `require(req.numHashes <= GLOBAL_MAX_NUM_HASHES)`.
3. **Add a refund path** for requests that have been outstanding longer than a configurable timeout, so users are not permanently locked out of their fees.

---

### Proof of Concept

```solidity
// Attacker script (pseudocode)
address provider = /* provider with maxNumHashes == 0 and low fee */;
uint256 fee = entropy.getFee(provider);

// Make N requests without the provider advancing their commitment.
// N chosen so that N * 30 gas > block gas limit (e.g., N = 1_100_000 on Ethereum).
for (uint i = 0; i < N; i++) {
    entropy.requestWithCallback{value: fee}(provider, keccak256(abi.encode(i)));
}

// The Nth request now has numHashes = N.
// Provider's revealWithCallback for that request will always revert with OOG.
// User fee for that request is permanently locked.
```

The `constructProviderCommitment` loop for the Nth request:

```
while (1_100_000 > 0) {
    currentHash = keccak256(...);  // ~30 gas each
    numHashes -= 1;
}
// Total: ~33,000,000 gas > 30,000,000 block gas limit → OOG
``` [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-250)
```text
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L252-256)
```text
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-439)
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

        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            req.provider
        ];
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-566)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        bytes32 randomNumber;
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
