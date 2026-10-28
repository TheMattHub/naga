# Glossary

This page defines the terms the learning path and the goal tutorials use, one line each. The terms are in six groups: building a model, running it, comparing and searching, fitting inputs, calibration, data and checks, and assistants, sharing and learning. A tutorial links a term here the first time it uses it.

## Building a model

| Term | Meaning |
|---|---|
| <a id="model-file"></a>**Model file** | The JSON file that holds a model. The Python builder, the editor and the command line all read and write it. |
| <a id="builder"></a>**Builder** | The Python module `nagasim.build`. It writes a model file from Python code. |
| <a id="block"></a>**Block** | One step of the flow a model describes, such as `create`, `process` or `dispose`. Blocks are linked in order. |
| <a id="entity"></a>**Entity** | A thing that moves through the blocks, such as a customer, a call or a part. |
| <a id="resource"></a>**Resource** | Something an entity needs to be served and that has a limited capacity, such as a server, an agent or a machine. |
| <a id="queue"></a>**Queue** | The entities waiting for a resource. |
| <a id="arrival-rate"></a>**Arrival rate** | How many entities enter per time unit. An arrival rate of 1.0 per minute means one a minute on average. |
| <a id="service-rate"></a>**Service rate** | How many entities one server finishes per time unit when it is never idle. A rate of 1.25 per minute means 0.8 minutes per entity on average. |
| <a id="distribution"></a>**Distribution** | The rule for a random duration or count, such as `Exponential(rate=1.0)`. Each draw is one value from it. |
| <a id="time-unit"></a>**Time unit** | The unit of every time in the model. The tutorials use minutes. |
| <a id="override-path"></a>**Override path** | The address of one value in a model file, such as `resources.Agents.capacity`. Scenarios, factors and the optimiser use it to change that value. |

## Running a model

| Term | Meaning |
|---|---|
| <a id="seed"></a>**Seed** | The number that fixes the random draws of a run. The same model and seed give the same numbers. |
| <a id="replication"></a>**Replication** | One run of the model with its own seed. One replication is one sample, so a claim needs many. |
| <a id="warm-up"></a>**Warm-up** | The first part of a run that is thrown away. A model starts empty, and the warm-up lets it reach its usual load before statistics start. |
| <a id="steady-state"></a>**Steady state** | The long-run behaviour of a system once the effect of its empty start has gone. |
| <a id="mser-5"></a>**MSER-5** | A rule that suggests a warm-up length from the throughput times of a run. It groups them in batches of five and drops the head that leaves the tail steadiest. |
| <a id="kpi"></a>**KPI** | A key performance indicator: a number a run reports, such as `resource/Server/Time in Queue Avg`. The [KPI reference](reference/kpis.md) lists them. |
| <a id="utilisation"></a>**Utilisation** | The share of time a resource is busy. |
| <a id="wip"></a>**WIP** | Work in progress: the entities inside the system at one moment. |
| <a id="throughput"></a>**Throughput** | How many entities leave the system per time unit. |
| <a id="littles-law"></a>**Little's law** | In a stable system, the average number inside equals the arrival rate times the average time inside. |
| <a id="confidence-interval"></a>**Confidence interval** | A range that holds the true mean with a stated probability, usually 95%. The tutorials compute it across replications. |
| <a id="half-width"></a>**Half-width** | Half the length of a confidence interval, so a mean of 3.2 with a half-width of 0.3 reads as 3.2 plus or minus 0.3. |
| <a id="precision"></a>**Precision** | The half-width you accept, set as a share of the mean (relative) or in the KPI's unit (absolute). |

## Comparing and searching

| Term | Meaning |
|---|---|
| <a id="scenario"></a>**Scenario** | A name and a list of overrides on the base model. It answers one what-if question. |
| <a id="common-random-numbers"></a>**Common random numbers** | Running every scenario or design point on the same seeds, so the random noise they share cancels in the comparison. |
| <a id="factor"></a>**Factor** | One model parameter an experiment varies, with its range, such as the number of agents from 5 to 7. |
| <a id="response"></a>**Response** | The KPI an experiment measures at each design point. |
| <a id="doe"></a>**DOE** | Design of experiments: choosing which combinations of factor values to run so that effects can be read from few runs. |
| <a id="full-factorial"></a>**Full factorial** | A design that runs every combination of the factors' levels. |
| <a id="main-effect"></a>**Main effect** | How much the response changes, on average, when one factor goes from its low to its high level. |
| <a id="interaction"></a>**Interaction** | An effect that depends on another factor, such as extra agents mattering only when arrivals are high. |
| <a id="tornado"></a>**Tornado** | A one-at-a-time sensitivity table: each factor swings alone while the others stay in the middle, sorted by how far the response moves. |
| <a id="decision-variable"></a>**Decision variable** | A parameter you can choose, such as the number of agents. The optimiser searches over it. Optimisation calls it a factor. |
| <a id="objective"></a>**Objective** | The KPI the optimiser minimises or maximises, such as total cost. |
| <a id="optimisation"></a>**Optimisation** | A search over the decision variables for the best value of the objective within a budget of replications. |
| <a id="budget"></a>**Budget** | The most replications a search may spend. |

