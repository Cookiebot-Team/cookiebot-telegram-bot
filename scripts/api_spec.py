"""The OpenAPI document, as a gate and as two artifacts.

    uv run python scripts/api_spec.py lint        # is every endpoint documented?
    uv run python scripts/api_spec.py generate    # the published spec + the reference page
    uv run python scripts/api_spec.py generate --check   # would generating change anything?

`cb_api.main.app` already builds a full OpenAPI 3.1 document — FastAPI derives
it from the same type annotations the handlers run on, so it cannot drift from
the code the way a hand-written API document does. What it *can* be is thin: a
route with no summary, a response typed `object`, a refusal nobody declared.
Those are invisible until someone tries to write a client or a test against it,
which is exactly the moment being thin is most expensive.

So this file does two things with that document.

**`lint`** is the gate. Nine rules, each one a thing a client author or a tester
needs and cannot recover from the code. `cb.py check` runs it, so an endpoint
lands documented or it does not land.

**`generate`** turns the document into what people read and test against:

| Output | For |
|---|---|
| `docs/site/public/openapi.json` | client generators, and the contract tests in `qa/api/` |
| `docs/site/public/api-reference/index.html` | a rich, offline, searchable reference — every operation with its scopes, parameters, response shape and a copyable request |

Both are committed, and `generate --check` fails when they are stale. A
generated artifact nobody regenerated is worse than none: it is wrong and
authoritative-looking at the same time.

The published `openapi.json` is not decoration. `qa/api/test_contract.py`
validates every response the API gives against the schema in that file, so a
handler that changes its shape without the document following fails the suite —
which is what makes the document a contract rather than a description.

One loader, two outputs, one gate, in one file — because all three need the
same list of what is exempt and why, and a second copy of that list is how it
goes stale.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "docs" / "site" / "public"
SPEC_FILE = PUBLIC / "openapi.json"
# `/api-reference/`, not `/api/`: the docs site owns `/api/search` (Fumadocs'
# static search index), and a published page sharing a prefix with a framework
# route is a collision waiting for whoever adds the next one.
REFERENCE_FILE = PUBLIC / "api-reference" / "index.html"

#: Nothing is exempt any more, and the empty set is kept deliberately rather
#: than deleted — as the place to record the next exemption *with its reason*.
#:
#: v1's `/` and `POST /login` used to be here: their bodies are what
#: `COOKIEBOT-WebHub` reads by name, so a `response_model` would filter an
#: unlisted key back out the day one is added. They now declare their shapes
#: through `responses={200: {"model": ...}}` with `response_model=None`, which
#: documents the body without touching a byte of it — the pattern to copy when
#: something else needs to be compatible and described at once.
UNMODELLED: set[tuple[str, str]] = set()

#: FastAPI's own validation envelope. Not ours to document.
FOREIGN_SCHEMAS = {"HTTPValidationError", "ValidationError"}

METHOD_ORDER = ("get", "post", "put", "patch", "delete")


def load_spec() -> dict[str, Any]:
    """The document the running service would serve, without running it.

    Imported rather than fetched over HTTP on purpose: the gate has to work in
    CI with no database, no port and no container.
    """
    sys.path[:0] = [
        str(ROOT / "packages" / package / "src")
        for package in ("cb-core", "cb-api", "cb-gateway", "cb-worker")
    ]
    from cb_api.main import app

    return dict(app.openapi())


@dataclass(frozen=True)
class Operation:
    """One method on one path, with the document's own view of it."""

    method: str
    path: str
    body: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    @property
    def tag(self) -> str:
        tags = self.body.get("tags") or []
        return str(tags[0]) if tags else ""

    @property
    def secured(self) -> bool:
        return bool(self.body.get("security"))

    @property
    def group_scoped(self) -> bool:
        return "{group_id}" in self.path

    @property
    def fleet_scoped(self) -> bool:
        return self.path.startswith("/admin")

    def responses(self) -> dict[str, dict[str, Any]]:
        return dict(self.body.get("responses") or {})

    def success(self) -> dict[str, Any] | None:
        for status, response in self.responses().items():
            if status.startswith("2"):
                return response
        return None


