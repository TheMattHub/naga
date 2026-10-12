# Semantic diff

A plain `git diff` on two model JSON files is mostly noise: a tool that saves a model writes blocks, resources and
every other list back in whatever order its own structures hold them, so saving a model
with no real edit can still produce a wall of reordered lines. `naga diff` compares two model files by **record identity** instead -- a block by its name,
a link by its source/target/port, a network segment by its from/to -- so it reports only what
actually changed.

## `naga diff`

```console
naga diff model.json line.json
naga diff model.json line.json --json --all
```

Exit code 0 when there is no visible change, 1 when there is, 2 on error (a missing file, invalid
JSON, or a `schema_version` newer than this build supports). Each change is one of:

| Kind | Meaning |
|---|---|
| `added` | a record present in `b` but not `a` -- the path is the whole record (`resources.Spare`), not a field under it |
| `removed` | a record present in `a` but not `b` -- same rule |
| `changed` | a field differs between two matched records (`blocks.Drill.params.dist_params.value`) |
| `moved` | a purely visual field differs -- drawing geometry the engine never reads |

`moved` changes (a block's position, a rotated asset, a relabelled tag position, ...) are
**hidden by default**: a model that was only moved around, not edited, diffs clean. Pass `--all` to
see them too. `--json` prints the changes as a JSON array (`kind`, `path`, `old`, `new`) instead of
one line each.

## Identity and ceilings

Records are matched by identity, not by list position, for every list-valued section of the model
file format plus a network's own `nodes`/`segments`:

| Section(s) | Identity |
|---|---|
| `blocks` | `name` |
| `links` | `source`, `target`, and `source_port`/`target_port` when either is set |
| `network.nodes` | `name` |
| `network.segments` | `from`, `to` |
| `orders`, `annotations` | `id` |
| every other named section (`resources`, `resource_sets`, `entities`, `variables`, `schedules`, `failures`, `tables`, `timeseries_inputs`, `levels`, `flows`, `fixed_costs`, `auxiliaries`, `delays`, `subscripts`, `agent_types`, `populations`, `conversions`, `sequences`, `fleets`, `scenarios`, `reality_checks`, `data_sources`, `data_bindings`) | `name` |
| `submodels` (a `{name: definition}` object, not a list) | the dict key |
| an agent type's `states` (one level inside `agent_types`) | `name` -- so a state's position on a statechart drawing is a hidden `moved` |

This list is **discovered from the model schema** (`nagasim.modelschema.build_json_schema`), not
hard-coded, so a future section is picked up automatically. Three documented ceilings:

- **A list with no identity key at all** diffs by position (pairs index *i* of `a` with index *i* of
  `b`). No current section falls into this case.
- **Two parallel links** (or any two records) with an *identical* identity -- e.g. two links between
  the same pair of blocks, neither carrying a port -- cannot be told apart by identity alone, and are
  paired by the order they appear in their list.
- **A renamed record is a remove + add**, never a `changed`. Renaming `Drill` to `Drill1` reports
  `removed resources.Drill` and `added resources.Drill1`, even though every other field is identical.
- **Nested lists one level inside a record** (a submodel definition's own `blocks`/`links`, a
  resource set's `members`, an ASSIGN's `assignments`, ...) are **not** identity-matched: they compare
  as a single value, the whole list old vs. new, exactly like a scalar field. An agent type's
  `states` are the one exception (table above).

A value that changes shape (an object turned into a list) is one `changed` on that field.

## `git diff` on model files

`naga diff --textconv file.json` renders one model file as a stable, line-per-record text form --
identical for two files that differ only in list order, so `git diff` (or `git show`) reads like the
semantic diff above instead of a wall of moved lines. Wire it in with a `.gitattributes` entry and a
matching `git config`:

```text
# .gitattributes
*.json diff=naga-model
```

```bash
git config diff.naga-model.textconv "naga diff --textconv"
```

This is per-clone configuration (`git config`, not `.gitattributes` alone, sets the actual command
to run) -- share the `.gitattributes` line in the repository and the `git config` line in your setup
notes.

## `naga merge` and the git merge driver

```console
naga merge model.json model.json line.json -o merged.json
```

The three files are BASE, OURS and THEIRS, in that order; `--json` prints the conflicts as JSON.

A three-way merge on the same identity paths. All three files are
migrated to the current schema first. Per path:

- changed on one side only: that side's value; changed identically on both: fine;
- changed differently on both: a **conflict**, and the merged file keeps **ours**;
- deleted on one side, changed on the other: a conflict, the merged file keeps ours' choice;
- added on both sides under the same identity with different content: a conflict, ours kept;
- `moved` (drawing-only) fields never conflict: ours wins, theirs' move applies when ours left it alone.

List order in the merged file is ours' order, with theirs' additions appended in theirs' relative
order. The merged model is then validated: every error the merge itself created (absent from ours
and theirs alone) is a conflict with reason `validation` -- e.g. a link theirs added to a block ours
deleted -- and that record goes back to ours' version. In a model with submodels, errors are found
in the flattened model and only name their pointer, with nothing reverted -- except a top-level
link to a block that no longer exists, which is reverted exactly as above.

OUT defaults to OURS (git's `%A`) and is written even on conflicts. Exit code 0 clean, 1 on
conflicts (one line per conflict on stderr; `--json` also prints them as a JSON array on stdout),
2 on error. Wire it in as a git merge driver, scoping the attribute to your model files:

```text
# .gitattributes in the folder that holds the models (a non-model JSON file must not match)
*.json merge=naga-model
```

```bash
git config merge.naga-model.driver "naga merge %O %A %B"
```

Ceilings: a renamed record is a remove + add; a list without an identity key merges by position; the
merge is value-level only -- two different edits of one expression string conflict, they are never
merged inside the string.
