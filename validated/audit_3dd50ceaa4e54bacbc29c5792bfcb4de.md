### Title
No-Slippage-Guard on `delegate` Allows Staker to Be Front-Run Into Over-Cap Position — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/types/integrity-pool.ts`)

---

### Summary

The OIS `delegate` instruction accepts only an `amount` argument with no minimum-remaining-capacity guard. A staker who observes a pool with available capacity can be front-run by another staker who fills the pool to its soft cap. The victim's tokens land over the cap: they earn zero rewards but remain fully exposed to slashing. No on-chain protection prevents this outcome.

---

### Finding Description

Every publisher pool has a soft cap `C_p` computed from the publisher's symbol set. Rewards are `R_p = y · min(S_p, C_p)`, so stake above `C_p` earns nothing. The OIS documentation explicitly states: *"Staking into the pool can exceed the soft cap. The excess amount is subject to the penalty if the assigned publisher's data is inaccurate."*

The `delegate` instruction's argument list contains only `amount`:

```json
"args": [
  { "name": "amount", "type": "u64" }
],
"name": "delegate"
``` [1](#0-0) 

There is no `min_cap_remaining` or equivalent slippage parameter. The frontend computes `remainingPool = poolCapacity - poolUtilization - poolUtilizationDelta` to display available space: [2](#0-1) 

but this check is purely off-chain and is not enforced in the on-chain instruction. Between the time a user reads the remaining capacity and the time their transaction lands, another user's `delegate` transaction can fill the pool.

---

### Impact Explanation

A staker who intended to stake within the cap ends up over the cap:

- **Zero rewards** — their stake does not contribute to `min(S_p, C_p)`, so they receive no yield (impact is latent while `y = 0` per OP-PIP-103, but becomes active if governance raises `y`).
- **Full slashing exposure** — over-cap stake is still slashed pro-rata if the publisher misbehaves, creating an asymmetric risk: no upside, full downside.
- **Cooldown lock** — unstaking requires waiting through the cooldown period (end of next epoch), during which the slashing window remains open.

The combination of zero reward eligibility and retained slashing exposure is the direct analog to the original report's "lock tokens for 4 years with no bonus." [3](#0-2) 

---

### Likelihood Explanation

- Any two unprivileged stakers targeting the same near-full pool in the same block trigger the condition.
- Pool capacity is publicly visible; rational stakers will race to fill pools with high-quality publishers.
- Solana's transaction ordering is not user-controllable, so the victim cannot guarantee their transaction lands first.
- No privileged access is required; the entry path is the standard `delegate` instruction callable by any token holder. [4](#0-3) 

---

### Recommendation

Add a `min_cap_remaining: u64` argument to the `delegate` instruction. Before creating the position, assert:

```
require!(
    pool_cap - current_pool_utilization >= min_cap_remaining,
    ErrorCode::InsufficientCapacity
);
```

This mirrors the standard slippage-protection pattern and lets callers express "only stake me if at least X capacity remains within the cap."

---

### Proof of Concept

1. Pool P has `poolCapacity = 1000`, `poolUtilization = 800`. Remaining = 200.
2. Alice reads remaining = 200 and submits `delegate(amount=200)` to fill the pool exactly.
3. Bob also reads remaining = 200 and submits `delegate(amount=200)` in the same block.
4. Alice's transaction lands first; pool is now at 1000/1000.
5. Bob's transaction succeeds (no on-chain guard rejects it); Bob's 200 tokens are staked over the cap.
6. Bob earns 0 rewards (`min(1200, 1000) - min(1000, 1000) = 0` attributable to Bob's stake).
7. If publisher P is slashed at rate `w`, Bob loses `w * 200` tokens with no compensating yield. [5](#0-4)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L536-543)
```json
      "args": [
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [90, 147, 75, 178, 85, 88, 4, 137],
      "name": "delegate"
```

**File:** apps/staking/src/components/OracleIntegrityStaking/index.tsx (L1052-1065)
```typescript
    const remainingPoolA =
      a.poolCapacity - a.poolUtilization - a.poolUtilizationDelta;
    const remainingPoolB =
      b.poolCapacity - b.poolUtilization - b.poolUtilizationDelta;
    if (remainingPoolA <= 0n && remainingPoolB <= 0n) {
      return 0;
    } else if (remainingPoolA <= 0n && remainingPoolB > 0n) {
      return 1;
    } else if (remainingPoolB <= 0n && remainingPoolA > 0n) {
      return -1;
    } else {
      return (reverse ? -1 : 1) * Number(remainingPoolB - remainingPoolA);
    }
  }
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L34-48)
```text
The reward $R_p$ distributed to each pool is calculated as follows:

$$
\large{\bold{R_p} = y \cdot \min(S_p, C_p)}
$$

Where:

- $y$ is the cap to the rate of rewards for any pool (currently $y = 0$)
- $S_p$ be the stake assigned to the publisher p pool, made of self-staked amount $S^{p}_{p}$ and delegated stake $S^{d}_{p}$, or $S_{p} = S^{p}_{p} + S^{d}_{p}$.
- $C_p$ be the stake cap for the pool assigned to publisher p.

<Callout type="info">
  Since $y = 0$, no rewards are currently distributed to any pool.
</Callout>
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L425-455)
```typescript
    },
    {
      name: "delegate";
      discriminator: [90, 147, 75, 178, 85, 88, 4, 137];
      accounts: [
        {
          name: "owner";
          writable: true;
          signer: true;
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked against data in the pool_data",
          ];
```

**File:** governance/pyth_staking_sdk/src/utils/apy.ts (L44-62)
```typescript
  const delegatorPoolCapacity = poolCapacity - eligibleSelfStake;
  const eligibleStake =
    // This eslint rule incorrectly tries to use Math.min() here instead, which
    // casts down to number
    // eslint-disable-next-line unicorn/prefer-math-min-max
    delegatorPoolUtilization > delegatorPoolCapacity
      ? delegatorPoolCapacity
      : delegatorPoolUtilization;

  if (poolUtilization === selfStake) {
    return (
      (selfStake >= poolCapacity ? 0 : apyPercentage) * delegatorPercentage
    );
  }

  return (
    (apyPercentage * delegatorPercentage * Number(eligibleStake)) /
    Number(delegatorPoolUtilization)
  );
```