def operations(spec: dict[str, Any]) -> list[Operation]:
    found = [
        Operation(method, path, body)
        for path, methods in spec.get("paths", {}).items()
        for method, body in methods.items()
        if method in METHOD_ORDER
    ]
    return sorted(found, key=lambda op: (op.tag, op.path, METHOD_ORDER.index(op.method)))


def ref_name(schema: dict[str, Any] | None) -> str | None:
    """The component this schema points at, one level of `$ref` or `allOf`."""
    if not schema:
        return None
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    for branch in schema.get("allOf", []) or []:
        name = ref_name(branch)
        if name:
            return name
    return None


def response_schema(response: dict[str, Any] | None) -> dict[str, Any] | None:
    content = (response or {}).get("content") or {}
    return (content.get("application/json") or {}).get("schema")


# ------------------------------------------------------------------- the gate


@dataclass(frozen=True)
class Finding:
    where: str
    rule: str
    message: str


#: Every rule, with the reason it exists. The reason is printed with the
#: finding: "add a summary" is an instruction, and "a generated client turns
#: this into the method's docstring" is why anyone should want to.
RULES: dict[str, str] = {
    "summary": "a generated client turns `summary` into the method's own name and tooltip",
    "description": "the reference page and every client's docstring come from it",
    "tag": "an operation with no described tag is unfiled in every reference that groups by tag",
    "response-schema": "a response typed `object` hands a client author `Record<string, unknown>` and a guess",
    "declares-401": "a client that cannot tell 'log in again' from 'you may not' retries the wrong one",
    "declares-404": "a group-scoped path answers 404 for a group you do not administer; that is a contract, not an accident",
    "declares-403": "a fleet-wide path refuses with 403, and hiding behind 404 there would only mislead an owner",
    "refusal-schema": "a client that cannot parse the 401 it was given cannot tell 'log in again' from 'the server broke'",
    "parameter-description": "a query parameter with no description is a knob nobody knows how to turn",
    "schema-description": "the reference renders this shape; without a docstring it renders a bare table of field names",
}


def lint(spec: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    described_tags = {
        str(tag.get("name")): bool(tag.get("description")) for tag in spec.get("tags", []) or []
    }
    ops = operations(spec)

    for op in ops:
        where = f"{op.method.upper()} {op.path}"

        if not op.body.get("summary"):
            findings.append(Finding(where, "summary", "no summary"))
        if not (op.body.get("description") or "").strip():
            findings.append(
                Finding(where, "description", "no description — the handler needs a docstring")
            )

        if not op.tag:
            findings.append(Finding(where, "tag", "no tag"))
        elif not described_tags.get(op.tag):
            findings.append(
                Finding(where, "tag", f"tag {op.tag!r} has no description in `main._TAGS`")
            )

        if op.key not in UNMODELLED:
            schema = response_schema(op.success())
            if schema is None:
                findings.append(Finding(where, "response-schema", "no JSON response declared"))
            elif not ref_name(schema) and "$ref" not in json.dumps(schema):
                findings.append(
                    Finding(where, "response-schema", "the success response names no schema")
                )

        statuses = set(op.responses())
        if op.secured and "401" not in statuses:
            findings.append(Finding(where, "declares-401", "requires a token but declares no 401"))
        if op.group_scoped and "404" not in statuses:
            findings.append(Finding(where, "declares-404", "group-scoped but declares no 404"))
        if op.fleet_scoped:
            if "403" not in statuses:
                findings.append(Finding(where, "declares-403", "fleet-wide but declares no 403"))
            if "404" in statuses:
                findings.append(
                    Finding(where, "declares-403", "fleet-wide paths must not answer 404")
                )

        for status, response in sorted(op.responses().items()):
            if status.startswith(("2", "3")) or status == "422":
                # 422 is FastAPI's own envelope, declared for every route that
                # validates anything and modelled by FastAPI, not by us.
                continue
            if response_schema(response) is None:
                findings.append(
                    Finding(where, "refusal-schema", f"{status} is declared with no body model")
                )

        for parameter in op.body.get("parameters", []) or []:
            if not (parameter.get("description") or "").strip():
                name = parameter.get("name")
                findings.append(
                    Finding(
                        where, "parameter-description", f"parameter {name!r} has no description"
                    )
                )

    for name in sorted(referenced_schemas(spec, ops) - FOREIGN_SCHEMAS):
        schema = spec["components"]["schemas"].get(name, {})
        if not (schema.get("description") or "").strip():
            findings.append(
                Finding(f"components/{name}", "schema-description", "no docstring on the model")
            )

    return findings


def referenced_schemas(spec: dict[str, Any], ops: list[Operation]) -> set[str]:
    """Every component an operation can hand back, transitively.

    Only what a *response* reaches: a request-body model is documented by the
    endpoint that takes it, and the reference page renders it there.
    """
    schemas = spec.get("components", {}).get("schemas", {})
    seen: set[str] = set()
    queue = [
        name
        for op in ops
        for response in op.responses().values()
        if (name := ref_name(response_schema(response)))
    ]
    while queue:
        name = queue.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        queue.extend(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(schemas[name])))
    return seen


