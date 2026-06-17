### Title
Provider Frontrunning Fee Decrease Causes Unrefunded User Overpayment With No Slippage Control - (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
The `requestHelper` function in `Entropy.sol` does not refund excess `msg.value` and provides no `maxFee` slippage guard. A registered provider can frontrun a user's `requestV2` call by atomically decreasing their fee via `setProviderFee`, causing the user's transaction to succeed while silently routing the overpayment to `accruedPythFeesInWei` — permanently lost to the user.

### Finding Description

`requestHelper` computes the required fee at execution time from the current on-chain `provider.feeInWei`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The entire surplus `msg.value - providerFee` is credited to Pyth protocol fees — never refunded. The interface documentation explicitly acknowledges this:

> "Note that the fee can change over time… Further note that excess value is *not* refunded to the caller." [2](#0-1) 

`setProviderFee` and `setProviderFeeAsFeeManager` take effect immediately with no timelock or delay:

```solidity
function setProviderFee(uint128 newFeeInWei) external override {
    ...
    provider.feeInWei = newFeeInWei;
``` [3](#0-2) 

None of the four `requestV2` variants accept a `maxFee` parameter:

```solidity
function requestV2() external payable returns (uint64);
function requestV2(uint32 gasLimit) external payable returns (uint64);
function requestV2(address provider, uint32 gasLimit) external payable returns (uint64);
function requestV2(address provider, bytes32 userRandomNumber, uint32 gasLimit) external payable returns (uint64);
``` [4](#0-3) 

Provider registration is permissionless — anyone can register and set arbitrary fees: [5](#0-4) 

### Impact Explanation

A user reads `getFeeV2(provider, gasLimit)` off-chain (or in a prior block) and submits `requestV2{value: fee}()`. A malicious provider frontruns by calling `setProviderFee(lowerFee)`. When the user's transaction executes:

- `requiredFee` is now lower than `msg.value`
- The check `msg.value < requiredFee` passes
- `providerFee` is the new lower value
- The delta `msg.value - newProviderFee` — which includes the entire original provider fee premium — is credited to `accruedPythFeesInWei` and is permanently unrecoverable by the user

The user pays the old (higher) fee but the provider only accrues the new (lower) fee. The difference is silently confiscated as Pyth protocol fees. The user receives the random number service but suffers a direct, unrecoverable ETH loss proportional to `oldProviderFee - newProviderFee`.

### Likelihood Explanation

Provider registration is permissionless. Any address can register as a provider and set fees. The `setProviderFee` call takes effect in the same block with no delay. A malicious provider can monitor the mempool for pending `requestV2` calls and frontrun them with a fee decrease. On chains with public mempools (all EVM chains where Entropy is deployed), this is straightforward. The Fortuna keeper infrastructure itself calls `set_provider_fee_as_fee_manager` dynamically based on gas prices, meaning fee changes are a normal operational event — making malicious fee changes indistinguishable from legitimate ones. [6](#0-5) 

### Recommendation

Add a `maxFee` parameter to `requestV2` variants as a slippage guard, reverting if `requiredFee > maxFee`:

```solidity
function requestV2(
    address provider,
    bytes32 userRandomNumber,
    uint32 gasLimit,
    uint128 maxFee   // new slippage control
) external payable returns (uint64) {
    uint128 requiredFee = getFeeV2(provider, gasLimit);
    if (requiredFee > maxFee) revert EntropyErrors.FeeTooHigh();
    ...
}
```

Additionally, consider refunding `msg.value - requiredFee` to `msg.sender` when the user overpays, rather than routing the surplus to `accruedPythFeesInWei`.

### Proof of Concept

1. Provider registers with `feeInWei = 1000 wei`. Pyth fee = 100 wei. `getFeeV2() = 1100 wei`.
2. User's contract reads `getFeeV2()` off-chain → 1100 wei. Submits `requestV2{value: 1100}()`.
3. Provider frontruns: calls `setProviderFee(100)`. Now `getFeeV2() = 200 wei`.
4. User's transaction executes:
   - `requiredFee = 200`, `msg.value = 1100 >= 200` → no revert
   - `providerFee = 100`
   - `providerInfo.accruedFeesInWei += 100`
   - `accruedPythFeesInWei += (1100 - 100) = 1000`
5. User paid 1100 wei. Provider earned 100 wei. Pyth earned 1000 wei. User lost 900 wei with no recourse. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-820)
```text
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
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L26-101)
```text
    function requestV2()
        external
        payable
        returns (uint64 assignedSequenceNumber);

    /// @notice Request a random number using the default provider with specified gas limit
    /// @param gasLimit The gas limit for the callback function.
    /// @return assignedSequenceNumber A unique identifier for this request
    /// @dev The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    /// The `entropyCallback` method on that interface will receive a callback with the returned sequence number and
    /// the generated random number.
    ///
    /// `entropyCallback` will be run with the `gasLimit` provided to this function.
    /// The `gasLimit` will be rounded up to a multiple of 10k (e.g., 19000 -> 20000), and furthermore is lower bounded
    /// by the provider's configured default limit.
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(gasLimit)`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2(gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    ///
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
    function requestV2(
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);

    /// @notice Request a random number from a specific provider with specified gas limit
    /// @param provider The address of the provider to request from
    /// @param gasLimit The gas limit for the callback function
    /// @return assignedSequenceNumber A unique identifier for this request
    /// @dev The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    /// The `entropyCallback` method on that interface will receive a callback with the returned sequence number and
    /// the generated random number.
    ///
    /// `entropyCallback` will be run with the `gasLimit` provided to this function.
    /// The `gasLimit` will be rounded up to a multiple of 10k (e.g., 19000 -> 20000), and furthermore is lower bounded
    /// by the provider's configured default limit.
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    ///
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);

    /// @notice Request a random number from a specific provider with a user-provided random number and gas limit
    /// @param provider The address of the provider to request from
    /// @param userRandomNumber A random number provided by the user for additional entropy
    /// @param gasLimit The gas limit for the callback function. Pass 0 to get a sane default value -- see note below.
    /// @return assignedSequenceNumber A unique identifier for this request
    /// @dev The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    /// The `entropyCallback` method on that interface will receive a callback with the returned sequence number and
    /// the generated random number.
    ///
    /// `entropyCallback` will be run with the `gasLimit` provided to this function.
    /// The `gasLimit` will be rounded up to a multiple of 10k (e.g., 19000 -> 20000), and furthermore is lower bounded
    /// by the provider's configured default limit.
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
    function requestV2(
        address provider,
        bytes32 userRandomNumber,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);
```

**File:** apps/fortuna/src/keeper/fee.rs (L379-384)
```rust
            target_fee
        );
        let contract_call = contract.set_provider_fee_as_fee_manager(provider_address, target_fee);
        send_and_confirm(contract_call).await?;

        *sequence_number_of_last_fee_update = Some(provider_info.sequence_number);
```