## Fitting inputs

| Term | Meaning |
|---|---|
| <a id="distribution-fit"></a>**Distribution fit** | Choosing a distribution and its parameters so that it matches observed data. |
| <a id="aic"></a>**AIC** | A score for a fitted distribution that rewards fit and penalises extra parameters. Lower is better, and a gap under about 2 is no real difference. |
| <a id="ks-test"></a>**KS test** | A test of how far a sample is from a fitted distribution. A very small p-value means the fit is poor. |
| <a id="input-uncertainty"></a>**Input uncertainty** | The error in a result that comes from fitting inputs to a limited sample. More replications do not shrink it. |
| <a id="bootstrap"></a>**Bootstrap** | Redrawing a sample with replacement from the data, many times, to see how much a fit would change with different data. |

## Calibration, data and checks

| Term | Meaning |
|---|---|
| <a id="calibration"></a>**Calibration** | Tuning model parameters until the model reproduces what the real system measured. |
| <a id="calibration-target"></a>**Calibration target** | One measured number the model has to reproduce, such as a mean wait of 3.2 minutes, with the tolerance you accept. |
| <a id="abc"></a>**ABC** | Approximate Bayesian computation: calibration that returns every parameter value consistent with the data, as an interval, and not only the best one. |
| <a id="data-source"></a>**Data source** | A named place a model reads data from, such as a CSV file or a REST service. |
| <a id="data-binding"></a>**Data binding** | A rule that reads one column of a data source and writes the result over one model parameter. |
| <a id="connection"></a>**Connection** | The address and credential of a live data source. It lives in a file of yours, and the model names it. |
| <a id="order-book"></a>**Order book** | The list of jobs a plan has to schedule, each with a release time, a due date and a routing. |
| <a id="makespan"></a>**Makespan** | The time from the start of a plan to the end of its last job. |
| <a id="tardiness"></a>**Tardiness** | How late a job finishes after its due date, or 0 when it is on time. |
| <a id="cp-sat"></a>**CP-SAT** | The constraint solver in the OR-Tools package. The engine uses it to sequence an order book. |
| <a id="stock"></a>**Stock** | A quantity that accumulates, such as a population or a tank level. The model file calls it a level. |
| <a id="system-dynamics"></a>**System dynamics** | Modelling with stocks, the flows that fill and drain them and the feedback between them. |
| <a id="agent"></a>**Agent** | An individual with its own states and rules, such as a staff member who is working or on a break. |
| <a id="hybrid-model"></a>**Hybrid model** | A model that mixes event flow, system dynamics and agents, and lets each read the others. |
| <a id="vensim"></a>**Vensim and XMILE** | Two file formats for system dynamics models. Vensim files end in `.mdl`, and XMILE files in `.xmile` or `.stmx`. |
| <a id="assertion"></a>**Assertion** | A claim about a KPI stored in the model file, such as a mean wait of at most 5 minutes. It passes, fails or is inconclusive. |
| <a id="continuous-integration"></a>**Continuous integration** | A job that runs your checks automatically on every push to a repository. |
| <a id="junit"></a>**JUnit report** | An XML file of test results that most CI systems can display. |
| <a id="holding-cost"></a>**Holding cost** | The cost of one entity being in the system for one model time unit. |
| <a id="cost-driver"></a>**Cost driver** | An input whose change moves the total cost the most. |

## Assistants, sharing and learning

| Term | Meaning |
|---|---|
| <a id="tool-call"></a>**Tool call** | One request an AI assistant makes to the engine, such as adding a block or running a model. Each tool is a plain Python function. |
| <a id="mcp"></a>**MCP** | The Model Context Protocol, a standard way for an AI assistant to call tools. `naga mcp` publishes the engine's tools over it. |
| <a id="workspace"></a>**Workspace** | The folder an assistant may reach through the tools. The person who starts the server sets it, and a tool cannot widen it. |
| <a id="player-file"></a>**Player file** | One HTML page that replays a recorded run in a browser. It holds names, layout and the run, and no parameter of the model. |
| <a id="viewer-file"></a>**Viewer file** | One HTML page in which the engine itself runs, so the reader can change inputs and run again. It holds the whole model. |
| <a id="plugin"></a>**Plugin** | A Python file or package that adds a block, a distribution or an exporter to the engine. It is trusted code. |
| <a id="policy"></a>**Policy** | A rule that picks an action at a decision point, such as which of two machines gets the next job. |
| <a id="reinforcement-learning"></a>**Reinforcement learning** | Learning a policy by trying actions in a simulated system and keeping those that earned more reward. |
| <a id="episode"></a>**Episode** | One run of the model from start to end, during which a policy makes its decisions. |
