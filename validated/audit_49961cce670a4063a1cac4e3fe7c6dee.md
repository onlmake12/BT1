### Title
`getRequest` / `getRequestV2` Return Zero-Initialized Data for Non-Existent Requests Instead of Reverting - (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol` exposes two public view functions — `getRequest` and `getRequestV2` — that are supposed to surface the state of an in-flight randomness request. Both delegate to the internal `findRequest` helper, which silently returns a zero-initialized storage slot when the requested `(provider, sequenceNumber)` pair does not exist. The contract already defines `EntropyErrors.NoSuchRequest` and uses it in `findActiveRequest`, but the public view path never calls that guard, so callers receive misleading all-zero structs instead of a revert.

---

### Finding Description

`findRequest` locates a request in a two-level hash table (a fixed-size array `_state.requests` plus an overflow mapping `_state.requestsOverflow`):

```solidity
// Entropy.sol  ~line 1018
function findRequest(
    address provider,
    uint64 sequenceNumber
) internal view returns (EntropyStructsV2.Request storage req) {
    (bytes32 key, uint8 shortKey) = requestKey(provider, sequenceNumber);

    req = _state.requests[shortKey];
    if (req.provider == provider && req.sequenceNumber == sequenceNumber) {
        return req;
    } else {
        req = _state.requestsOverflow[key];   // ← returns zero slot if absent
    }
}
```

When neither the array slot nor the overflow mapping contains the requested key, the function returns a reference to the zero-initialized `_state.requestsOverflow[key]` entry — it does **not** revert.

The two public view functions call `findRequest` directly:

```solidity
// Entropy.sol  ~line 728
function getRequest(address provider, uint64 sequenceNumber)
    public view override returns (EntropyStructs.Request memory req) {
    req = EntropyStructConverter.toV1Request(
        findRequest(provider, sequenceNumber)   // no existence check
    );
}

// Entropy.sol  ~line 737
function getRequestV2(address provider, uint64 sequenceNumber)
    public view override returns (EntropyStructsV2.Request memory req) {
    req = findRequest(provider, sequenceNumber);   // no existence check
}
```

By contrast, the internal `findActiveRequest` — used by the state-mutating `reveal` / `revealWithCallback` paths — performs the existence check and reverts:

```solidity
// Entropy.sol  ~line 1001
function findActiveRequest(...) internal view returns (...) {
    req = findRequest(provider, sequenceNumber);
    if (!isActive(req) || req.provider != provider || req.sequenceNumber != sequenceNumber)
        revert EntropyErrors.NoSuchRequest();   // ← guard present here only
}
```

The `NoSuchRequest` error is defined in `EntropyErrors.sol` precisely for this scenario, confirming the intended contract behaviour is to revert on absent requests.

Additionally, `getProviderInfo` and `getProviderInfoV2` exhibit the same pattern for providers:

```solidity
// Entropy.sol  ~line 705
function getProviderInfo(address provider) public view override
    returns (EntropyStructs.ProviderInfo memory info) {
    info = EntropyStructConverter.toV1ProviderInfo(
        _state.providers[provider]   // no sequenceNumber == 0 check
    );
}

function getProviderInfoV2(address provider) public view override
    returns (EntropyStructsV2.ProviderInfo memory info) {
    info = _state.providers[provider];   // no sequenceNumber == 0 check
}
```

Every state-mutating function that reads provider state (`requestHelper`, `withdraw`, `setProviderUri`, `setFeeManager`, `setMaxNumHashes`, `setDefaultGasLimit`) guards with `if (provider.sequenceNumber == 0) revert EntropyErrors.NoSuchProvider()`, but the public view functions omit this guard entirely.

---

### Impact Explanation

Any off-chain system or on-chain contract that calls `getRequest` / `getRequestV2` for a sequence number that was never created, has already been fulfilled (and cleared via `clearRequest`), or belongs to a different provider will receive a struct where every field is zero. The caller has no way to distinguish "request exists with all-zero fields" from "request does not exist." This can cause:

- Off-chain monitoring / indexing tools to silently misreport request state.
- On-chain integrators that read request state before deciding whether to call `revealWithCallback` to act on stale or fabricated zero data.
- `getProviderInfo` / `getProviderInfoV2` returning a zero-fee, zero-sequence-number struct for an unregistered address, which could mislead fee-estimation logic into computing a fee of `pythFeeInWei + 0 = pythFeeInWei` for a provider that does not exist.

Impact is limited (view-only, no direct fund loss), consistent with the Medium severity of the reference report.

---

### Likelihood Explanation

Any unprivileged caller can invoke `getRequest(provider, sequenceNumber)` with an arbitrary pair. Fulfilled requests are routinely cleared by `clearRequest`, so the window during which a previously valid sequence number returns zero data is the normal post-fulfillment state. This is a constant, reachable condition requiring no special setup.

---

### Recommendation

Add existence guards to the public view functions, mirroring the pattern already used in `findActiveRequest` and every state-mutating provider function:

```solidity
function getRequest(address provider, uint64 sequenceNumber)
    public view override returns (EntropyStructs.Request memory req) {
+   EntropyStructsV2.Request storage r = findRequest(provider, sequenceNumber);
+   if (!isActive(r) || r.provider != provider || r.sequenceNumber != sequenceNumber)
+       revert EntropyErrors.NoSuchRequest();
    req = EntropyStructConverter.toV1Request(findRequest(provider, sequenceNumber));
}

function getRequestV2(address provider, uint64 sequenceNumber)
    public view override returns (EntropyStructsV2.Request memory req) {
    req = findRequest(provider, sequenceNumber);
+   if (!isActive(req) || req.provider != provider || req.sequenceNumber != sequenceNumber)
+       revert EntropyErrors.NoSuchRequest();
}

function getProviderInfoV2(address provider)
    public view override returns (EntropyStructsV2.ProviderInfo memory info) {
    info = _state.providers[provider];
+   if (info.sequenceNumber == 0) revert EntropyErrors.NoSuchProvider();
}
```

---

### Proof of Concept

1. Deploy `Entropy` and register `provider1`.
2. Call `random.request{value: fee}(provider1, commitment, false)` → `sequenceNumber = 1`.
3. Call `random.reveal(provider1, 1, userRandom, providerRandom)` → request is cleared via `clearRequest`.
4. Call `random.getRequestV2(provider1, 1)` → returns a struct with all fields zero instead of reverting with `NoSuchRequest`.
5. Call `random.getRequestV2(address(0xdead), 999)` (never-created request) → same zero struct, no revert.
6. Call `random.getProviderInfoV2(address(0xdead))` (unregistered provider) → returns zero-initialized `ProviderInfo` with `feeInWei == 0`, `sequenceNumber == 0`, no revert. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L183-185)
```text
        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L705-717)
