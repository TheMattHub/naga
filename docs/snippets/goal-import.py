import math

from nagasim import api, mdlimport

# A small Vensim file: logistic growth of a population, plus one variable the reader cannot translate.
# In a study you would point `mdlimport.load_mdl` at your own file.
MDL = """{UTF-8}
Population = INTEG(Births, 10)
	~	people
	~	The stock.
	|
Births = Growth Rate * Population * (1 - Population / Capacity)
	~	people/Year
	~	A flow with a feedback loop.
	|
Growth Rate = 0.4
	~	1/Year
	~	|
Capacity = 1000
	~	people
	~	|
Survey Result = GET XLS CONSTANTS('survey.xls', 'Sheet1', 'B2')
	~	people
	~	Data in a spreadsheet next to the model.
	|
INITIAL TIME = 0 ~ Year ~ | FINAL TIME = 30 ~ Year ~ | TIME STEP = 0.25 ~ Year ~ | SAVEPER = TIME STEP ~ Year ~ |
"""

layout, problems = mdlimport.import_mdl(MDL)
print(f"imported {len(layout['levels'])} stock, {len(layout['flows'])} flow, {len(layout['auxiliaries'])} auxiliaries")
for problem in problems:
    print(f"{problem.severity} {problem.code}: {problem.message}")

# The imported file is an ordinary model: run it.
results = api.run_model(layout, replications=1)
series = results["timeseries"].query("Series == 'level/Population'")
print(f"population after {series['Time'].iloc[-1]:.0f} years: {series['Value'].iloc[-1]:.1f}")
print(f"closed form for logistic growth: {1000 / (1 + 99 * math.exp(-0.4 * 30)):.1f}")