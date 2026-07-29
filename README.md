# sigma-source-parameter-repair

Detect and repair broken **source parameters** in [Sigma](https://www.sigmacomputing.com/) workbooks after a source swap.

If you use a template data model plus a template workbook and clone them for each new team, tenant, or environment, you have probably hit this: you swap the new workbook onto the new data model, and every source parameter goes invalid at once. This tool fixes them in a single pass.

No dependencies — Python 3.9+ standard library only.

## The problem

A workbook control can drive a control defined inside a data model. That binding is called a *source parameter*, and in the workbook spec it looks like this:

```yaml
- kind: control
  controlId: RegionControl                        # the workbook-side control
  id: aBcDeFgHiJcon
  filters:
    - source: { kind: table, elementId: KLm0nOpQrS }
      columnId: TuVwXyZ012
  parameters:                                     # <-- the source parameter
    - kind: data-model
      dataModelId: 11111111-1111-1111-1111-111111111111
      controlId: Store-Region                     # the control inside the data model
```

`swapSources` accepts a `columnMapping` and a `metricMapping`, and that is all. There is no parameter mapping. So when you swap a workbook's source, Sigma rewrites each element's `source.dataModelId` but leaves every `parameters[].dataModelId` pointing at the **old** data model. Each source parameter now references a model the workbook no longer reads from, and Sigma marks it invalid.

The fix is unglamorous: rewrite that one field. The awkward part is that a real template workbook has dozens of parameters spread over dozens of elements, and the Sigma UI makes you visit each one.

## Why this is safe to automate

Cloning a data model **preserves its control ids verbatim**. A template model and its copy are identical apart from identity fields, right down to each control's `controlId`. So the repair is a straight id rewrite rather than a fuzzy name match, and the tool can verify its choice: before rewriting a binding it confirms the destination data model actually defines a control of that id.

If it cannot confirm that, it refuses to guess. See [Resolution rules](#resolution-rules).

## Install

```sh
git clone https://github.com/twells89/sigma-source-parameter-repair.git
cd sigma-source-parameter-repair
```

That's it. Optionally put it on your `PATH`:

```sh
chmod +x sigma_source_params.py
ln -s "$PWD/sigma_source_params.py" ~/.local/bin/sigma-source-params
```

## Credentials

Create API credentials in Sigma under **Administration → APIs and Tokens**, then export them. `SIGMA_BASE_URL` is the **API host** for your cloud and region, not your app URL — look it up under **Administration → Developer Access**, or in [Sigma's region support table](https://help.sigmacomputing.com/docs/region-warehouse-and-feature-support).

```sh
export SIGMA_BASE_URL="https://aws-api.sigmacomputing.com"   # adjust to your region
export SIGMA_CLIENT_ID="..."
export SIGMA_CLIENT_SECRET="..."
```

The credentials' owner needs **Can edit** access to the workbook, and an account type with *Create, edit, and publish workbooks*.

## Usage

Take the workbook id from its URL — either the UUID or the short url id works.

### Check

Report on a workbook without changing anything. Exits `1` if any binding needs attention, so it works as a pipeline gate.

```sh
python3 sigma_source_params.py check WORKBOOK_ID
```

```
workbook : Regional Template          (version 3)
reads from: 22222222-2222-2222-2222-222222222222
source parameters: 3

  [REPAIR  ] 'City'  (element aBcDeFgHiJcon)
             data model control: Store-City
             11111111-1111-1111-1111-111111111111
          -> 22222222-2222-2222-2222-222222222222
             live source defines control 'Store-City'

  ...

3 source parameter(s) need attention.
```

Add `--json` for machine-readable output (available on `repair` too).

### Repair

Dry run by default — it prints exactly what it would change and touches nothing:

```sh
python3 sigma_source_params.py repair WORKBOOK_ID
```

Write the change:

```sh
python3 sigma_source_params.py repair WORKBOOK_ID --apply
```

`--apply` creates a **new workbook version**. The previous version stays available in Sigma's version history, so the change is revertible.

After writing, the tool re-reads the workbook and reports how many bindings still need attention, rather than assuming the write worked.

## Resolution rules

A binding is healthy only when its data model is a live source **and** that model really defines the referenced control. Sigma validates both halves and matches control ids exactly, so a stale control id is just as broken as a stale model id.

For anything else, the tool picks a target:

| Situation | Result |
| --- | --- |
| Live source, defines the control, and filters the right element | `ok` — left alone |
| Exactly one live source defines a compatible control with that id | `REPAIR` — rewritten to that source |
| Several live sources define that control id | `AMBIGUOUS` — needs `--data-model` |
| No live source defines that control id | `NO MATCH` — needs `--map` |
| The control exists but filters a different element | `MISMATCH` — needs `--map` |

The last three are deliberate. If a control was renamed, removed, or points at the wrong element, the correct target is a judgement call about intent, and quietly rebinding it to something plausible would be worse than saying so.

### Element mismatch

A binding needs more than a control that exists. The data-model control must filter the **same element the workbook control reads its values from**. A control drawing its values from a Customers table cannot drive a control that filters Stores, and Sigma rejects the pairing with the same message it uses for a stale model id — which makes it easy to misread as a tool failure.

`MISMATCH` catches that before the write and names the likely target:

```
[MISMATCH ] 'City'  (element aBcDeFgHiJcon)
             data model control: Store-City
             this control reads values from data model element customerElement1,
             but 'Store-City' filters storeElement01 — Sigma rejects that pairing.
             Did you mean 'Cust-City'? --map Store-City=Cust-City
             reads element: customerElement1
```

Suggestions are the controls that actually filter the right element, ranked so one sharing the same trailing word comes first. That is a hint for you to confirm, never an automatic rebinding.

If the control was instead meant to filter the *other* element, the fix is not a mapping — re-point its value source in Sigma, then run a plain repair.

Repairs are idempotent — running twice is a no-op.

### Repair is all-or-nothing

Sigma validates an entire spec on write, so a **partial repair cannot be saved**: one unresolved binding rejects the whole `PUT` and nothing changes. `repair --apply` therefore checks first and refuses to write while anything is unresolved, rather than attempting a doomed write:

```
Cannot write: 1 binding(s) could not be resolved.
Sigma validates the whole spec on write, so a partial repair cannot be saved —
an unresolved binding would reject the write and nothing would change.
Resolve the remaining binding(s) with --map OLD_CONTROL_ID=NEW_CONTROL_ID
(or --data-model to pick between sources), then run again.

2 other binding(s) are ready and will be written in the same pass once the rest resolve.
```

Supply the missing mapping and every binding is repaired together in one atomic version.

## Resolving what the tool cannot infer

Two flags exist to supply knowledge the tool has no way to derive. Both apply to `check` as well as `repair`, so you can see the effect before writing.

**A control was renamed** between template and clone. Only you know `Store-City` became `Store-Municipality`:

```sh
python3 sigma_source_params.py repair WORKBOOK_ID \
  --map Store-City=Store-Municipality \
  --map Store-Region=Store-District --apply
```

For many renames, keep them in a file — either a JSON object or `OLD=NEW` lines with `#` comments:

```sh
python3 sigma_source_params.py repair WORKBOOK_ID --map-file renames.txt --apply
```

A mapping is not a licence to invent: if the mapped-to control does not exist either, the binding stays `NO MATCH`.

**Several live sources define the same control id.** Pick one:

```sh
python3 sigma_source_params.py repair WORKBOOK_ID --data-model DATA_MODEL_ID --apply
```

`--data-model` selects among *valid* candidates. It cannot force a binding onto a model that lacks the control.

> Control id matching is **exact** — case- and separator-sensitive. `store-city`, `Store_City` and `Store-City` are three different controls to Sigma, and only the precise one is accepted. Worth knowing that `swapSources` *does* normalise case and punctuation when auto-matching columns; parameters get no such grace.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing needs attention (or a dry run completed) |
| `1` | Stale bindings found, or the write was blocked by an unresolved binding |
| `2` | Missing credentials, or a malformed `--map` argument |
| `3` | The Sigma API returned an error |

## Use it as a rollout gate

The durable pattern for template-based provisioning is to make the repair part of the pipeline instead of something a human remembers. Because a repair is atomic, one command either fixes everything or changes nothing:

```sh
# 1. clone the template data model
# 2. clone the template workbook
# 3. swap the new workbook onto the new data model

# 4. repair the source parameters that step 3 could not carry over.
#    Exits non-zero and writes nothing if any binding needs a human decision,
#    so the rollout stops rather than shipping a half-wired workbook.
python3 sigma_source_params.py repair "$NEW_WORKBOOK_ID" --apply
```

If your template renames controls between versions, keep the renames in a file next to the pipeline and pass `--map-file` so the rollout stays hands-off.

## A useful side effect

Sigma's write path validates source parameters. `POST /v2/workbooks/spec` and `PUT /v2/workbooks/{id}/spec` reject a stale binding with a message naming the exact offender:

```json
{
  "message": "Invalid parameter on control: aBcDeFgHiJcon targeting data model: 11111111-1111-1111-1111-111111111111, controlId: Store-City.",
  "code": "invalid_request"
}
```

So a spec that writes cleanly has no broken source parameters. Note that `GET` does **not** validate — it happily returns the stale binding — so a read-back alone proves nothing. Only the write path is a real check.

## Limitations

- Only `kind: data-model` parameters are handled. Other parameter kinds are ignored.
- A source parameter can only bind to a data-model control whose target element the workbook itself includes. Sigma rejects a binding to a control that filters an element the workbook does not use.
- Workbook spec endpoints are a Sigma **beta** API and may change.
- The tool verifies the repair at the API layer. It cannot click your dashboard for you — open the workbook to confirm the controls behave as you expect.

## Further reading

- [How it works](docs/how-it-works.md) — the spec shapes and API contracts behind the tool
- [Manage workbooks as code](https://help.sigmacomputing.com/docs/manage-workbooks-as-code)
- [Sigma REST API reference](https://help.sigmacomputing.com/reference)

## Contributing

Bug reports and pull requests are welcome. Run the tests with:

```sh
python3 -m unittest discover -s tests -v
```

The spec-analysis core is pure and fully unit tested with no network access; please keep it that way and add a case alongside any behaviour change.

There is also an end-to-end test that runs against a real Sigma organization. It is not part of CI, because it needs credentials and creates and deletes real documents. Run it before releasing a change to resolution logic or the API client:

```sh
export SIGMA_BASE_URL=... SIGMA_CLIENT_ID=... SIGMA_CLIENT_SECRET=...
export SIGMA_E2E_DATA_MODEL_ID=<a data model with 2+ controls on one element>
python3 tests/e2e/test_e2e.py
```

It clones a data model with one control deliberately renamed, builds a workbook against the original, breaks it with a real `swapSources` call, and asserts the whole flow through to an atomic repair. It removes everything it creates, including sweeping strays by name if a run dies midway.

## License

[Apache-2.0](LICENSE)

---

Not an official Sigma Computing product.
