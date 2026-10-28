Use these functions for ordinary arithmetic in any expression: a rounded quantity, a bounded value, a conditional, a safe division. `IF(condition, a, b)` is the way to branch inside an expression. `ZIDZ` and `XIDZ` avoid a division by zero. `MODULO` is the remainder with the sign of the first argument.

## Behaviour and rules

The Python-style functions `abs`, `ceil`, `exp`, `float`, `floor`, `int`, `log`, `log10`, `max`, `min`, `round` and `sqrt` are lower case. Their upper-case spellings do not exist, except `SQRT`. `IF` is upper case and takes exactly three arguments, so write `IF(x > 0, 1, 2)`. The Python form `a if c else b` also works.

`ZIDZ(a, b)` returns 0 when `b` is 0, and `XIDZ(a, b, x)` returns `x` instead. With two arguments, `XIDZ` behaves like `ZIDZ`. `MODULO(-10, 3)` is -1, not 2.