def report(findings: list[Finding], total: int) -> int:
    if not findings:
        print(f"every one of the {total} documented operations passes all {len(RULES)} rules")
        return 0
    width = max(len(finding.where) for finding in findings)
    current = ""
    for finding in sorted(findings, key=lambda f: (f.rule, f.where)):
        if finding.rule != current:
            current = finding.rule
            print(f"\n{finding.rule}: {RULES[finding.rule]}")
        print(f"  {finding.where:<{width}}  {finding.message}")
    print(f"\n{len(findings)} finding(s) across {total} operations")
    return 1


# ------------------------------------------------- what the reference examples show

#: The seeded identities `qa_setup.py` creates and `qa/api/` authenticates as.
#: Named here because the reference's copyable examples use the same shell
#: variables the test suite and `qa_setup.py env` export.
ADMIN, OWNER, STRANGER = "admin_token", "owner_token", "stranger_token"


def caller_for(op: Operation) -> str | None:
    """Which of the seeded roles an example request should use."""
    if not op.secured:
        return None
    if op.fleet_scoped:
        return OWNER
    return ADMIN


def query_string(op: Operation) -> str:
    """Only the parameters worth showing: a capped `limit`, so the example is a
    request somebody would actually send rather than the bare path."""
    for parameter in op.body.get("parameters", []) or []:
        if parameter.get("name") == "limit" and parameter.get("in") == "query":
            return "?limit=5"
    return ""


# -------------------------------------------------------------- the reference

