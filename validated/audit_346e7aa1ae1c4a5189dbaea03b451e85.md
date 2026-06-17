### Title
Permanent Subscription Creation Allows Enabled Whitelist With Empty Reader List, Permanently Blocking All Non-Manager Price Data Access - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `_validateSubscriptionParams` function does not check for consistency between `whitelistEnabled` and `readerWhitelist`. This allows any unprivileged user to call `createSubscription` with `whitelistEnabled = true`, an empty `readerWhitelist`, and `isPermanent = true`, producing a permanently immutable subscription where no address except the manager can ever read price data — and the state can never be corrected.

---

### Finding Description

`_validateSubscriptionParams` validates whitelist parameters only for size and uniqueness:

```solidity
// Whitelist size limit and uniqueness
if (params.readerWhitelist.length > MAX_READER_WHITELIST_SIZE) {
    revert SchedulerErrors.TooManyWhitelistedReaders(...);
}
for (uint i = 0; i < params.readerWhitelist.length; i++) {
    for (uint j = i + 1; j < params.readerWhitelist.length; j++) {
        if (params.readerWhitelist[i] == params.readerWhitelist[j]) {
            revert SchedulerErrors.DuplicateWhitelistAddress(...);
        }
    }
}
``` [1](#0-0) 

There is no check that `whitelistEnabled == true` requires `readerWhitelist.length > 0`. The `onlyWhitelistedReader` modifier enforces access as follows:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    // Manager is always allowed
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    // If whitelist is not used, allow any reader
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // Check if caller is in whitelist
    address[] storage whitelist = _state.subscriptionParams[subscriptionId].readerWhitelist;
    bool isWhitelisted = false;
    for (uint i = 0; i < whitelist.length; i++) {
        if (whitelist[i] == msg.sender) { isWhitelisted = true; break; }
    }
    if (!isWhitelisted) { revert SchedulerErrors.Unauthorized(); }
    _;
}
``` [2](#0-1) 

When `whitelistEnabled = true` and `readerWhitelist` is empty, the loop finds no match and every non-manager caller reverts. For a **permanent** subscription, `updateSubscription` immediately reverts:

```solidity
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [3](#0-2) 

This means the whitelist can never be populated and `whitelistEnabled` can never be set to `false`. The broken state is permanent and irrecoverable.

The `createSubscription` function accepts and stores these parameters without any cross-field consistency check:

```solidity
function createSubscription(
    SchedulerStructs.SubscriptionParams memory subscriptionParams
) external payable override returns (uint256 subscriptionId) {
    _validateSubscriptionParams(subscriptionParams);
    // ...
    _state.subscriptionParams[subscriptionId] = subscriptionParams;
``` [4](#0-3) 

The `SubscriptionParams` struct confirms both fields are independently settable:

```solidity
struct SubscriptionParams {
    bytes32[] priceIds;
    address[] readerWhitelist; // Optional array of addresses allowed to read prices
    bool whitelistEnabled;     // Whether to enforce whitelist or allow anyone to read
    bool isActive;
    bool isPermanent;
    UpdateCriteria updateCriteria;
}
``` [5](#0-4) 

---

### Impact Explanation

A subscription created with `whitelistEnabled = true`, `readerWhitelist = []`, and `isPermanent = true` results in:

1. **All non-manager callers permanently blocked** from `getPricesUnsafe` / `getPricesNoOlderThan` — the empty whitelist loop never matches, and `Unauthorized` is always thrown.
2. **No recourse**: `updateSubscription` reverts for permanent subscriptions, so neither `whitelistEnabled` can be set to `false` nor can any address be added to `readerWhitelist`.
3. **Funds permanently locked**: permanent subscriptions also block `withdrawFunds`, so the deposited ETH is irrecoverable.
4. **Keeper fees continue draining**: keepers can still call `updatePriceFeeds` (no whitelist on that path), so the balance is drained for price updates that no one except the manager can consume.

---

### Likelihood Explanation

Any unprivileged user calling `createSubscription` can trigger this state — no special role or key is required. A developer integrating Pulse who intends to add readers after making the subscription permanent, or who misunderstands the interaction between `whitelistEnabled` and `readerWhitelist`, will produce this state. The combination of `isPermanent = true` with `whitelistEnabled = true` and an empty whitelist is a realistic misconfiguration with no on-chain guard.

---

### Recommendation

Add a consistency check inside `_validateSubscriptionParams`:

```solidity
// If whitelist is enabled, at least one reader must be present
if (params.whitelistEnabled && params.readerWhitelist.length == 0) {
    revert SchedulerErrors.EmptyWhitelistWithWhitelistEnabled();
}
```

This mirrors the fix applied in the Suzaku `VaultTokenized` report: validate the combination of the "whitelist enabled" flag and the "whitelist population" field unconditionally, not only under certain sub-conditions.

---

### Proof of Concept

```solidity
function test_PermanentSubscriptionEmptyWhitelistLockout() public {
    bytes32[] memory priceIds = new bytes32[](1);
    priceIds[0] = bytes32(uint256(1));
    address[] memory emptyWhitelist = new address[](0);

    SchedulerStructs.SubscriptionParams memory params = SchedulerStructs.SubscriptionParams({
        priceIds: priceIds,
        readerWhitelist: emptyWhitelist,
        whitelistEnabled: true,   // whitelist ON
        isActive: true,
        isPermanent: true,        // immutable forever
        updateCriteria: SchedulerStructs.UpdateCriteria({
            updateOnHeartbeat: true,
            heartbeatSeconds: 60,
            updateOnDeviation: false,
            deviationThresholdBps: 0
        })
    });

    uint256 minBalance = scheduler.getMinimumBalance(1);
    uint256 subId = scheduler.createSubscription{value: minBalance}(params);

    // Non-manager cannot read — reverts Unauthorized
    vm.prank(address(0xdead));
    vm.expectRevert(SchedulerErrors.Unauthorized.selector);
    scheduler.getPricesUnsafe(subId, new bytes32[](0));

    // Manager cannot fix it — permanent subscription blocks all updates
    vm.expectRevert(SchedulerErrors.CannotUpdatePermanentSubscription.selector);
    params.whitelistEnabled = false;
    scheduler.updateSubscription(subId, params);

    // Funds are also permanently locked
    vm.expectRevert(SchedulerErrors.CannotUpdatePermanentSubscription.selector);
    scheduler.withdrawFunds(subId, minBalance);
}
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L32-58)
```text
    function createSubscription(
        SchedulerStructs.SubscriptionParams memory subscriptionParams
    ) external payable override returns (uint256 subscriptionId) {
        _validateSubscriptionParams(subscriptionParams);

        // Calculate minimum balance required for this subscription
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );

        // Ensure enough funds were provided
        if (msg.value < minimumBalance) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        // Set subscription to active
        subscriptionParams.isActive = true;

        subscriptionId = _state.subscriptionNumber++;

        // Store the subscription parameters
        _state.subscriptionParams[subscriptionId] = subscriptionParams;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L183-198)
```text
        // Whitelist size limit and uniqueness
        if (params.readerWhitelist.length > MAX_READER_WHITELIST_SIZE) {
            revert SchedulerErrors.TooManyWhitelistedReaders(
                params.readerWhitelist.length,
                MAX_READER_WHITELIST_SIZE
            );
        }
        for (uint i = 0; i < params.readerWhitelist.length; i++) {
            for (uint j = i + 1; j < params.readerWhitelist.length; j++) {
                if (params.readerWhitelist[i] == params.readerWhitelist[j]) {
                    revert SchedulerErrors.DuplicateWhitelistAddress(
                        params.readerWhitelist[i]
                    );
                }
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L750-779)
```text
    modifier onlyWhitelistedReader(uint256 subscriptionId) {
        // Manager is always allowed
        if (_state.subscriptionManager[subscriptionId] == msg.sender) {
            _;
            return;
        }

        // If whitelist is not used, allow any reader
        if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) {
            _;
            return;
        }

        // Check if caller is in whitelist
        address[] storage whitelist = _state
            .subscriptionParams[subscriptionId]
            .readerWhitelist;
        bool isWhitelisted = false;
        for (uint i = 0; i < whitelist.length; i++) {
            if (whitelist[i] == msg.sender) {
                isWhitelisted = true;
                break;
            }
        }

        if (!isWhitelisted) {
            revert SchedulerErrors.Unauthorized();
        }
        _;
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L9-16)
```text
    struct SubscriptionParams {
        bytes32[] priceIds; // Array of Pyth price feed IDs to subscribe to
        address[] readerWhitelist; // Optional array of addresses allowed to read prices
        bool whitelistEnabled; // Whether to enforce whitelist or allow anyone to read
        bool isActive; // Whether the subscription is active
        bool isPermanent; // Whether the subscription can be updated
        UpdateCriteria updateCriteria; // When to update the price feeds
    }
```
