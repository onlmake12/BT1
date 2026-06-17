### Title
Uncapped External Callback in `revealWithCallback` Legacy Path Enables Fortuna Keeper Gas Drain - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains a legacy execution path that makes an **uncapped** external call to the requester's `_entropyCallback`. When a provider has `defaultGasLimit == 0`, or when a request is in the `CALLBACK_FAILED` recovery state, no gas limit is placed on the callback. The Fortuna keeper (provider/relayer) estimates gas before submitting, but a malicious requester can change modifiable state between the keeper's gas estimation and actual execution, causing the keeper's transaction to revert and forcing it to pay for wasted gas on each failed attempt.

---

### Finding Description

`revealWithCallback` branches on two conditions:

```solidity
if (
    req.gasLimit10k != 0 &&
    req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
) {
    // Safe path: excessivelySafeCall with explicit gas cap
    (success, ret) = req.requester.excessivelySafeCall(
        uint256(req.gasLimit10k) * TEN_THOUSAND, ...
    );
} else {
    // UNSAFE path: uncapped call, no gas limit
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber, provider, randomNumber
    );
}
```

The `else` branch is reached in two production-reachable scenarios:

1. **`gasLimit10k == 0`**: When a provider has `defaultGasLimit == 0` (the legacy opt-out), `requestHelper` sets `req.gasLimit10k = 0` for every request from that provider.
2. **`callbackStatus == CALLBACK_FAILED`**: The recovery path for previously-failed callbacks also falls into the `else` branch, making an uncapped call.

In both cases, `IEntropyConsumer(callAddress)._entropyCallback(...)` is called with **no gas stipend**, so the EVM default of `gasleft() * 63 / 64` is forwarded to the untrusted requester contract.

The Fortuna keeper (`apps/fortuna`) calls `estimate_gas()` before submitting, then sets the transaction gas limit to that estimate:

```rust
let gas: U256 = call.estimate_gas().await...?;
let mut transaction = call.tx.clone();
transaction.set_gas(gas);
```

A malicious requester can:
1. Deploy a callback that reads a modifiable storage variable and uses cheap gas when the variable is in state A.
2. Submit a request to a provider with `defaultGasLimit == 0`.
3. Wait for the Fortuna keeper to call `estimate_gas()` — the estimate is low (state A).
4. Flip the storage variable to state B (expensive gas path) before the keeper's transaction lands.
5. The keeper's transaction reverts out-of-gas; the keeper pays for gas up to the estimate.
6. The keeper retries with a new (higher) estimate; the attacker flips back to state A.
7. The keeper's retry succeeds, but the attacker has caused one full wasted-gas payment per cycle.

This cycle can be repeated indefinitely, draining the keeper's ETH balance.

---

### Impact Explanation

The Fortuna keeper is the Pyth-operated provider/relayer that pays gas to fulfill entropy requests. Each forced revert costs the keeper the full estimated gas amount (which can be substantial for complex callbacks). Repeated cycling causes unbounded financial loss to the keeper with no corresponding cost to the attacker (who only pays for cheap state-flip transactions). At scale, this can render the Fortuna keeper insolvent or force it to stop serving requests, breaking liveness of the Entropy protocol for all users.

---

### Likelihood Explanation

- **Providers with `defaultGasLimit == 0`** are explicitly supported by the contract (the legacy path). Any provider that has not called `setDefaultGasLimit` with a nonzero value is vulnerable.
- The `CALLBACK_FAILED` recovery path is reachable by any request that previously failed its callback.
- The attacker only needs to be the requester (an unprivileged role — anyone can call `requestWithCallback`).
- The state-flip between estimation and execution is a standard front-running technique requiring no special access.
- The Fortuna keeper's retry-with-backoff logic (`submit_tx_with_backoff`, up to 5 minutes of retries) amplifies the damage per attack cycle.

---

### Recommendation