_CSS = """\
:root{color-scheme:light dark;--bg:#fbfbfd;--panel:#fff;--ink:#16161a;--muted:#5c5f6b;
--line:#e4e4ec;--accent:#7a5cff;--code:#f3f3f8}
@media (prefers-color-scheme:dark){:root{--bg:#0f0f13;--panel:#17171d;--ink:#ececf1;
--muted:#9a9aa8;--line:#26262f;--accent:#a992ff;--code:#1d1d25}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Inter,sans-serif}
code,pre{font-family:ui-monospace,"SF Mono",Menlo,monospace}
.wrap{display:grid;grid-template-columns:270px minmax(0,1fr);gap:2.5rem;
max-width:1180px;margin:0 auto;padding:0 1.5rem}
nav{position:sticky;top:0;align-self:start;max-height:100vh;overflow:auto;padding:1.5rem 0}
nav h2{font-size:.7rem;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);
margin:1.4rem 0 .4rem}
nav a{display:block;padding:.2rem .45rem;border-radius:5px;text-decoration:none;color:var(--ink);
font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
nav a:hover{background:var(--code)}
#find{width:100%;padding:.5rem .6rem;border:1px solid var(--line);border-radius:7px;
background:var(--panel);color:var(--ink);font-size:.85rem}
main{padding:1.5rem 0 6rem}
header.top{border-bottom:1px solid var(--line);padding-bottom:1.4rem;margin-bottom:1.6rem}
header.top h1{margin:0 0 .3rem;font-size:1.65rem}
.lede{color:var(--muted);max-width:60ch}
section.op{background:var(--panel);border:1px solid var(--line);border-radius:11px;
padding:1.1rem 1.25rem;margin:0 0 1.1rem}
section.op>h3{margin:0 0 .45rem;font-size:.95rem;display:flex;gap:.55rem;align-items:center;
flex-wrap:wrap}
.m{font:600 .7rem/1 ui-monospace,monospace;padding:.3rem .45rem;border-radius:5px;
background:var(--accent);color:#fff;letter-spacing:.04em}
.m.post{background:#2f8f5b}.m.put{background:#b4762a}.m.patch{background:#b4762a}
.m.delete{background:#c0392b}
.p{font-family:ui-monospace,monospace;font-size:.9rem}
.sum{color:var(--muted);margin:.1rem 0 .7rem}
.desc p{margin:.5rem 0;max-width:72ch}
.pill{display:inline-block;font-size:.7rem;padding:.15rem .45rem;border:1px solid var(--line);
border-radius:99px;color:var(--muted)}
.pill.scope{border-color:var(--accent);color:var(--accent)}
h4{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:1.1rem 0 .35rem}
table{border-collapse:collapse;width:100%;font-size:.83rem}
th,td{text-align:left;vertical-align:top;padding:.35rem .6rem .35rem 0;
border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:.72rem;text-transform:uppercase;
letter-spacing:.06em}
td.k{font-family:ui-monospace,monospace;white-space:nowrap}
td.t{color:var(--muted);font-family:ui-monospace,monospace;font-size:.78rem;white-space:nowrap}
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:.7rem .85rem;
overflow:auto;font-size:.8rem;margin:.4rem 0 0}
.req{color:var(--accent);font-size:.7rem}
.hide{display:none}
@media (max-width:900px){.wrap{grid-template-columns:1fr}nav{position:static;max-height:none}}
"""

_JS = """\
const find=document.getElementById('find');
find.addEventListener('input',()=>{
  const q=find.value.trim().toLowerCase();
  document.querySelectorAll('section.op').forEach(el=>{
    el.classList.toggle('hide', q!=='' && !el.dataset.find.includes(q));
  });
  document.querySelectorAll('nav a[data-find]').forEach(el=>{
    el.classList.toggle('hide', q!=='' && !el.dataset.find.includes(q));
  });
  document.querySelectorAll('section.tag').forEach(el=>{
    el.classList.toggle('hide', el.querySelectorAll('section.op:not(.hide)').length===0);
  });
});
"""


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def paragraphs(text: str) -> str:
    """The handler's docstring, as HTML.

    Deliberately not a Markdown renderer: these docstrings are prose with the
    occasional `code span` and **bold**, and pulling in a parser to handle three
    constructs would be a dependency for the reference page alone.
    """
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    rendered = []
    for block in blocks:
        safe = esc(" ".join(line.strip() for line in block.splitlines()))
        safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
        safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
        rendered.append(f"<p>{safe}</p>")
    return "".join(rendered)


def type_of(schema: dict[str, Any]) -> str:
    """A one-line type, the way a client author would write it."""
    name = ref_name(schema)
    if name:
        return name
    if "anyOf" in schema:
        return " | ".join(type_of(branch) for branch in schema["anyOf"])
    kind = schema.get("type")
    if kind == "array":
        return f"{type_of(schema.get('items') or {})}[]"
    if kind == "null":
        return "null"
    fmt = schema.get("format")
    return f"{kind}<{fmt}>" if fmt and kind else str(kind or "any")


