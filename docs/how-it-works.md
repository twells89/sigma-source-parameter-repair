# How it works

Reference notes on the spec shapes and API contracts this tool depends on. Everything here was established by reading the Sigma OpenAPI description and by round-tripping real workbooks through the API.

## Where source parameters live

In the workbook spec, a source parameter is an entry in the `parameters` array of a `kind: control` element. It sits alongside `filters`, which is a separate mechanism — `filters` targets columns of elements *inside* the workbook, while `parameters` targets a control *inside a data model*.

```yaml
pages:
  - id: pAgE123456
    name: Page 1
    elements:
      - kind: control
        controlId: RegionControl          # identifies the control within the workbook
        id: aBcDeFgHiJcon                 # element id; also the URL's :nodeId
        name: Region                      # display label
        filters:                          # element-level filter targets
          - source:
              kind: table
              elementId: KLm0nOpQrS
            columnId: TuVwXyZ012
        parameters:                        # source parameters
          - kind: data-model
            dataModelId: 11111111-1111-1111-1111-111111111111
            controlId: Store-Region        # control id *inside* that data model
        controlType: list
        mode: include
        selectionMode: multiple
        values: []
```

Two different `controlId` values appear here and they are easy to confuse:

- the element's own `controlId` (`RegionControl`) — the workbook-side handle used in formulas
- `parameters[].controlId` (`Store-Region`) — the id of the control defined in the data model

The tool matches on the second one.

Because workbooks-as-code exposes this array, source parameters *are* settable from code, even where interactive builder tooling does not offer them.

## Why a source swap breaks them

`POST /v2/workbooks/{workbookId}/swapSources` and `POST /v3alpha/workbooks/{workbookId}:swapSources` both accept exactly two mapping collections:

- `columnMapping`
- `metricMapping`

There is no parameter mapping in either version. The swap rewrites `source.dataModelId` on each data-bearing element, so tables and charts follow the new model, but `parameters[].dataModelId` is left as it was. Every source parameter then names a data model the workbook no longer reads from.

This is worth understanding as an API gap rather than a bug in your workbook: the swap already auto-matches columns and metrics by name when you omit the explicit mappings, and since cloned data models keep their control ids, it has everything it needs to do the same for parameters.

## Why an id rewrite is sufficient

Cloning a data model preserves control identity completely. Comparing a template model with its clone shows the two specs are identical apart from `dataModelId`, `name`, `url`, `documentVersion` and timestamps — every control keeps both its `controlId` and its internal element `id`.

The practical consequence is that all the stale bindings in a freshly swapped workbook share a *single* old data model id, and the repair is one substitution applied everywhere. The tool still verifies each one against the destination model's real control ids rather than trusting that invariant blindly, which is what lets it detect the renamed-control case.

## Endpoints used

| Call | Purpose |
| --- | --- |
| `POST /v2/auth/token` | Exchange client credentials for a bearer token (~1h TTL) |
| `GET /v2/workbooks/{id}/spec?format=json` | Read the workbook's code representation |
| `GET /v2/workbooks/{id}/sources` | List what the workbook actually reads from now |
| `GET /v2/dataModels/{id}/spec?format=json` | Read a data model, to enumerate its control ids |
| `PUT /v2/workbooks/{id}/spec` | Write the repaired workbook |

### The document envelope, and why layout gets lost

`GET` returns read-only metadata around the document. **Workbooks nest the
document under a `document` key; data models return it flat.** Both carry a
`kind` field.

```
workbook   {workbookId, name, url, documentVersion, ..., document: {kind, layout, pages, schemaVersion}}
data model {dataModelId, name, url, documentVersion, ..., kind, pages, schemaVersion}
```

Writes mirror that: a workbook `PUT` body is `{"document": {...}}` and a flat one
is rejected with `Expecting { schemaVersion: 1 } at 0.document`; a data model
takes the document flat and rejects the wrapper with `Syntax error in data model
spec`.

The dangerous part is what happens when a field is *omitted* rather than wrong.
Measured, holding everything else constant:

| Body | Result |
| --- | --- |
| the whole document | accepted, layout byte-identical |
| without `kind` | **rejected** |
| without `layout` | **accepted — Sigma regenerates the layout** |
| `layout: null` | **accepted — Sigma regenerates the layout** |
| only `schemaVersion` + `pages` | **rejected** |

A regenerated layout means every element is repositioned: the controls move.
Nothing in the response tells you it happened.

So the document is sent back **whole** — `unwrap_document()` splits the envelope
off, the bindings are mutated in place, and `document_for_write()` returns the
same dict with nothing removed. `ENVELOPE_METADATA` is a *denylist* of read-only
fields rather than an allowlist of known content, so a field this tool has never
heard of still survives the round trip. Cherry-picking known keys is precisely
the bug.

