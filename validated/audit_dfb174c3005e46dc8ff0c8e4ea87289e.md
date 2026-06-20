Looking at the vulnerability class — a sentinel "max" value passed as an amount that is supposed to mean "use the current balance," but the code forwards it downstream without replacing it with the actual balance — I need to find an analog in CKB's production code.

Let me examine the most relevant candidates I found: