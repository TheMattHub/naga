# How do I connect live data end to end?

> **In short**
>
> - **You build:** a script that reads a line's current rates from a web service on your own machine and writes them into a model. A change in the line then moves the forecast.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [calibration tutorial](calibrate-to-data.md). No network is used.
> - **Time:** about fifteen minutes.

A model that holds fixed numbers goes stale, while one that reads them from the plant stays current. By the end you define a [connection](../glossary.md#connection), declare a [data source](../glossary.md#data-source), tie two parameters to it with [data bindings](../glossary.md#data-binding), and see a change in the plant change the result.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-live.py`. A small web server on `127.0.0.1` stands in for your MES or line controller. It answers every request with one row of rates. In a study you delete the server and point the connection at your own system.

```python
--8<-- "docs/snippets/goal-live.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-live.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-live-output.txt"
```

3. Read the connections file the script writes. It holds the address and the word `private`, which says the address is inside your network. The model file never holds an address or a password, only the name `plant`. A model you receive from someone else therefore cannot choose where your machine connects.

4. Read the data source and the two bindings. The source says which connection and which path to read. Each binding reads one column and writes it over one parameter, using the same [override path](../glossary.md#override-path) that scenarios use. The transform `value` takes the first cell.

5. Read the first output line. `resolve_bindings` read the plant, wrote 1.0 and 1.25 into a copy of the model, and left your original untouched. The mean wait is about 3.2 minutes, the exact queueing answer for these rates.

6. Read the second line. The plant's service rate fell to 1.1, so the next read produced a mean wait of about 8.5 minutes. Running the same model on the same bindings after every read is the core of a digital twin.

7. To use the same files from the command line, pass the connections file to `naga bind`, `naga watch` or `naga twin` with `--connections connections.json`. Every reader only reads. No reader can write to the plant.

## Check yourself

- Predict, then run: if the plant's arrival rate rises to 1.1 and the service rate stays at 1.25, will the wait be longer or shorter than 3.2 minutes? Change `arrivals_per_min` and check.
- Explain it back: why does the model name a connection and not an address?

## Done when

- [ ] The script prints two lines, with the second wait clearly longer than the first.
- [ ] You can say which file holds the address and which holds the name.
- [ ] You can say why a failed read leaves the model's stored value in place.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `E_DATA_CONNECTION` | The source names a connection that the connections file does not define | Match the `connection` name in the model to a key in the file. |
| A refusal about a private address | `private` is missing from the connection and the address is on your machine or network | Add `"private": true` for an address inside your network. |
| A binding reports `E_DATA_READ` and the value does not change | The service is down or answered something other than a JSON array of objects | Open the address in a browser. Set `records` in the source options when the array sits under a key. |
| `E_DATA_TYPE` on a text column | A live source never writes text into the model | Bind a number or a true/false column. |
| `unknown key` in the connections file | The file allows only the documented keys | Remove the key. There is no key that turns certificate checks off. |

## Elements this page uses

- Python: `resolve_bindings` from `nagasim.databind`, `NetworkPolicy` and `load_connections` from `nagasim.netguard`, [`run_model`](../reference/api/run_model.md) and [`Model`](../reference/api/Model.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- KPI: `Time in Queue Avg` in the [resource summary](../reference/kpis.md#resource_summary).
- Command line: [`naga bind`](../reference/cli/twin.md#naga-bind) and [`naga watch`](../reference/cli/twin.md#naga-watch).
- Background: [Live data](../live-data.md) covers REST, MQTT, OPC UA and Modbus, and [Digital twin](../digital-twin.md) covers the rest of the loop.

Next: [Plan or schedule jobs](plan-or-schedule-jobs.md).
