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

Add `--json` for machine-readable output.

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

For each source parameter whose `dataModelId` is not among the workbook's live data-model sources, the tool picks a target:

| Situation | Result |
| --- | --- |
| Already points at a live source | `ok` — left alone |
| Exactly one live source defines a control with that id | `REPAIR` — rewritten to that source |
| Several live sources define that control id | `AMBIGUOUS` — left alone, reported |
| No live source defines that control id | `NO MATCH` — left alone, reported |

The last two are deliberate. If a control was renamed or removed in the new data model, the correct target is a judgement call about intent, and quietly rebinding it to something plausible would be worse than saying so. `--apply` repairs what it can confirm, leaves the rest untouched, and exits non-zero so you know work remains.

Repairs are idempotent — running twice is a no-op.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing needs attention (or a dry run completed) |
| `1` | Stale bindings found, or some could not be resolved automatically |
| `2` | Missing or invalid credentials |
| `3` | The Sigma API returned an error |

## Use it as a rollout gate

The durable pattern for template-based provisioning is to make the repair part of the pipeline instead of something a human remembers:

```sh
# 1. clone the template data model
# 2. clone the template workbook
# 3. swap the new workbook onto the new data model
# 4. repair the source parameters that step 3 could not carry over
python3 sigma_source_params.py repair "$NEW_WORKBOOK_ID" --apply

# 5. fail the rollout if anything is still unresolved
python3 sigma_source_params.py check "$NEW_WORKBOOK_ID"
```

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

## License

[Apache-2.0](LICENSE)

---

Not an official Sigma Computing product.
