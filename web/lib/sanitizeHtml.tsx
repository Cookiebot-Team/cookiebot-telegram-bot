// Renders the tiny slice of Telegram's `parse_mode=HTML` our handlers use
// (`<b>`, `<i>`, `<code>`, `<blockquote>`) as real React elements — WITHOUT
// ever calling `dangerouslySetInnerHTML`.
//
// The approach: find only literal occurrences of those four tag names (no
// attributes, case-insensitive) with a regex, and use them to split the
// string into a tree of { tag, children } nodes. Every other span of text —
// including anything that merely *looks* like a tag ("<script>", "<a href>",
// a stray "<b class=x>") — is left as plain string content. React renders
// string children as text nodes, escaping them automatically, so none of it
// is ever interpreted as markup. There is no code path here that parses or
// injects raw HTML.

import type { ReactNode } from "react";
import { Fragment } from "react";

const ALLOWED_TAGS = ["b", "i", "code", "blockquote"] as const;
type AllowedTag = (typeof ALLOWED_TAGS)[number];

const TAG_PATTERN = new RegExp(`<(/?)(${ALLOWED_TAGS.join("|")})>`, "gi");

type Token = { type: "text"; value: string } | { type: "open" | "close"; tag: AllowedTag };

function tokenize(input: string): Token[] {
  const tokens: Token[] = [];
  let lastIndex = 0;
  TAG_PATTERN.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = TAG_PATTERN.exec(input)) !== null) {
    if (match.index > lastIndex) tokens.push({ type: "text", value: input.slice(lastIndex, match.index) });
    const tag = match[2].toLowerCase() as AllowedTag;
    tokens.push({ type: match[1] === "/" ? "close" : "open", tag });
    lastIndex = TAG_PATTERN.lastIndex;
  }
  if (lastIndex < input.length) tokens.push({ type: "text", value: input.slice(lastIndex) });
  return tokens;
}

// Telegram's HTML mode requires the source to escape `&`, `<`, `>` outside of
// tags; decode just those back to literal characters for display. This is a
// fixed substitution table, not a parser — it cannot turn text into markup.
function decodeEntities(text: string): string {
  return text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;|&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

interface TreeNode {
  tag: AllowedTag | null;
  children: (TreeNode | string)[];
}

function buildTree(tokens: Token[]): TreeNode {
  const root: TreeNode = { tag: null, children: [] };
  const stack: TreeNode[] = [root];

  for (const token of tokens) {
    const top = stack[stack.length - 1];
    if (token.type === "text") {
      if (token.value.length > 0) top.children.push(decodeEntities(token.value));
      continue;
    }
    if (token.type === "open") {
      const node: TreeNode = { tag: token.tag, children: [] };
      top.children.push(node);
      stack.push(node);
      continue;
    }
    // Closing tag: find a matching open ancestor. Bot output should always be
    // well-formed, but a stray/mismatched close must not throw — treat it as
    // literal text instead.
    const matchDepth = [...stack].reverse().findIndex((node) => node.tag === token.tag);
    if (matchDepth === -1) {
      top.children.push(`</${token.tag}>`);
      continue;
    }
    for (let i = 0; i <= matchDepth; i++) stack.pop();
  }

  return root;
}

function renderNode(node: TreeNode, key: string): ReactNode {
  const children = node.children.map((child, index) =>
    typeof child === "string" ? child : renderNode(child, `${key}.${index}`),
  );
  switch (node.tag) {
    case "b":
      return <b key={key}>{children}</b>;
    case "i":
      return <i key={key}>{children}</i>;
    case "code":
      return (
        <code key={key} className="rounded bg-black/25 px-1 py-0.5 font-mono text-[0.9em]">
          {children}
        </code>
      );
    case "blockquote":
      return (
        <blockquote key={key} className="my-1 border-l-2 border-tg-accent/60 pl-2 text-tg-text/90">
          {children}
        </blockquote>
      );
    default:
      return <Fragment key={key}>{children}</Fragment>;
  }
}

/** Turn bot/user message text into safe React nodes, honouring only
 * `<b>`/`<i>`/`<code>`/`<blockquote>`. Wrap the result in an element with
 * `whitespace-pre-wrap` so the source's newlines still read as line breaks. */
export function renderInlineHtml(text: string): ReactNode {
  const tree = buildTree(tokenize(text));
  return renderNode(tree, "root");
}
