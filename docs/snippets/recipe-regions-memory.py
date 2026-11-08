from nagasim import build as nb

# Three regions sell 10, 20 and 30 a day. SUM, PROD, VMAX and VMIN fold the region vector; INITIAL
# keeps day 0's price, SAMPLE_IF_TRUE keeps the stock read at the last stock-take (every 7 days),
# DELAY reads a 4-day shipping pipeline, ACTIVE_INITIAL gives a forecast of 25 while the initial
# values are set up (54 once the run moves it), and TABLE_EXTRAPOLATE continues a learning curve
# past its last point (10, 8, 7 hours at 0, 5 and 10 units, so 6 at 15).
m = nb.Model(time_limit=20, time_unit="days", sd={"dt": 1, "method": "euler"})
m.add_subscript(name="region", elements=["north", "south", "east"])
m.add_auxiliary(name="sales", dims=["region"], expr="10", elements={"south": "20", "east": "30"})
m.add_auxiliary(name="total", expr='SUM(AUX("sales[*]"))')
m.add_auxiliary(name="product", expr='PROD(AUX("sales[*]"))')
m.add_auxiliary(name="best", expr='VMAX(AUX("sales[*]"))')
m.add_auxiliary(name="worst", expr='VMIN(AUX("sales[*]"))')
m.add_auxiliary(name="price", expr="5 + 0.1 * TNOW")
m.add_auxiliary(name="opening_price", expr='INITIAL(AUX("price"))')
m.add_level(name="Stock", initial=100)
m.add_flow(name="sell", rate='AUX("total") / 10', rate_time_unit="days", **{"from": "Stock"})
m.add_auxiliary(name="stock_take", expr='SAMPLE_IF_TRUE(TNOW % 7 < 0.5, LEVEL("Stock"), 100)')
m.add_delay(name="shipping", input='AUX("total")', delay_time="4", kind="material")
m.add_auxiliary(name="arriving", expr='DELAY("shipping")')
m.add_auxiliary(name="forecast", expr='ACTIVE_INITIAL(AUX("total") * 0.9, 25)')
m.add_table(name="learning", keys=[0, 5, 10], values=[10, 8, 7], key_mode="linear")
m.add_auxiliary(name="hours_at_15", expr='TABLE_EXTRAPOLATE("learning", 15)')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
for name in ("total", "product", "best", "worst", "opening_price", "stock_take", "arriving",
             "forecast", "hours_at_15"):
    first, last = series["aux/" + name].iloc[0], series["aux/" + name].iloc[-1]
    print(f"{name:<14} day 0 {first:8.2f}   day 20 {last:8.2f}")