def fields_table(name: str, spec: dict[str, Any], seen: set[str] | None = None) -> str:
    """A component's fields, with nested components expanded once.

    Once, not recursively to the leaves: a fully expanded `OverviewResponse` is
    four screens of nesting, and the reader who needs that opens the component
    it names, which is on the same page.
    """
    seen = seen or set()
    schema = spec.get("components", {}).get("schemas", {}).get(name)
    if not schema or name in seen:
        return ""
    properties = schema.get("properties") or {}
    if not properties:
        return ""
    required = set(schema.get("required") or [])
    rows = []
    nested: list[str] = []
    for field, definition in properties.items():
        child = ref_name(definition) or ref_name(definition.get("items") or {})
        for branch in definition.get("anyOf", []) or []:
            child = child or ref_name(branch)
        if child and child not in seen and child not in FOREIGN_SCHEMAS:
            nested.append(child)
        mark = '<span class="req">required</span>' if field in required else ""
        rows.append(
            f'<tr><td class="k">{esc(field)} {mark}</td><td class="t">{esc(type_of(definition))}</td>'
            f"<td>{esc(definition.get('description', ''))}</td></tr>"
        )
    table = (
        f"<table><thead><tr><th>field</th><th>type</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    described = schema.get("description")
    lede = f'<div class="desc">{paragraphs(described)}</div>' if described else ""
    out = f"<h4>{esc(name)}</h4>{lede}{table}"
    for child in dict.fromkeys(nested):
        out += fields_table(child, spec, seen | {name})
    return out


def scopes_of(op: Operation) -> list[str]:
    """The scopes named in the 403's own description, which is where the router
    puts them — there is no machine-readable scope list in this document
    because the dependencies, not the security scheme, enforce them."""
    text = json.dumps(op.responses().get("403", {}))
    return sorted(set(re.findall(r"\b((?:groups|audit|admin):[a-z]+)\b", text)))


def curl_for(op: Operation) -> str:
    token = {OWNER: "$CB_QA_OWNER_TOKEN", ADMIN: "$CB_QA_ADMIN_TOKEN"}.get(caller_for(op) or "", "")
    path = op.path.replace("{group_id}", "$CB_QA_GROUP")
    parts = [f'curl -s "$CB_QA_API{path}{query_string(op)}"']
    if op.method != "get":
        parts.insert(0, "")
        parts[0] = f'curl -s -X {op.method.upper()} "$CB_QA_API{path}"'
        parts = parts[:1]
    if token:
        parts.append(f'-H "Authorization: Bearer {token}"')
    return " \\\n     ".join(parts) + " | jq"


def render_reference(spec: dict[str, Any]) -> str:
    info = spec.get("info", {})
    tag_notes = {str(t.get("name")): str(t.get("description", "")) for t in spec.get("tags", [])}
    ops = operations(spec)
    by_tag: dict[str, list[Operation]] = {}
    for op in ops:
        by_tag.setdefault(op.tag or "other", []).append(op)

    nav, body = [], []
    for tag, group in by_tag.items():
        anchor = re.sub(r"[^a-z0-9]+", "-", tag.lower())
        nav.append(f"<h2>{esc(tag)}</h2>")
        section = [f'<section class="tag" id="{anchor}"><h2>{esc(tag)}</h2>']
        if tag_notes.get(tag):
            section.append(f'<div class="desc lede">{paragraphs(tag_notes[tag])}</div>')
        for op in group:
            slug = re.sub(r"[^a-z0-9]+", "-", f"{op.method}-{op.path}".lower()).strip("-")
            needle = f"{op.method} {op.path} {op.body.get('summary', '')}".lower()
            nav.append(
                f'<a href="#{slug}" data-find="{esc(needle)}">'
                f"<code>{esc(op.method.upper())}</code> {esc(op.path)}</a>"
            )
            section.append(render_operation(op, spec, slug, needle))
        section.append("</section>")
        body.append("".join(section))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(info.get("title", "API"))} — reference</title>
