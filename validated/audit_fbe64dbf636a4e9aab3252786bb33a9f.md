### Title
Provider Can Set Unbounded `feePerGasInWei` Causing `getFee()` to Revert and Permanently Lock User Funds — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, the `setProviderFee` function places no upper bound on `feePerGasInWei`. A registered provider (or their fee manager) can set this value high enough that the multiplication `callbackGasLimit * feePerGasInWei` exceeds `type(uint96).max`, causing `SafeCast.toUint96(gasFee)` to revert inside `getFee()`. Because `requestPriceUpdatesWithCallback` calls `getFee()` internally, all new requests to that provider become permanently impossible. Combined with the absence of any user-initiated refund or cancellation path for pending requests, a malicious provider can lock user funds already held in the contract.

---

### Finding Description

`setProviderFee` in `Echo.sol` accepts arbitrary `uint96` values for `newBaseFeeInWei`, `newFeePerFeedInWei`, and `newFeePerGasInWei` with no cap: [1](#0-0) 

`getFee()` computes the gas component as:

```solidity
uint256 gasFee = callbackGasLimit * providerFeeInWei;
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
``` [2](#0-1) 

`callbackGasLimit` is `uint32` (max ≈ 4.3 × 10⁹). If `feePerGasInWei` is set to any value greater than `type(uint96).max / callbackGasLimit`, the product overflows `uint96` and `SafeCast.toUint96` reverts. For example, with `callbackGasLimit = 100_000` (a typical value), setting `feePerGasInWei` to just `~7.9 × 10²³` (well within `uint96`) causes the revert.

`requestPriceUpdatesWithCallback` calls `getFee()` directly: [3](#0-2) 

So once the provider raises `feePerGasInWei` past the threshold, every new request to that provider reverts. There is no user-callable cancel or refund function anywhere in the contract for pending requests whose `req.fee` is already held in the contract balance. [4](#0-3) 

The pending `req.fee` (provider fee portion paid upfront by the user) sits in the contract with no withdrawal path for the user: [5](#0-4) 

The only way those funds leave the contract is via `executeCallback`, which only the provider (or anyone after the exclusivity period) can call. A malicious provider can simply refuse to call it.

---

### Impact Explanation

A registered provider can:

1. Attract users with low fees.
2. Raise `feePerGasInWei` past the `SafeCast.toUint96` threshold, making `getFee()` revert for any non-zero `callbackGasLimit`.
3. Refuse to call `executeCallback` for already-pending requests.

Result: users' `req.fee` ETH is permanently locked in the contract with no recovery path. The admin's `withdrawFees` is bounded by `_state.accruedFeesInWei` (only the Pyth protocol fee portion) and cannot reach the locked provider-fee funds: [6](#0-5) 

---

### Likelihood Explanation

Any address can self-register as a provider via `registerProvider` with no vetting: [7](#0-6) 

The fee manager role (set by the provider via `setFeeManager`) also has unrestricted access to `setProviderFee`: [8](#0-7) 

No governance delay, timelock, or fee cap prevents an immediate fee spike. The attack requires only one `setProviderFee` transaction after users have submitted requests.

---

### Recommendation

1. **Add a maximum cap on `feePerGasInWei`** (and the other fee components) in `setProviderFee`, e.g., `require(newFeePerGasInWei <= MAX_FEE_PER_GAS, "fee too high")`.
2. **Add a user-callable refund function** that allows a requester to reclaim `req.fee` if the request has not been fulfilled within a configurable timeout window.
3. Consider a timelock or minimum notice period before fee increases take effect, so users can observe the change and avoid submitting new requests.

---

### Proof of Concept

```solidity
// 1. Attacker registers as a provider with low fees
vm.prank(attacker);
echo.registerProvider(1 wei, 1 wei, 1 wei);

// 2. User submits a request and pays req.fee upfront
vm.deal(user, 1 ether);
vm.prank(user);
uint64 seq = echo.requestPriceUpdatesWithCallback{value: echo.getFee(attacker, 100_000, priceIds)}(
    attacker, block.timestamp, priceIds, 100_000
);
// user's ETH is now locked in the contract as req.fee

// 3. Attacker raises feePerGasInWei past the SafeCast.toUint96 threshold
//    type(uint96).max / 100_000 ≈ 7.9e23; set to 7.9e23 + 1
vm.prank(attacker);
echo.setProviderFee(attacker, 1 wei, 1 wei, uint96(type(uint96).max / 100_000 + 1));

// 4. getFee now reverts for any callbackGasLimit >= 100_000
vm.expectRevert(); // SafeCast overflow
echo.getFee(attacker, 100_000, priceIds);

// 5. Attacker never calls executeCallback — user's req.fee is permanently locked
// No refund function exists for the user to recover funds
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-76)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-84)
```text
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L248-254)
```text
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-426)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );

        uint96 oldBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 oldFeePerFeed = _state.providers[provider].feePerFeedInWei;
        uint96 oldFeePerGas = _state.providers[provider].feePerGasInWei;
        _state.providers[provider].baseFeeInWei = newBaseFeeInWei;
        _state.providers[provider].feePerFeedInWei = newFeePerFeedInWei;
        _state.providers[provider].feePerGasInWei = newFeePerGasInWei;
        emit ProviderFeeUpdated(
            provider,
            oldBaseFee,
            oldFeePerFeed,
            oldFeePerGas,
            newBaseFeeInWei,
            newFeePerFeedInWei,
            newFeePerGasInWei
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L12-29)
```text
    struct Request {
        // Slot 1: 8 + 8 + 4 + 12 = 32 bytes
        uint64 sequenceNumber;
        uint64 publishTime;
        uint32 callbackGasLimit;
        uint96 fee;
        // Slot 2: 20 + 12 = 32 bytes
        address requester;
        // 12 bytes padding

        // Slot 3: 20 + 12 = 32 bytes
        address provider;
        // 12 bytes padding

        // Dynamic array starts at its own slot
        // Store only first 8 bytes of each price ID to save gas
        bytes8[] priceIdPrefixes;
    }
```
