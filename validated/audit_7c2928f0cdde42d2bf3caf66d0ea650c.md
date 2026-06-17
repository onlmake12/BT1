### Title
Unprivileged Users Can Permanently Stall New Entropy Requests by Exhausting `maxNumHashes` via Unrevealed Non-Callback Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `requestHelper` function in `Entropy.sol` enforces a shared, provider-level `maxNumHashes` guard. The gap counter `numHashes = assignedSequenceNumber − currentCommitmentSequenceNumber` grows whenever non-callback requests are made and never revealed. Because only the original requester can call `reveal()`, an attacker can make `maxNumHashes` non-callback requests and simply never reveal them, causing every subsequent request from any user to revert with `LastRevealedTooOld`. The provider must actively call `advanceProviderCommitment()` to recover, and the attacker can repeat the attack immediately after each recovery.

---

### Finding Description

In `requestHelper`, before storing a new request, the contract computes:

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

`currentCommitmentSequenceNumber` is a **single shared value per provider**. It only advances when a request is revealed via `revealHelper`:

```solidity
if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
    providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
    providerInfo.currentCommitment = providerContribution;
}
``` [2](#0-1) 

For non-callback requests made via `request()`, the `reveal()` function enforces that **only the original requester** can reveal:

```solidity
if (req.requester != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
``` [3](#0-2) 

The non-callback `request()` entry point is fully permissionless:

```solidity
function request(
    address provider,
    bytes32 userCommitment,
    bool useBlockHash
) public payable override returns (uint64 assignedSequenceNumber) {
``` [4](#0-3) 

An attacker calls `request()` exactly `maxNumHashes` times and never calls `reveal()`. Because no one else can reveal those requests, `currentCommitmentSequenceNumber` stays frozen. The next legitimate request from any user computes `numHashes = (k + maxNumHashes + 1) − k = maxNumHashes + 1`, which exceeds the limit and reverts. The Fortuna keeper's automatic reveals of callback requests do advance `currentCommitmentSequenceNumber`, but only for requests the keeper itself fulfills — the attacker's unrevealed non-callback requests are permanently stuck in the gap.

The only recovery path is for the provider to call `advanceProviderCommitment()`:

```solidity
function advanceProviderCommitment(
    address provider,
    uint64 advancedSequenceNumber,
    bytes32 providerContribution
) public override {
``` [5](#0-4) 

This requires the provider to know the hash-chain value at the target sequence number (only the provider knows this), and to submit a transaction. The attacker can immediately repeat the attack after each recovery, creating a sustained griefing loop.

The `maxNumHashes` field is part of `ProviderInfo` and is set per-provider:

```solidity
uint32 maxNumHashes;
uint32 defaultGasLimit;
``` [6](#0-5) 

When `maxNumHashes == 0` the check is skipped, but any provider that sets it to a non-zero value (the intended use case for gas-bounded callbacks) is vulnerable.

---

### Impact Explanation

All new entropy requests to the targeted provider revert with `LastRevealedTooOld` until the provider manually calls `advanceProviderCommitment()`. During the attack window, no user — including those using the default Fortuna provider — can obtain randomness. Applications depending on Entropy (e.g., NFT mints, gaming, lotteries) are completely stalled. The provider must monitor on-chain state and respond to each attack cycle, creating an indefinite operational burden.

---

### Likelihood Explanation

The attack is cheap on any L2. If the provider sets `maxNumHashes = 100` and the per-request fee is $0.001 on Arbitrum, the attacker spends $0.10 per cycle. The provider must pay gas for each `advanceProviderCommitment()` recovery. The attacker can automate the attack to fire immediately after each recovery. There is no rate-limiting, no minimum deposit, and no slashing mechanism. Any unprivileged address can call `request()`.

---

### Recommendation

1. **Allow anyone to clear stale non-callback requests after a timeout** (e.g., if a non-callback request has not been revealed within N blocks, allow the provider or anyone to call a `clearStaleRequest()` function that advances `currentCommitmentSequenceNumber` past it).
2. **Alternatively, allow the provider to reveal non-callback requests on behalf of the requester** when the request is older than a configurable timeout, so the keeper can unblock the queue.
3. **Or, in `requestHelper`, skip unrevealed non-callback requests when computing `numHashes`** by tracking the highest sequence number that has been either revealed or explicitly abandoned, rather than using `currentCommitmentSequenceNumber` as the sole lower bound.

---

### Proof of Concept

```
Setup:
  provider.maxNumHashes = 100
  provider.currentCommitmentSequenceNumber = k
  provider.sequenceNumber = k + 1

Step 1 (Attacker):
  for i in range(100):
      call request(provider, userCommitment_i, false)
      // pays fee, never calls reveal()
  // provider.sequenceNumber is now k + 101
  // provider.currentCommitmentSequenceNumber is still k

Step 2 (Legitimate user):
  call requestV2{ value: fee }()
  // assignedSequenceNumber = k + 101
  // numHashes = (k + 101) - k = 101 > 100
  // REVERTS: LastRevealedTooOld

Step 3 (Provider recovery):
  call advanceProviderCommitment(provider, k + 100, hash_chain[k+100])
  // currentCommitmentSequenceNumber advances to k + 100

Step 4 (Attacker repeats immediately):
  for i in range(100):
      call request(provider, userCommitment_i, false)
  // currentCommitmentSequenceNumber is k + 100
  // provider.sequenceNumber is now k + 201
  // numHashes for next request = (k + 201) - (k + 100) = 101 > 100
  // All new requests blocked again
```

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L435-438)
```text
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
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

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L39-42)
```text
        uint32 maxNumHashes;
        // Default gas limit to use for callbacks.
        uint32 defaultGasLimit;
    }
```
