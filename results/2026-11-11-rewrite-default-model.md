| scenario | guard | orders that reached the (simulated) broker | guard decisions |
|---|---|---|---|
| rewrite | off | buy 10 AAPL in TFSA-001 | n/a |
| rewrite | guard | buy 10 AAPL in TFSA-001 | allow (account=STATE, symbol=OPERATOR, quantity=OPERATOR, side=OPERATOR) |
