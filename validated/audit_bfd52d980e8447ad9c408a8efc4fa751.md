### Title
No Registration Fee in `Entropy.register()` Enables Unbounded Provider Spam and Storage Bloat — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `register()` function in `Entropy.sol` charges no fee for provider registration. Any unprivileged address can call it repeatedly (re-registration of the same address is explicitly supported), passing arbitrarily large `commitmentMetadata` and `uri` byte arrays that are written directly to persistent contract storage. This allows an attacker to bloat the `providers` mapping and spam the event log at the cost of gas alone.

---

### Finding Description

`Entropy.register()` is declared `public override` with no `payable` modifier and no `msg.value` check:

```solidity
function register(
    uint128 feeInWei,
    bytes32 commitment,
    bytes calldata commitmentMetadata,
    uint64 chainLength,
    bytes calldata uri
) public override {
    if (chainLength == 0) revert EntropyErrors.AssertionFailure();
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    ...
    provider.commitmentMetadata = commitmentMetadata;
    ...
    provider.uri = uri;
    provider.sequenceNumber += 1;
    emit EntropyEvents.Registered(...);
    emit EntropyEventsV2.Registered(msg.sender, bytes(""));
}
``` [1](#0-0) 

Two properties make this exploitable:

1. **No registration fee**: The function is `nonpayable` and performs no `msg.value >= fee` check. The only cost to the caller is EVM gas.
2. **Re-registration is unrestricted**: Unlike `Echo.registerProvider()` which guards with `require(!provider.isRegistered, ...)`, `Entropy.register()` has no such guard. The comment at line 124 explicitly notes that `sequenceNumber` is incremented on every call, meaning the same address can call `register()` an unlimited number of times. [2](#0-1) 

The `commitmentMetadata` and `uri` parameters are dynamic `bytes` stored in the `providers` mapping: [3](#0-2) 

Each call with large byte arrays causes new storage slot allocations. Each call also emits two `Registered` events, spamming off-chain indexers.

The same pattern exists in `Echo.registerProvider()`, but that function has a `require(!provider.isRegistered, "Provider already registered")` guard, limiting each address to one registration. `Entropy.register()` has no such guard. [4](#0-3) 

---

### Impact Explanation

- **Storage bloat**: An attacker using many addresses (or re-registering the same address) with large `commitmentMetadata`/`uri` payloads writes unbounded data to the `providers` mapping at minimal cost.
- **Event log spam**: Each call emits two `Registered` events. Off-chain services (Fortuna, provider discovery) that index these events are degraded.
- **Provider list pollution**: Legitimate users and integrators querying the provider list encounter thousands of garbage entries, degrading usability and trust in the system.
- **Sequence number exhaustion per address**: Re-registering the same address increments `sequenceNumber` each time (`provider.sequenceNumber += 1`), which could be used to manipulate `endSequenceNumber` arithmetic for that address. [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged Ethereum address can call `register()` directly. No special role, key, or governance access is required. The only barrier is gas cost, which on low-fee chains (e.g., chains where Entropy is deployed) is negligible. The attack is straightforward to script.

---

### Recommendation

Introduce a `msg.value >= registrationFee` check in `register()`, analogous to the fee check in `requestHelper`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
``` [6](#0-5) 

A governance-controlled `registrationFeeInWei` parameter should be added to `EntropyState`, and `register()` should require `msg.value >= registrationFeeInWei`. Accrued registration fees can be credited to `accruedPythFeesInWei`. Additionally, consider adding a maximum byte length for `commitmentMetadata` and `uri` to bound per-call storage cost regardless of fee.

---

### Proof of Concept

```solidity
// Any address can register as a provider with no fee, repeatedly
for (uint i = 0; i < 1000; i++) {
    // Each call writes large bytes to storage and emits 2 events
    entropy.register(
        0,                          // feeInWei = 0 (attacker charges nothing)
        bytes32(uint256(i)),        // arbitrary commitment
        new bytes(10_000),          // large commitmentMetadata bloats storage
        1,                          // chainLength = 1 (minimum valid)
        new bytes(10_000)           // large uri bloats storage
    );
    // sequenceNumber increments each call, no re-registration guard
}
// Result: 1000 * 2 = 2000 Registered events emitted
// Result: ~20KB of storage written per address at gas cost only
```

The same address can call this in a loop. Different addresses can each register once, each writing a new `ProviderInfo` entry to the `providers` mapping. [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-235)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L35-36)
```text
        // Mapping from randomness providers to information about each them.
        mapping(address => EntropyStructsV2.ProviderInfo) providers;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }
```
