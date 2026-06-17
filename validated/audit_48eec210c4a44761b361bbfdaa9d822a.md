### Title
Unbounded `while` Loop in `constructProviderCommitment` Enables DoS of `revealWithCallback` — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy::constructProviderCommitment()` contains an unbounded `while (numHashes > 0)` loop. The iteration count is determined by `req.numHashes`, which is stored at request time as `assignedSequenceNumber − currentCommitmentSequenceNumber`. When a provider leaves `maxNumHashes` at its default value of `0`, no cap is enforced at request time. An unprivileged user can make enough sequential requests — without the provider advancing their commitment — to produce a request whose stored `numHashes` is large enough to exhaust block gas when `revealWithCallback` is later called, permanently bricking that randomness request.

---

### Finding Description

In `requestHelper`, `req.numHashes` is computed and stored:

```solidity
req.numHashes = SafeCast.toUint32(
    assignedSequenceNumber -
        providerInfo.currentCommitmentSequenceNumber
);
if (
    providerInfo.maxNumHashes != 0 &&
    req.numHashes > providerInfo.maxNumHashes
) {
    revert EntropyErrors.LastRevealedTooOld();
}
``` [1](#0-0) 

The guard is skipped entirely when `providerInfo.maxNumHashes == 0`, which is the default for any provider that has not explicitly called `setMaxNumHashes`. The stored `numHashes` value is later consumed in `revealHelper`:

```solidity
bytes32 providerCommitment = constructProviderCommitment(
    req.numHashes,
    providerContribution
);
``` [2](#0-1) 

`constructProviderCommitment` iterates unconditionally:

```solidity
function constructProviderCommitment(
    uint64 numHashes,
    bytes32 revelation
) internal pure returns (bytes32 currentHash) {
    currentHash = revelation;
    while (numHashes > 0) {
        currentHash = keccak256(bytes.concat(currentHash));
        numHashes -= 1;
    }
}
``` [3](#0-2) 

Each iteration costs ~30 gas for `keccak256`. A block gas limit of ~30 M gas allows roughly 1 M iterations before OOG. Because `numHashes` is typed as `uint32` (max ≈ 4.3 B), the theoretical maximum far exceeds any block gas limit.

`revealWithCallback` is callable by anyone: [4](#0-3) 

Once a request is stored with a `numHashes` value that causes OOG inside `constructProviderCommitment`, neither the provider nor any third party can ever successfully call `revealWithCallback` for it. The request is permanently stuck.

---

### Impact Explanation

Any in-flight randomness request whose stored `numHashes` exceeds the gas budget of `constructProviderCommitment` can never be fulfilled. The user's callback is never invoked, and the request slot is never cleared. For providers that have not set `maxNumHashes`, this affects all requests made after a sufficiently long gap since the last `advanceProviderCommitment` call. Affected users permanently lose their randomness and any application logic depending on the callback is frozen.

---

### Likelihood Explanation

Any unprivileged user can call `requestV2()` repeatedly, paying only the provider fee per call. Each call increments `providerInfo.sequenceNumber` by 1 without advancing `currentCommitmentSequenceNumber`. Providers that do not proactively call `advanceProviderCommitment` — or that are simply slow to do so — are vulnerable. Because `maxNumHashes` defaults to `0` and its documentation is not prominent, many real-world providers are likely to leave it unset. The attacker's cost is bounded only by the provider's fee per request, which can be very low.

---

### Recommendation

1. **Enforce a hard cap on `numHashes` unconditionally**, not only when `maxNumHashes != 0`:
   ```solidity
   uint32 hardCap = providerInfo.maxNumHashes != 0
       ? providerInfo.maxNumHashes
       : DEFAULT_MAX_NUM_HASHES; // e.g. 1000
   if (req.numHashes > hardCap) {
       revert EntropyErrors.LastRevealedTooOld();
   }
   ```
2. **Document that providers must set `maxNumHashes`** to a value consistent with their callback gas budget, and enforce a non-zero default at registration time.
3. **Consider replacing the `while` loop** in `constructProviderCommitment` with a gas-checked variant that reverts cleanly rather than OOGing.

---

### Proof of Concept

1. Provider registers with default `maxNumHashes = 0` and a low `feeInWei`.
2. Attacker calls `requestV2(provider, gasLimit)` in a loop across many transactions, each time incrementing `providerInfo.sequenceNumber`. Provider does not call `advanceProviderCommitment`.
3. After N requests, attacker (or any user) makes one more request. This request is stored with `numHashes = N`.
4. Provider calls `revealWithCallback(provider, sequenceNumber, ...)` for this request.
5. Inside `revealHelper → constructProviderCommitment`, the loop runs N times. For N ≥ ~1 M, the transaction runs out of gas and reverts.
6. The request is permanently stuck: `numHashes` is immutably stored in the request struct, and no path exists to fulfill or cancel it. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L247-256)
```text
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L395-439)
```text
    function revealHelper(
        EntropyStructsV2.Request storage req,
        bytes32 userContribution,
        bytes32 providerContribution
    ) internal returns (bytes32 randomNumber, bytes32 blockHash) {
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();

        blockHash = bytes32(uint256(0));
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();

            blockHash = _blockHash;
        }

        randomNumber = combineRandomValues(
            userContribution,
            providerContribution,
            blockHash
        );

        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            req.provider
        ];
        if (providerInfo.currentCommitmentSequenceNumber < req.sequenceNumber) {
            providerInfo.currentCommitmentSequenceNumber = req.sequenceNumber;
            providerInfo.currentCommitment = providerContribution;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-548)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L987-996)
```text
    function constructProviderCommitment(
        uint64 numHashes,
        bytes32 revelation
    ) internal pure returns (bytes32 currentHash) {
        currentHash = revelation;
        while (numHashes > 0) {
            currentHash = keccak256(bytes.concat(currentHash));
            numHashes -= 1;
        }
    }
```