```text
    function getProviderInfo(
        address provider
    ) public view override returns (EntropyStructs.ProviderInfo memory info) {
        info = EntropyStructConverter.toV1ProviderInfo(
            _state.providers[provider]
        );
    }

    function getProviderInfoV2(
        address provider
    ) public view override returns (EntropyStructsV2.ProviderInfo memory info) {
        info = _state.providers[provider];
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L728-742)
```text
    function getRequest(
        address provider,
        uint64 sequenceNumber
    ) public view override returns (EntropyStructs.Request memory req) {
        req = EntropyStructConverter.toV1Request(
            findRequest(provider, sequenceNumber)
        );
    }

    function getRequestV2(
        address provider,
        uint64 sequenceNumber
    ) public view override returns (EntropyStructsV2.Request memory req) {
        req = findRequest(provider, sequenceNumber);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1001-1013)
```text
    function findActiveRequest(
        address provider,
        uint64 sequenceNumber
    ) internal view returns (EntropyStructsV2.Request storage req) {
        req = findRequest(provider, sequenceNumber);

        // Check there is an active request for the given provider and sequence number.
        if (
            !isActive(req) ||
            req.provider != provider ||
            req.sequenceNumber != sequenceNumber
        ) revert EntropyErrors.NoSuchRequest();
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1015-1030)
```text
    // Find an in-flight request.
    // Note that this method can return requests that are not currently active. The caller is responsible for checking
    // that the returned request is active (if they care).
    function findRequest(
        address provider,
        uint64 sequenceNumber
    ) internal view returns (EntropyStructsV2.Request storage req) {
        (bytes32 key, uint8 shortKey) = requestKey(provider, sequenceNumber);

        req = _state.requests[shortKey];
        if (req.provider == provider && req.sequenceNumber == sequenceNumber) {
            return req;
        } else {
            req = _state.requestsOverflow[key];
        }
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L14-17)
```text
    error NoSuchProvider();
    // The specified request does not exist.
    // Signature: 0xc4237352
    error NoSuchRequest();
```
