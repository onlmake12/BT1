Looking at the CKB codebase for arithmetic formula errors analogous to the `scalingFactor * e^x - 1` vs `scalingFactor * (e^x - 1)` operator-precedence bug, I systematically reviewed all critical numeric formulas across the DAO, reward, and fee subsystems.

**Formulas reviewed:**

1. **`calculate_maximum_withdraw`** (`util/dao/src