<style>{_CSS}</style></head><body>
<div class="wrap">
<nav><input id="find" type="search" placeholder="filter endpoints…" autocomplete="off">
{"".join(nav)}</nav>
<main>
<header class="top">
<h1>{esc(info.get("title", "API"))} <span class="pill">v{esc(info.get("version", ""))}</span></h1>
<p class="lede">{esc(info.get("summary", ""))}</p>
<p class="lede">Generated from <code>/openapi.json</code> by
<code>python scripts/cb.py api-docs</code>. Every shape here is the one the
service actually serves — FastAPI derives both from the same annotations.
The variables in the examples come from <code>uv run scripts/qa_setup.py env</code>.</p>
</header>
{"".join(body)}
</main></div>
<script>{_JS}</script>
</body></html>
"""


def render_operation(op: Operation, spec: dict[str, Any], slug: str, needle: str) -> str:
    parts = [
        f'<section class="op" id="{slug}" data-find="{esc(needle)}">',
        f'<h3><span class="m {op.method}">{esc(op.method.upper())}</span>'
        f'<span class="p">{esc(op.path)}</span>',
    ]
    if op.secured:
        parts.append('<span class="pill">bearer</span>')
    for scope in scopes_of(op):
        parts.append(f'<span class="pill scope">{esc(scope)}</span>')
    parts.append("</h3>")
    if op.body.get("summary"):
        parts.append(f'<div class="sum">{esc(op.body["summary"])}</div>')
    if op.body.get("description"):
        parts.append(f'<div class="desc">{paragraphs(op.body["description"])}</div>')

    parameters = op.body.get("parameters") or []
    if parameters:
        rows = "".join(
            f'<tr><td class="k">{esc(p.get("name"))}'
            f"{' <span class=req>required</span>' if p.get('required') else ''}</td>"
            f'<td class="t">{esc(p.get("in"))} · {esc(type_of(p.get("schema") or {}))}</td>'
            f"<td>{esc(p.get('description', ''))}</td></tr>"
            for p in parameters
        )
        parts.append(
            "<h4>parameters</h4><table><thead><tr><th>name</th><th>in · type</th>"
            f"<th></th></tr></thead><tbody>{rows}</tbody></table>"
        )

    request = ((op.body.get("requestBody") or {}).get("content") or {}).get("application/json")
    if request:
        name = ref_name(request.get("schema") or {})
        parts.append("<h4>request body</h4>")
        parts.append(
            fields_table(name, spec)
            if name
            else f"<pre>{esc(json.dumps(request.get('schema', {}), indent=2))}</pre>"
        )

    success_name = ref_name(response_schema(op.success()))
    if success_name:
        parts.append("<h4>response</h4>")
        parts.append(fields_table(success_name, spec))

    others = {
        status: response.get("description", "")
        for status, response in sorted(op.responses().items())
        if not status.startswith("2")
    }
    if others:
        rows = "".join(
            f'<tr><td class="k">{esc(status)}</td><td>{esc(text)}</td></tr>'
            for status, text in others.items()
        )
        parts.append(
            f"<h4>refusals</h4><table><thead><tr><th>status</th><th></th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    parts.append(f"<h4>try it</h4><pre>{esc(curl_for(op))}</pre>")
    parts.append("</section>")
    return "".join(parts)


# ------------------------------------------------------------------ commands


def artifacts(spec: dict[str, Any]) -> dict[Path, str]:
    return {
        SPEC_FILE: json.dumps(spec, indent=2, sort_keys=True) + "\n",
        REFERENCE_FILE: render_reference(spec),
    }


def command_generate(args: argparse.Namespace) -> int:
    spec = load_spec()
    findings = lint(spec)
    if findings and not args.check:
        print("refusing to generate from an underdocumented spec — run `api-lint` first\n")
        return report(findings, len(operations(spec)))

    stale: list[Path] = []
    for path, content in artifacts(spec).items():
        current = path.read_text() if path.exists() else None
        if current == content:
            continue
        stale.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    if args.check:
        if stale:
            print("these generated files are stale — run `python scripts/cb.py api-docs`:")
            for path in stale:
                print(f"  {path.relative_to(ROOT)}")
            return 1
        print("the published spec and the reference page are both current")
        return 0

    for path in stale:
        print(f"wrote {path.relative_to(ROOT)}")
    print(f"{len(stale)} file(s) changed; the reference is {REFERENCE_FILE.relative_to(ROOT)}")
    return 0


def command_lint(_: argparse.Namespace) -> int:
    spec = load_spec()
    return report(lint(spec), len(operations(spec)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("lint", help="fail on an undocumented endpoint").set_defaults(
        handler=command_lint
    )
    generate = subparsers.add_parser("generate", help="write the published spec and the reference")
    generate.add_argument("--check", action="store_true", help="fail if anything is stale")
    generate.set_defaults(handler=command_generate)
    args = parser.parse_args()
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
