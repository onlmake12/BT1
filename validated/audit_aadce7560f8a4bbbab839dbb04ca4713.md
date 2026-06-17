### Title
`_validateSubscriptionParams` Inconsistently Applied in `Scheduler.updateSubscription` Inactive→Inactive Path Allows Permanent Fund Lock — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`_validateSubscriptionParams` is applied in `createSubscription` and in every active code path of `updateSubscription`, but is entirely skipped when a subscription is inactive and will remain inactive (`!wasActive && !willBeActive`). This allows any subscription manager to write arbitrary unvalidated parameters — including setting `isPermanent = true` — directly into storage on an inactive subscription. Once `isPermanent = true` is stored this way, the subscription can never be reactivated and the manager's ETH can never be withdrawn, permanently locking funds with no recovery path.

---

### Finding Description

In `Scheduler.sol`, `updateSubscription` contains an early-return path:

```solidity
// Lines 94–102
bool wasActive = currentParams.isActive;
bool willBeActive = newParams.isActive;
if (!wasActive && !willBeActive) {
    // Update subscription parameters
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;                          // ← skips _validateSubscriptionParams entirely
}
_validateSubscriptionParams(newParams);   // ← only reached when active path
```

When both `wasActive` and `willBeActive` are `false`, `_validateSubscriptionParams` is never called and `newParams` is written directly to storage. Every other entry point applies the validator:

| Code path | `_validateSubscriptionParams` called? |
|---|---|
| `createSubscription` | ✅ Yes (line 35) |
| `updateSubscription` active→active | ✅ Yes (line 103) |
| `updateSubscription` active→inactive | ✅ Yes (line 103) |
| `updateSubscription` inactive→active | ✅ Yes (line 103) |
| **`updateSubscription` inactive→inactive** | ❌ **No — early return at line 101** |

The `isPermanent` field in `SubscriptionParams` is never separately guarded in this path. An attacker (the subscription manager) can therefore flip `isPermanent` from `false` to `true` on an inactive subscription without any check.

**Exploit path:**

1. Manager creates a subscription (`isActive = true`, `isPermanent = false`) with `msg.value = minimumBalance`.
2. Manager deactivates it: calls `updateSubscription(subId, params)` with `params.isActive = false`. This takes the active→inactive path; `_validateSubscriptionParams` runs normally.
3. Manager calls `updateSubscription(subId, params)` again with `params.isActive = false` and `params.isPermanent = true`. This hits the inactive→inactive early return — **no validation** — and stores `isPermanent = true` in `_state.subscriptionParams[subscriptionId]`.
4. Any subsequent `updateSubscription` call (including reactivation with `isActive = true`) hits:

```solidity
// Line 90–92
if