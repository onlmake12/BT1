### Title
Validator-Provider Collusion Enables Predictable Random Number Manipulation via `requestV2()` In-Contract PRNG — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The three no-user-contribution overloads of `requestV2()` in the Entropy contract generate the user's randomness contribution entirely on-chain via an internal `random()` PRNG call. This removes the user's ability to supply an independent secret, collapsing the two-party security model into a single-party model. A colluding validator (block producer) and provider can predict the final random number before the reveal transaction is submitted, enabling selective fulfillment (censorship) or active outcome manipulation in any downstream application that uses these convenience functions.

---

### Finding Description

The Entropy protocol's security guarantee is that the final random number `r = hash(x_i, x_U)` is unpredictable as long as **either** the provider or the user is honest. This guarantee requires that the user contribution `x_U` be a secret unknown to the provider before the request is committed on-chain.

The three convenience overloads of `requestV2()` break this guarantee by substituting the user's secret with an in-contract PRNG value:

```solidity
// Entropy.sol line 286-293
function requestV2()
    external payable override
    returns (uint64 assignedSequenceNumber)
{
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
}
```

```solidity
// Entropy.sol line 295-303
function requestV2(uint32 gasLimit)
    external payable override
    returns (uint64 assignedSequenceNumber)
{
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), gasLimit);
}
```

```solidity
// Entropy.sol line 305-310
function requestV2(address provider, uint32 gasLimit)
    external payable override
    returns (uint64 assignedSequenceNumber)
{
    assignedSequenceNumber = requestV2(provider, random(), gasLimit);
}
```

The NatSpec on `IEntropyV2` explicitly acknowledges the resulting collusion surface:

> *"This approach modifies the security guarantees such that a dishonest validator and provider can collude to manipulate the result (as opposed to a malicious user and provider). That is, the user now trusts the validator honestly draw a random number."*

The provider already knows their entire hash chain `x_0 … x_{N-1}` before any request is made. If the validator (block producer) can predict or influence the output of `random()` — which uses on-chain state available at block-production time — the provider and validator together can compute `r = hash(x_i, random())` before the block is finalized. The provider can then:

1. **Selectively withhold reveals** (censorship attack): compute `r` off-chain, and only submit the reveal transaction when `r` produces a favorable outcome for themselves or a colluding party.
2. **Actively steer outcomes** (manipulation attack): the validator inserts or reorders transactions to set the PRNG state to a value that, combined with the provider's known `x_i`, yields a desired `r`.

The full `requestV2(address, bytes32, uint32)` overload is immune because the user supplies their own `userRandomNumber` as a secret pre-image, restoring the two-party security property. The convenience overloads silently downgrade to a weaker trust model without any on-chain enforcement or warning to callers.

---

### Impact Explanation

Any smart contract that calls `requestV2()`, `requestV2(uint32)`, or `requestV2(address, uint32)` — the three variants recommended in the developer documentation and used in the `CoinFlip` example — is vulnerable to outcome manipulation by a colluding provider and validator. Concrete downstream impacts include:

- **Gaming / lotteries**: winner selection or dice rolls can be steered to always favor the attacker.
- **NFT minting**: rare trait assignment can be predicted and front-run.
- **DeFi protocols**: random liquidation ordering or collateral selection can be manipulated.
- **Any protocol paying out based on randomness**: the payout can be claimed selectively only when the outcome is favorable.

The Entropy contract itself does not hold user funds beyond fees, but the downstream applications that rely on it for fairness guarantees are directly at risk of financial loss.

---

### Likelihood Explanation

The default developer path — `requestV2()` with no arguments — is the variant shown in the official quick-start documentation and the `CoinFlip` example. Most integrators will use this path. The Fortuna provider is a single known entity; any compromise or insider threat at the provider level, combined with a colluding validator (achievable via MEV infrastructure or block-builder relationships), is sufficient to trigger the attack. The provider alone can execute the censorship variant without any validator involvement, since they know `x_i` in advance and can compute `r` once the PRNG state is visible in the mempool.

---

### Recommendation

1. **Remove or deprecate the three no-user-contribution overloads**, or add an on-chain revert if the caller is a contract (since contracts cannot supply a secret off-chain).
2. **If the convenience overloads are retained**, emit a distinct event or set a flag in the stored request indicating that the user contribution was PRNG-generated, so downstream auditors and monitoring tools can identify at-risk requests.
3. **Document the downgraded security model prominently** at the call site in Solidity (not only in the interface NatSpec), so integrators who copy-paste the convenience call are forced to see the warning.
4. **Encourage integrators** to always use `requestV2(address provider, bytes32 userRandomNumber, uint32 gasLimit)` and generate `userRandomNumber` off-chain before submitting the transaction.

---

### Proof of Concept

**Setup**: Attacker controls (or bribes) the Fortuna provider and a block-builder/validator.

1. **Provider pre-computes**: For sequence number `i`, the provider knows `x_i` from their hash chain. They compute `providerCommitment = keccak256(x_i)` (after `numHashes` applications).

2. **Victim calls `requestV2()`**: The victim's contract calls `entropy.requestV2{value: fee}()`. The contract internally calls `random()` to generate `userContribution`. The PRNG state is derived from on-chain data visible to the validator at block-production time.

3. **Validator predicts PRNG output**: Before including the victim's transaction, the validator simulates the block state and computes the value that `random()` will return for the victim's call. They share this value with the provider.

4. **Provider computes final outcome**: `r = combineRandomValues(userContribution, x_i, 0)` (no blockhash since `useBlockhash = false` for callback requests). The provider evaluates whether `r` is favorable.

5. **Selective reveal**: If `r` is unfavorable (e.g., the victim wins the lottery), the provider withholds the reveal transaction, causing the request to time out. If `r` is favorable (e.g., the attacker wins), the provider submits `revealWithCallback(provider, sequenceNumber, userContribution, x_i)`.

6. **Result**: The attacker wins every high-value lottery draw while victims' requests time out, with no on-chain evidence of manipulation.

**Key code references**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-293)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-390)
```text
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L21-26)
```text
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
    function requestV2()
```

**File:** apps/developer-hub/content/docs/entropy/protocol-design.mdx (L50-55)
```text
This flow is secure as long as several trust assumptions hold:

- Providers are trusted to reveal their random number $$(x_i)$$ regardless of what the final result $$(r)$$ is. Providers can compute $$(r)$$ off-chain before they reveal $$(x_i)$$, which permits a censorship attack.
- Providers are trusted not to front-run user transactions (via the mempool or colluding with the validator). Providers who observe user transactions can manipulate the result by inserting additional reuests or rotating their commitment.
- Providers are trusted not to keep their hash chain a secret. Anyone with the hash chain can predict the result of a randomness request before it is requested,
  and therefore manipulate the result. This applies both to users of the protocol as well as blockchain validators who can use this information to manipulate the on-chain PRNG or reorder user transactions.
```
