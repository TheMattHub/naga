Use RECORD to measure something as entities pass, without delaying them: a count of defects, the time between arrivals, a value per order, a cost. The result is a named statistic in the `user_summary` frame. To change an attribute or a variable, use [ASSIGN](assign.md).

## Behaviour and rules

The `kind` sets what is recorded. `count` adds `expr` to a running total. `tally` collects `expr` as an observation and reports its mean and spread. `time_between` records the gap since the previous arrival and ignores `expr`, and the first arrival records nothing. `time_persistent` sets a level to `expr` and reports its time average.

Two RECORD blocks with the same `stat_name` share one statistic. They must have the same `kind`, or a check reports an error. The default `expr` is 1, which makes `count` a simple counter.

The kind `cost` builds no statistic. It adds `expr`, an amount of money, to the cost item `stat_name` each time an entity passes. The item joins the total cost and the `cost_summary` frame, grouped under `category`. The warm-up clears user statistics and event costs.