1. **Apply a gas cap in the `else` branch** analogous to the `excessivelySafeCall` used in the `if` branch. For legacy requests (`gasLimit10k == 0`), use the provider's `defaultGasLimit` as a fallback cap, or enforce a hard maximum.
2. **Deprecate the `gasLimit10k == 0` path** by requiring all providers to set a nonzero `defaultGasLimit` before accepting new requests.
3. **In the Fortuna keeper**, add a configurable `gas_limit_cap` parameter. Before submitting, compare the estimate against the cap and refuse to submit if the estimate exceeds it, preventing the keeper from being tricked into paying for arbitrarily expensive callbacks.

---

### Proof of Concept

**Setup:**
- Provider registers with `defaultGasLimit = 0` (or attacker uses an existing legacy provider).
- Attacker deploys:

```solidity
contract MaliciousConsumer is IEntropyConsumer {
    bool public expensive;
    function setExpensive(bool v) external { expensive = v; }

    function _entropyCallback(uint64, address, bytes32) external override {
        if (expensive) {
            // burn gas: e.g., write to many storage slots
            for (uint i = 0; i < 500; i++) {
                assembly { sstore(i, 1) }
            }
        }
    }
    function getEntropy() internal view override returns (address) { ... }
}
```

**Attack cycle:**
1. Attacker calls `requestWithCallback(provider, userRandom)` — `gasLimit10k` is stored as `0`.
2. Fortuna keeper observes the event, calls `estimate_gas()` with `expensive = false` → estimate = ~50k gas.
3. Attacker calls `setExpensive(true)`.
4. Keeper submits `revealWithCallback` with gas = 50k → transaction reverts (out of gas in uncapped callback). Keeper loses ~50k gas worth of ETH.
5. Keeper retries, re-estimates with `expensive = true` → estimate = ~500k gas.
6. Attacker calls `setExpensive(false)`.
7. Keeper submits with gas = 500k → succeeds, but keeper overpaid ~450k gas.
8. Repeat from step 1 with a new request.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-283)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
            // This check does two important things:
            // 1. Providers have a minimum fee set for their defaultGasLimit. If users request less gas than that,
            //    they still pay for the full gas limit. So we may as well give them the full limit here.
            // 2. If a provider has a defaultGasLimit != 0, we need to ensure that all requests have a >0 gas limit
            //    so that we opt-in to the new callback failure state flow.
            req.gasLimit10k = roundTo10kGas(
                callbackGasLimit < providerInfo.defaultGasLimit
                    ? providerInfo.defaultGasLimit
                    : callbackGasLimit
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```

**File:** apps/fortuna/src/eth_utils/utils.rs (L270-276)
```rust
    let gas: U256 = call.estimate_gas().await.map_err(|e| {
        backoff::Error::transient(SubmitTxError::GasUsageEstimateError(call.tx.clone(), e))
    })?;

    let mut transaction = call.tx.clone();
    // Setting the gas here avoids a redundant call to estimate_gas within the Provider's fill_transaction method.
    transaction.set_gas(gas);
```

**File:** apps/fortuna/src/keeper/process_event.rs (L141-186)
```rust
    let contract_call = contract.reveal_with_callback(
        event.provider_address,
        event.sequence_number,
        event.user_random_number,
        provider_revelation,
    );
    let error_mapper = |num_retries, e| {
        if let backoff::Error::Transient {
            err: SubmitTxError::GasUsageEstimateError(tx, ContractError::Revert(revert)),
            ..
        } = &e
        {
            if let Ok(PythRandomErrorsErrors::NoSuchRequest(_)) =
                PythRandomErrorsErrors::decode(revert)
            {
                let err = SubmitTxError::GasUsageEstimateError(
                    tx.clone(),
                    ContractError::Revert(revert.clone()),
                );
                // Slow down the retries if the request is not found.
                // This probably means that the request is already fulfilled via another process.
                // After 5 retries, we return the error permanently.
                if num_retries >= 5 {
                    return backoff::Error::Permanent(err);
                }
                let retry_after_seconds = match num_retries {
                    0 => 5,
                    1 => 10,
                    _ => 60,
                };
                return backoff::Error::Transient {
                    err,
                    retry_after: Some(Duration::from_secs(retry_after_seconds)),
                };
            }
        }
        e
    };

    let success = submit_tx_with_backoff(
        contract.client(),
        contract_call,
        escalation_policy,
        Some(error_mapper),
    )
    .await;
```
