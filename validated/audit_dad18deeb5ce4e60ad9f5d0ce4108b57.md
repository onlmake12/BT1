### Title
Entropy Provider Can Sandwich User Requests via Instant Fee Mutation to Cause DoS or Unrefunded ETH Loss — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

A registered Entropy provider can front-run (sandwich) a user's `requestV2` / `requestWithCallback` transaction by atomically mutating their `feeInWei` via `setProviderFee` with no timelock. Because `requestHelper` enforces `msg.value >= requiredFee` at execution time (not at the fee the user observed), and because **excess `msg.value` is never refunded**, the provider can either (a) increase the fee to revert the user's transaction (DoS), or (b) decrease the fee so the user's transaction succeeds but the surplus ETH is permanently credited to Pyth's treasury rather than returned to the user.

---

### Finding Description

**Root cause 1 — instant, unrestricted fee mutation:**

`setProviderFee` applies the new fee immediately with no delay or timelock:

```solidity
// Entropy.sol line 810-827
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) revert EntropyErrors.NoSuchProvider();
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;
    emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
    ...
}
``` [1](#0-0) 

The same instant mutation is also possible via `register`, which overwrites `provider.feeInWei` in a single call. [2](#0-1) 

**Root cause 2 — no `maxFee` slippage guard in `requestV2`:**

`requestHelper` reads the live `feeInWei` at execution time and checks only a lower bound:

```solidity
// Entropy.sol line 234-239
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [3](#0-2) 

There is no upper-bound (`maxFee`) parameter in any of the public entry points:

```solidity
function requestV2(address provider, bytes32 userContribution, uint32 gasLimit)
    public payable override returns (uint64)
``` [4](#0-3) 

**Root cause 3 — excess `msg.value` is permanently lost to the Pyth treasury:**

The entire surplus `msg.value - providerFee` is credited to `accruedPythFeesInWei` and is never refunded to the caller. The code comment in the public API explicitly warns: *"Note that excess value is not refunded to the caller."* [5](#0-4) 

---

### Impact Explanation

**Scenario A — DoS via fee increase sandwich:**

1. Provider registers with fee = `X`.
2. User calls `getFeeV2(provider, 0)` off-chain → observes fee = `X`.
3. User submits `requestV2(provider, contribution, 0)` with `msg.value = X`.
4. Provider front-runs with `setProviderFee(X + 1)`.
5. User's transaction reverts with `InsufficientFee`.
6. Provider resets fee to `X` and repeats.

Result: user loses gas on every attempt; randomness request is indefinitely blocked.

**Scenario B — ETH loss via fee decrease sandwich:**

1. Provider registers with fee = `X`.
2. User (or an automated contract) submits `requestV2` with `msg.value = X + buffer` (common defensive pattern to avoid reverts from minor fee fluctuations).
3. Provider front-runs with `setProviderFee(near 0)`.
4. User's transaction succeeds; `msg.value - near_0 ≈ X + buffer` is credited to Pyth treasury.
5. Provider resets fee to `X`.

Result: user permanently loses `X + buffer - near_0` ETH; provider can repeat this against any user who sends a buffer.

The `ProviderInfo.feeInWei` field in `EntropyStructsV2` has no minimum change delay or pending-change queue: [6](#0-5) 

---

### Likelihood Explanation

- Any address can permissionlessly register as an Entropy provider (`register` is public, no allowlist).
- `setProviderFee` is callable by the provider or their designated `feeManager` at any time with no cooldown.
- On EVM chains, a provider can atomically sandwich a user's pending transaction in the same block using standard MEV tooling (flashbots bundles, etc.).
- Scenario A requires zero capital; Scenario B requires only that the provider accept one request at a reduced fee.

---

### Recommendation

1. **Add a `maxFee` parameter** to `requestV2` / `requestWithCallback` so callers can specify the maximum fee they are willing to pay, reverting if the live fee exceeds it — analogous to slippage protection in AMMs.
2. **Refund excess `msg.value`** to the caller after deducting `requiredFee`, rather than routing the surplus to the Pyth treasury.
3. **Introduce a fee-change timelock** (e.g., a two-step pending/committed pattern) so that fee increases only take effect after N blocks, giving users time to observe the change before submitting requests.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

interface IEntropy {
    function setProviderFee(uint128 newFeeInWei) external;
    function requestV2(address provider, bytes32 userContribution, uint32 gasLimit)
        external payable returns (uint64);
    function getFeeV2(address provider, uint32 gasLimit) external view returns (uint128);
}

contract MaliciousProvider {
    IEntropy entropy;

    // Step 1: provider is registered with fee = 0.001 ether
    // Step 2: user queries getFeeV2 → 0.001 ether, submits requestV2 with msg.value = 0.001 ether
    // Step 3: provider front-runs in the same block:

    function sandwichIncrease() external {
        // Increase fee by 1 wei → user's pending tx will revert with InsufficientFee
        entropy.setProviderFee(0.001 ether + 1);
    }

    // After user's tx reverts, provider resets:
    function resetFee() external {
        entropy.setProviderFee(0.001 ether);
    }

    // Scenario B: decrease fee so user's buffered msg.value is swallowed by Pyth treasury
    function sandwichDecrease() external {
        entropy.setProviderFee(1 wei); // near-zero; user sent 0.001 ether + buffer
        // user's tx executes: ~0.001 ether goes to accruedPythFeesInWei, not refunded
    }
}
```

The attack entry path is entirely unprivileged: `setProviderFee` → mempool front-run → `requestHelper` reads mutated `feeInWei` at execution time → `InsufficientFee` revert (DoS) or unrefunded surplus (ETH loss). [7](#0-6) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-362)
```text
    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L809-827)
```text
    // Set provider fee. It will revert if provider is not registered.
    function setProviderFee(uint128 newFeeInWei) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
        uint128 oldFeeInWei = provider.feeInWei;
        provider.feeInWei = newFeeInWei;
        emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
        emit EntropyEventsV2.ProviderFeeUpdated(
            msg.sender,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L6-42)
```text
    struct ProviderInfo {
        uint128 feeInWei;
        uint128 accruedFeesInWei;
        // The commitment that the provider posted to the blockchain, and the sequence number
        // where they committed to this. This value is not advanced after the provider commits,
        // and instead is stored to help providers track where they are in the hash chain.
        bytes32 originalCommitment;
        uint64 originalCommitmentSequenceNumber;
        // Metadata for the current commitment. Providers may optionally use this field to help
        // manage rotations (i.e., to pick the sequence number from the correct hash chain).
        bytes commitmentMetadata;
        // Optional URI where clients can retrieve revelations for the provider.
        // Client SDKs can use this field to automatically determine how to retrieve random values for each provider.
        // TODO: specify the API that must be implemented at this URI
        bytes uri;
        // The first sequence number that is *not* included in the current commitment (i.e., an exclusive end index).
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