`PUT` **replaces** the workbook with the supplied representation and creates a new version. It does not destroy the previous one — earlier versions remain in Sigma's version history — but it does mean you must send the whole spec, not a patch. Always read immediately before writing.

## Validation as a linter

Both write paths validate source parameters and name the offending element:

```
HTTP 400
{
  "message": "Invalid parameter on control: aBcDeFgHiJcon targeting data model: 11111111-1111-1111-1111-111111111111, controlId: Store-City.",
  "code": "invalid_request"
}
```

This makes the write path a reliable oracle for whether a workbook's source parameters are sound, and it is why cloning a broken workbook via `POST /v2/workbooks/spec` fails until the bindings are repaired.

`GET` does not validate. It returns stale bindings without complaint, so read-back alone cannot tell you a workbook is healthy — it only tells you what the workbook currently claims.

## Writes are all-or-nothing

Because validation covers the whole spec, there is no way to persist a partial
repair. If a spec contains one binding that cannot be resolved, the `PUT` is
rejected and *none* of the other rewrites land either. The tool therefore
classifies every binding first and refuses to write while any of them is
unresolved, instead of attempting a write that cannot succeed.

This is why the escape hatches matter: `--map` and `--data-model` are not
conveniences, they are what unblocks the write.

## Identity is not acceptance

A binding names a `(dataModelId, controlId)` pair. Both must resolve — but even
when they do, Sigma may still decline the pairing.

Sweeping one control's value source against four candidate parameters, holding
everything else constant:

```
value source                        Store-State  Store-City  Cust-State  Cust-City
Store / Store State                 OK           OK          OK          OK
Store / Store City                  OK           OK          OK          OK
Customers / Cust State              REJECTED     REJECTED    OK          OK
Customers / Cust City               REJECTED     REJECTED    OK          OK
```

The column within the element makes no difference; the element does, and
**asymmetrically** — a Store-sourced control may drive any of the four, while a
Customers-sourced control may drive only the `Cust-*` pair. An earlier version of
this document claimed the two elements must simply match. That is false, and the
table above is the disproof.

The asymmetry looks like filter reachability through the model's join graph, and
that is not derivable from the spec: a data model's code representation contains
no relationships at all — they live in its upstream model. So the tool does not
try. It resolves identity, attempts the write, and translates a rejection.

`PUT` and `POST` were checked against each other and agree, so either can be
used as an oracle; `POST` has the useful property of creating nothing when it
rejects, which makes it a non-destructive preflight.

## A rejection is not a verdict on intent

`Invalid parameter on control: X targeting data model: Y, controlId: Z` is
emitted for at least three different causes — a stale model id, a control that
does not exist, and a pairing the API declines. `explain_rejection()`
distinguishes them by re-reading the target model, and is careful to say that the
*write API* refused rather than that the user is wrong: the UI may well accept a
pairing the spec API will not.

## Data models carry source parameters too

`parameters[]` on a control is documented in the data-model spec schema with the
identical shape, so a model's control can drive a control in its upstream model —
and a source swap breaks it the same way. Endpoint differences:

| | Workbook | Data model |
| --- | --- | --- |
| spec | `/v2/workbooks/{id}/spec` | `/v2/dataModels/{id}/spec` |
| `PUT` body | `schemaVersion`, `pages`, `layout`, theme | `schemaVersion`, `pages` **only** |
| sources | bare JSON array | `{"entries": [...]}` |

Sending `layout` to a data model is rejected, and reading `sources` as a bare
list silently yields no live sources for a model — which would make every
binding look unresolvable.

## Nested elements

Controls are often nested inside container elements, and containers can nest arbitrarily. A flat iteration over `page["elements"]` silently misses them, which in this problem space means silently under-reporting broken parameters. `iter_elements()` walks the tree, descending through both `elements` and `children`.

## Design notes

The spec-analysis core — `iter_elements`, `find_source_parameters`, `data_model_control_ids`, `plan_repairs`, `apply_repairs`, `spec_to_update_body` — is pure and network-free, so the interesting behaviour is unit testable against small literal specs. `SigmaClient` holds all the I/O. `plan_repairs` returns a classification for *every* parameter including healthy ones, so `check` can report the full picture instead of only the failures.

`apply_repairs` mutates the parameter dicts in place; `SourceParameter.raw` is a live reference into the spec that was read. That keeps the repaired spec byte-identical to the original except for the changed ids, which matters because the whole spec gets written back.
