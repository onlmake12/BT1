### Title
Excess ETH Permanently Absorbed as Protocol Fees in Entropy Request Functions - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
The `requestHelper()` internal function in `Entropy.sol`, called by all public `request`, `requestWithCallback`, and `requestV2` entry points, does not refund excess ETH sent by callers. Instead, any overpayment above the required fee is silently credited to `_state.accruedPythFeesInWei` (Pyth's protocol treasury), causing permanent loss of user funds.

### Finding Description
In `requestHelper()`, the fee accounting logic is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

The expression `SafeCast.toUint128(msg.value) - providerFee` credits the **entire** `msg.value` minus the provider's share to Pyth's fee pool. If a user sends `msg.value > requiredFee`, the excess (`msg.value - requiredFee`) is silently absorbed into `accruedPythFeesInWei` rather than being returned to the caller. There is no refund path.

This affects every public entry point that routes through `requestHelper`:
- `request(address provider, bytes32 userCommitment, bool useBlockHash)` [1](#0-0) 
- `requestWithCallback(address provider, bytes32 userContribution)` [2](#0-1) 
- All `requestV2(...)` overloads [3](#0-2) 

The root cause is at lines 238–239: [4](#0-3) 

The interface documentation explicitly acknowledges this: `IEntropyV2.sol` states "excess value is *not* refunded to the caller" on every `requestV2` variant. [5](#0-4) [6](#0-5) 

This is in direct contrast to `PythLazer.sol`'s `verifyUpdate()` and `PythAggregatorV3.sol`'s `updateFeeds()`, both of which explicitly refund excess ETH to `msg.sender`. [7](#0-6) [8](#0-7) 

### Impact Explanation
Any Entropy user (EOA or contract) who sends `msg.value` exceeding the exact required fee loses the excess permanently. The excess is credited to Pyth's `accruedPythFeesInWei` pool, which is only withdrawable by Pyth governance — not the original sender. This results in direct, irreversible financial loss for users. Integrating contracts that pass `msg.value` through from their own callers (e.g., a DeFi protocol wrapping Entropy) are especially susceptible, as they may forward a rounded-up or stale fee estimate.

### Likelihood Explanation
Likelihood is moderate-to-high. Fee amounts can change between the time a user queries `getFeeV2()` and the time their transaction is mined (e.g., provider fee update in a preceding block). Users and integrators commonly add a small buffer to avoid `InsufficientFee` reverts — a standard defensive practice. Every such overpayment results in fund loss. The behavior is documented but not enforced at the UI/SDK level, and the pattern is inconsistent with other Pyth contracts that do refund excess.

### Recommendation
Add a refund of excess ETH at the end of `requestHelper()`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (requiredFee - providerFee); // use requiredFee, not msg.value

uint256 excess = msg.value - requiredFee;
if (excess > 0) {
    (bool success, ) = msg.sender.call{value: excess}("");
    require(success, "ETH refund failed");
}
```

This mirrors the pattern already used in `PythLazer.verifyUpdate()` and `PythAggregatorV3.updateFeeds()`.

### Proof of Concept
1. Provider fee is set to 0.001 ETH; Pyth protocol fee is 0.0001 ETH; `requiredFee = 0.0011 ETH`.
2. User calls `requestV2{value: 0.002 ETH}()` (sending a 0.0009 ETH buffer to avoid revert).
3. `requestHelper` executes: `providerInfo.accruedFeesInWei += 0.001 ETH`; `_state.accruedPythFeesInWei += (0.002 - 0.001) = 0.001 ETH`.
4. The 0.0009 ETH excess above `requiredFee` is silently absorbed into `accruedPythFeesInWei`.
5. User receives no refund. The excess is only recoverable by Pyth governance via `withdrawFees`. [9](#0-8)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-390)
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

    // Request a random number. The method expects the provider address and a secret random number
    // in the arguments. It returns a sequence number.
    //
    // The address calling this function should be a contract that inherits from the IEntropyConsumer interface.
    // The `entropyCallback` method on that interface will receive a callback with the generated random number.
    //
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L46-47)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L73-77)
```text
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L26-38)
```text
    function updateFeeds(bytes[] calldata priceUpdateData) public payable {
        // Update the prices to the latest available values and pay the required fee for it. The `priceUpdateData` data
        // should be retrieved from our off-chain Price Service API using the `hermes-client` package.
        // See section "How Pyth Works on EVM Chains" below for more information.
        uint fee = pyth.getUpdateFee(priceUpdateData);
        pyth.updatePriceFeeds{value: fee}(priceUpdateData);

        // refund remaining eth
        // solhint-disable-next-line no-unused-vars
        (bool success, ) = payable(msg.sender).call{
            value: address(this).balance
        }("");
    }
```
