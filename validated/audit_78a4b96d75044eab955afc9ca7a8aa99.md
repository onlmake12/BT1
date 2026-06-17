### Title
Unprivileged Caller Can Hijack Any Publisher's Stake Account Association — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `setPublisherStakeAccount` instruction in the Integrity Pool program accepts an arbitrary `signer` and a separate `publisher` account that is **not required to be a signer**. Any unprivileged user can call this instruction with any publisher's pubkey and redirect (or remove) that publisher's associated stake account, disrupting or stealing the publisher's self-staking rewards.

---

### Finding Description

The `setPublisherStakeAccount` instruction's account layout is:

```
signer          — isSigner: true  (any address)
publisher       — isSigner: false (CHECK: only verified to exist in pool_data)
pool_data       — writable
pool_config     — PDA
newStakeAccountPositionsOption    — optional
currentStakeAccountPositionsOption — optional
``` [1](#0-0) 

The `signer` field carries no `relations` constraint tying it to `pool_config`, `pool_data`, or the `publisher` account. The `publisher` account is explicitly **not** a signer — the only check noted in the IDL is `"CHECK : The publisher will be checked against data in the pool_data"`, meaning the program verifies the publisher is registered, but does **not** verify that `signer == publisher`. [2](#0-1) 

The TypeScript client confirms the same structure — `signer` is the wallet, `publisher` is a separate pubkey argument, and there is no constraint enforcing `signer.key == publisher`: [3](#0-2) 

This is directly analogous to the OmoRouter bug: a function that associates a resource (stake account) with an identity (publisher) without verifying the caller controls that identity.

---

### Impact Explanation

An attacker can:

1. Call `setPublisherStakeAccount` with a victim publisher's pubkey and `newStakeAccountPositionsOption = attacker_stake_account`.
2. The pool data now maps the victim publisher to the attacker's stake account.
3. Self-staking rewards that the integrity pool credits to the publisher's stake account are redirected to the attacker's account.
4. Alternatively, the attacker can pass `newStakeAccountPositionsOption = None` to **remove** the publisher's stake account association entirely, denying the publisher their self-staking rewards.

**Impact: Medium–High** — direct theft or denial of staking rewards for any registered publisher.

---

### Likelihood Explanation

**Likelihood: Medium** — The function is permissionlessly callable by any on-chain transaction sender. No privileged role, leaked key, or oracle manipulation is required. All inputs (publisher pubkey, current stake account address) are publicly readable from on-chain state. The attacker only needs to submit a valid transaction.

---

### Recommendation

Add a constraint requiring `signer == publisher` (i.e., the publisher must co-sign the transaction), or add a `relations` constraint on `signer` that ties it to the publisher's registered key in `pool_data`. In Anchor this would be expressed as a `has_one` or `constraint` on the `signer` account:

```rust
#[account(
    constraint = signer.key() == publisher.key() @ IntegrityPoolError::Unauthorized
)]
pub signer: Signer<'info>,
```

---

### Proof of Concept

1. Observe that publisher `P` has stake account `S_P` registered in `pool_data`.
2. Attacker `A` creates their own stake account `S_A`.
3. Attacker calls `setPublisherStakeAccount` with:
   - `signer` = `A` (attacker's wallet, valid signer)
   - `publisher` = `P` (victim publisher, not required to sign)
   - `currentStakeAccountPositionsOption` = `S_P` (read from public chain state)
   - `newStakeAccountPositionsOption` = `S_A` (attacker's stake account)
4. The instruction succeeds because `signer` is a valid signer and `publisher` is a registered publisher — no check that `signer == publisher` exists.
5. `pool_data` now maps publisher `P` → stake account `S_A`. Future reward distributions for publisher `P`'s self-stake accrue to `S_A`, controlled by attacker `A`. [4](#0-3)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1-51)
```json
{
  "accounts": [
    {
      "discriminator": [203, 185, 161, 226, 129, 251, 132, 155],
      "name": "DelegationRecord"
    },
    {
      "discriminator": [149, 8, 156, 202, 160, 252, 176, 217],
      "name": "GlobalConfig"
    },
    {
      "discriminator": [26, 108, 14, 123, 116, 230, 129, 43],
      "name": "PoolConfig"
    },
    {
      "discriminator": [155, 28, 220, 37, 221, 242, 70, 167],
      "name": "PoolData"
    },
    {
      "discriminator": [85, 195, 241, 79, 124, 192, 79, 11],
      "name": "PositionData"
    },
    {
      "discriminator": [5, 87, 155, 44, 121, 90, 35, 134],
      "name": "PublisherCaps"
    },
    {
      "discriminator": [60, 32, 32, 44, 93, 234, 234, 89],
      "name": "SlashEvent"
    },
    {
      "discriminator": [157, 23, 139, 117, 181, 44, 197, 130],
      "name": "TargetMetadata"
    }
  ],
  "address": "pyti8TM4zRVBjmarcgAPmTNNAXYKJv7WVHrkrm6woLN",
  "errors": [
    {
      "code": 6000,
      "name": "PublisherNotFound"
    },
    {
      "code": 6001,
      "name": "PublisherOrRewardAuthorityNeedsToSign"
    },
    {
      "code": 6002,
      "name": "StakeAccountOwnerNeedsToSign"
    },
    {
      "code": 6003,
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L781-820)
```typescript
      name: "setPublisherStakeAccount";
      discriminator: [99, 46, 72, 132, 100, 235, 211, 117];
      accounts: [
        {
          name: "signer";
          signer: true;
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked against data in the pool_data",
          ];
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
          name: "newStakeAccountPositionsOption";
          optional: true;
        },
        {
          name: "currentStakeAccountPositionsOption";
          optional: true;
        },
      ];
      args: [];
    },
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L827-844)
```typescript
  async setPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey | undefined,
  ) {
    const instruction = await this.integrityPoolProgram.methods
      .setPublisherStakeAccount()
      .accounts({
        currentStakeAccountPositionsOption: stakeAccountPositions,
        // eslint-disable-next-line unicorn/no-null
        newStakeAccountPositionsOption: newStakeAccountPositions ?? null,
        publisher,
      })
      .instruction();

    await sendTransaction([instruction], this.connection, this.wallet);
    return;
  }
```

**File:** governance/xc_admin/packages/xc_admin_common/src/multisig_transaction/idl/integrity-pool.json (L1-52)
```json
{
  "instructions": [
    {
      "accounts": [
        {
          "isMut": false,
          "isSigner": true,
          "name": "signer"
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "isMut": false,
          "isSigner": false,
          "name": "publisher"
        },
        {
          "isMut": true,
          "isSigner": false,
          "name": "poolData"
        },
        {
          "isMut": false,
          "isSigner": false,
          "name": "poolConfig",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "type": "string",
                "value": "pool_config"
              }
            ]
          }
        },
        {
          "isMut": false,
          "isOptional": true,
          "isSigner": false,
          "name": "newStakeAccountPositionsOption"
        },
        {
          "isMut": false,
          "isOptional": true,
          "isSigner": false,
          "name": "currentStakeAccountPositionsOption"
        }
      ],
      "args": [],
      "name": "setPublisherStakeAccount"
    },
```
