Looking at the external report's vulnerability class — **uncapped resource accumulation feeding a downstream operation that fails when a hard limit is exceeded** — I need to find a CKB analog where an attacker-controlled input accumulates without a cap and causes a downstream failure.

Let me trace the relevant code paths.