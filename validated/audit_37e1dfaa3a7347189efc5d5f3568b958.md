### Title
Attacker Can Exhaust Entropy Provider's Committed Sequence Numbers Causing `OutOfRandomness` DOS — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

Any unprivileged caller can invoke `requestV2()` or `request()` repeatedly, consuming a provider's entire committed hash-chain range. Once `providerInfo.sequenceNumber >= providerInfo.endSequenceNumber`, every subsequent request reverts with `OutOfRandomness`, denying service to all legitimate users of that provider until the provider manually re-registers.

---

### Finding Description

In `requestHelper`, the contract unconditionally increments the provider's global sequence counter and checks it against the committed end:

```solidity
uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
    revert EntropyErrors.OutOfRandomness();
providerInfo.sequenceNumber += 1;
``` [1](#0-0) 

The `endSequenceNumber` is fixed at registration time as `sequenceNumber + chainLength`:

```solidity
provider.endSequenceNumber = provider.sequenceNumber + chainLength;
``` [2](#0-1) 

There is no per-requester rate limit, no minimum stake, and no access control on `requestV2()` / `request()`. Any EOA can call these functions, paying only the provider fee + Pyth fee per slot consumed. The attacker never needs to reveal the randomness — the in-flight requests simply remain open, and the sequence counter is permanently advanced. The provider's only recourse is to call `register()` again to extend `endSequenceNumber`, but the attacker can immediately exhaust the new range as well.

The public entry points that feed into `requestHelper` with no caller restriction are:

```solidity
function requestV2() external payable override ...
function requestV2(uint32 gasLimit) external payable override ...
function requestV2(address provider, bytes32 userContribution, uint32 gasLimit) public payable override ...
function request(address provider, bytes32 userCommitment, bool useBlockHash) public payable override ...
``` [3](#0-2) 

---

### Impact Explanation

Once the provider's sequence numbers are exhausted, every call to any `request*` variant reverts with `OutOfRandomness`:

```solidity
error OutOfRandomness(); // Provider is out of committed random numbers.
``` [4](#0-3) 

All dApps and users relying on that provider for on-chain randomness are completely blocked. The default provider (used by the no-argument `requestV2()`) is the most impactful target because it serves the broadest user base. The DOS persists until the provider detects the exhaustion and submits a new `register()` transaction, which itself can be immediately re-exhausted.

---

### Likelihood Explanation

**Medium.** The attacker must pay `getFeeV2(provider, gasLimit)` per slot consumed, which includes both the provider fee and the Pyth protocol fee. However:

- Providers with small `chainLength` values (e.g., tens of thousands) can be exhausted for a modest ETH cost.
- The attacker's requests are never revealed, so the attacker loses the fee but gains a sustained DOS window.
- The attack can be repeated every time the provider re-registers, creating a persistent harassment vector.
- The `requestV2()` zero-argument variant uses an in-contract PRNG (`random()`), so the attacker does not even need to supply a user commitment — the call is a single-argument payable call. [5](#0-4) 

---

### Recommendation

1. **Per-requester rate limiting**: Track the number of active (unrevealed) requests per `msg.sender` and cap it (e.g., 10 concurrent open requests per address).
2. **Minimum deposit / bond**: Require requesters to post a refundable bond that is slashed if the request is never revealed within a timeout window. This raises the economic cost of abandoning requests.
3. **Provider-side allowlist**: Allow providers to optionally restrict which addresses may request from them.
4. **Automatic chain extension**: Allow providers to pre-authorize automatic chain rotation so that exhaustion does not cause a service gap.

---

### Proof of Concept

```solidity
// Attacker exhausts the default provider's entire committed chain
function attack(IEntropy entropy, uint64 chainLength) external payable {
    uint256 fee = entropy.getFeeV2();
    // chainLength - 1 usable slots (slot 0 is consumed by registration)
    for (uint64 i = 0; i < chainLength - 1; i++) {
        entropy.requestV2{value: fee}();
        // Requests are never revealed; sequence counter advances permanently
    }
    // All subsequent legitimate requests now revert with OutOfRandomness
}
```

After this loop, any call by a legitimate user to `requestV2()` targeting the same provider reverts:

```solidity
if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
    revert EntropyErrors.OutOfRandomness();
``` [6](#0-5) 

The provider must call `register()` again to restore service, but the attacker can immediately re-exhaust the new range, creating a sustained DOS for the cost of `(chainLength - 1) × fee` per cycle.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L136-136)
```text
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L228-231)
```text
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-336)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }

    function requestV2(
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(
            getDefaultProvider(),
            random(),
            gasLimit
        );
    }

    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(provider, random(), gasLimit);
    }

    // As a user, request a random number from `provider`. Prior to calling this method, the user should
    // generate a random number x and keep it secret. The user should then compute hash(x) and pass that
    // as the userCommitment argument. (You may call the constructUserCommitment method to compute the hash.)
    //
    // This method returns a sequence number. The user should pass this sequence number to
    // their chosen provider (the exact method for doing so will depend on the provider) to retrieve the provider's
    // number. The user should then call fulfillRequest to construct the final random number.
    //
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
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

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L21-21)
```text
    error OutOfRandomness();
```
