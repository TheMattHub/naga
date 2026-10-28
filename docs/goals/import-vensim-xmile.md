# How do I import a Vensim or XMILE model?

> **In short**
>
> - **You build:** a script that reads a small Vensim file into a model, lists what the reader could not translate, and checks the run against the closed-form answer.
> - **You need:** Python 3.11 or later and `pip install nagasim`. A Vensim `.mdl` or XMILE file of your own is optional, because the script carries its own.
> - **Time:** about ten minutes.

Many [system dynamics](../glossary.md#system-dynamics) models already exist as [Vensim or XMILE](../glossary.md#vensim) files. By the end you import one and read the problem list the reader returns. Then you run the imported model like any other, so you know what carried over and what did not.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-import.py`. The Vensim text sits inside the script as a string: a [stock](../glossary.md#stock) of people that grows through a feedback loop, plus one variable that reads a spreadsheet. To import your own file, replace the string with `mdlimport.load_mdl("yourmodel.mdl")`, or use `sdimport.load_xmile` for XMILE.

```python
--8<-- "docs/snippets/goal-import.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-import.py
```

You should see this. The run has no randomness, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-import-output.txt"
```

3. Read the first line. The reader found one stock, one flow and two auxiliaries: `Growth_Rate` and `Capacity`. Vensim treats spaces and underscores as the same, so `Growth Rate` becomes `Growth_Rate`.

4. Read the problem line. The variable `Survey_Result` reads a spreadsheet with `GET XLS`. The reader never opens another file, so it leaves that variable out and reports it with a code. Nothing is dropped silently. Every construct it cannot translate comes back as a problem like this one.

5. Read the last two lines. The imported layout is an ordinary model, so `run_model` runs it. After 30 years the population is 999.5 against 999.4 from the closed-form logistic curve. The small gap is the step size of the numerical method.

6. To import from the command line, run `naga sdimport yourmodel.mdl -o model.json`. It prints the same problems and writes the model. The exit code is 1 when any problem is an error.

7. Treat every reported problem as work for you. Rewrite the variable by hand in the model file, replace a spreadsheet read with a [data binding](../glossary.md#data-binding), or accept the gap and say so in your results.

## Check yourself

- Predict, then run: if you change `Growth Rate = 0.4` to `0.2`, will the population at year 30 rise or fall? Change it and check.
- Explain it back: why does the reader report an untranslated variable instead of guessing it?

## Done when

- [ ] The script prints a count of imported records, one problem, and two populations that agree.
- [ ] You can say what the reader does with a construct it does not know.
- [ ] You can say how to import a file from the command line.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `E_SDIMPORT_UNSUPPORTED` for a variable | The file uses a macro, a `GET XLS` read or a function the reader lacks | Rewrite that variable in the model file, or remove the dependence on it. |
| Names look different after import | Vensim spaces became underscores | Use the underscore form in expressions. A dimension name with a space is not translated. |
| Accented letters look wrong | An older `.mdl` is Windows-1252 and not UTF-8 | The reader tries UTF-8 first and then Windows-1252, so check the original file's encoding. |
| `E_SDIMPORT_XML` on an XMILE file | The file is over 20 MB or has a `DOCTYPE` declaration, which is refused for safety | Remove the declaration, or export a smaller model. |
| The run differs from Vensim | The time step or integration method differs | Compare `TIME STEP` and the method in both tools, then compare a few points. |

## Elements this page uses

- Python: `import_mdl` from `nagasim.mdlimport`, `import_xmile` from `nagasim.sdimport`, and [`run_model`](../reference/api/run_model.md).
- Command line: [`naga sdimport`](../reference/cli/build-from-data.md#naga-sdimport).
- Background: [System dynamics](../system-dynamics.md#import-from-xmile-or-vensim) lists exactly which Vensim and XMILE constructs carry over.

Next: [Combine event flow, system dynamics and agents](hybrid-model.md).
