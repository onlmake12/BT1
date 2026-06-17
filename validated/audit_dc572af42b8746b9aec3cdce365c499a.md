### Title
Echo Provider Missing Direct Fee Withdrawal Function — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `Echo` contract accrues fees for registered providers in `ProviderInfo.accruedFeesInWei`, but unlike the sibling `Entropy` contract which exposes a `withdraw(uint128 amount)` function allowing any provider to pull their own fees directly, `Echo` only exposes `withdrawAsFeeManager(address provider, uint96 amount)`. A provider who has never called `setFeeManager` has `feeManager == address(0)`, so `withdrawAsFeeManager` will always revert for them, and there is no alternative path to recover their accrued fees.

### Finding Description

`Entropy.sol` provides two fee-withdrawal paths for providers:

1. `withdraw(uint128 amount)` — callable by the provider itself (`msg.sender` is the provider). [1](#0-0) 

2. `withdrawAsFeeManager(address provider, uint128 amount)` — callable by a designated fee manager. [2](#0-1) 

`Echo.sol` only implements the second path. The `withdrawFees` function in `Echo` is an **admin-only** function that withdraws the *Pyth* protocol fee (`_state.accruedFeesInWei`), not the provider's fee: [3](#0-2) 

`withdrawAsFeeManager` in `Echo` checks `msg.sender == providerInfo.feeManager` and reverts if the fee manager is unset (i.e., `address(0)`): [4](#0-3) 

Provider fees do accrue in `ProviderInfo.accruedFeesInWei` on every fulfilled request: [5](#0-4) 

There is no `withdraw(uint96 amount)` function in `Echo.sol` or `IEcho.sol` that a provider can call directly. The `IScheduler`/`IEcho` interface and the `Echo` implementation both omit it. [6](#0-5) 

### Impact Explanation

An Echo provider who registers and begins accruing fees but never calls `setFeeManager` has `feeManager == address(0)`. Because `withdrawAsFeeManager` requires `msg.sender == providerInfo.feeManager`, and `address(0)` cannot sign transactions, the provider's accrued fees are permanently inaccessible through any on-chain path. The provider must discover the workaround of calling `setFeeManager(address(self))` before they can recover funds — an undocumented extra step not required in the analogous `Entropy` contract. This is a direct loss-of-funds risk for any provider who does not know about this requirement.

### Likelihood Explanation

Echo is a new contract. Any provider who registers and starts receiving fees without reading the implementation carefully will be affected. The Entropy contract (the established predecessor) trains providers to expect a direct `withdraw` call, making it likely that providers will attempt the same pattern on Echo and find their funds inaccessible.

### Recommendation

Add a `withdraw(uint96 amount)` function to `Echo.sol` mirroring the pattern in `Entropy.sol`:

```solidity
function withdraw(uint96 amount) external override {
    EchoState.ProviderInfo storage providerInfo = _state.providers[msg.sender];
    require(providerInfo.isRegistered, "Provider not registered");
    require(providerInfo.accruedFeesInWei >= amount, "Insufficient balance");
    providerInfo.accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
    require(sent, "withdrawal to msg.sender failed");
    emit ProviderWithdrawal(msg.sender, amount);
}
```

This should also be added to the `IEcho` interface.

### Proof of Concept

1. Provider calls `echo.registerProvider(fee, ...)` — `feeManager` defaults to `address(0)`.
2. Users call `echo.requestPriceUpdatesWithCallback{value: fee}(provider, ...)` — `providerInfo.accruedFeesInWei` grows.
3. Provider attempts `echo.withdraw(amount)` — **function does not exist**, call reverts.
4. Provider attempts `echo.withdrawAsFeeManager(providerAddress, amount)` — reverts because `providerInfo.feeManager == address(0) != msg.sender`.
5. Provider's accrued fees are locked with no recovery path until they discover and call `setFeeManager(address(self))` in a separate transaction. [3](#0-2) [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-173)
```text
    function withdraw(uint128 amount) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(msg.sender, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            msg.sender,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L175-209)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            provider,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-299)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/test/Echo.t.sol (L604-608)
```text
        // Get provider's accrued fees instead of total fees
        EchoState.ProviderInfo memory providerInfo = echo.getProviderInfo(
            defaultProvider
        );
        uint128 providerAccruedFees = providerInfo.accruedFeesInWei;
```

**File:** target_chains/ethereum/contracts/test/Echo.t.sol (L636-640)
```text
    function testWithdrawAsFeeManagerUnauthorized() public {
        vm.prank(address(0xdead));
        vm.expectRevert("Only fee manager");
        echo.withdrawAsFeeManager(defaultProvider, 1 ether);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L37-60)
```text
interface IEcho is EchoEvents {
    // Core functions
    /**
     * @notice Requests price updates with a callback
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
     * @param provider The provider to fulfill the request
     * @param publishTime The minimum publish time for price updates, it should be less than or equal to block.timestamp + 60
     * @param priceIds The price feed IDs to update. Maximum 10 price feeds per request.
     *        Requests requiring more feeds should be split into multiple calls.
     * @param callbackGasLimit The amount of gas allocated for the callback execution
     * @return sequenceNumber The sequence number assigned to this request
     * @dev Security note: The 60-second future limit on publishTime prevents a DoS vector where
     *      attackers could submit many low-fee requests for far-future updates when gas prices
     *      are low, forcing executors to fulfill them later when gas prices might be much higher.
     *      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
     *      the fee estimation unreliable.
     */
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable returns (uint64 sequenceNumber);

```
