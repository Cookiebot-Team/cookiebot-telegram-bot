// Renders a Telegram `text` + `entities` pair the way a Telegram client does.
//
// This is the *current* shape of everything the bot sends: the sandbox parses
// `parse_mode=HTML`/`MarkdownV2` markup into plain text plus entities, exactly
// as the real Bot API server does (see `telegram_api._apply_parse_mode`), so a
// message's `text` no longer carries any markup to render. `sanitizeHtml.tsx`
// still exists for messages persisted before that landed, whose stored text
// really does contain literal tags.
//
// Entities nest and overlap (a link inside a bold run is two entities covering
// the same span), so this builds a tree rather than a flat list: entities are
// sorted outermost-first, and each one consumes the entities that fall inside
// it. `offset`/`length` are UTF-16 code units — the same unit JavaScript string
// indices use — so they are applied with `slice()` unconverted.
//
// Nothing here ever produces raw HTML: every node is a React element with
// string children, which React escapes.

import type { ReactNode } from "react";
import { Fragment } from "react";
import type { MessageEntity } from "@/types";

/** Entity types that wrap their span in an element. Anything else — `mention`,
 * `hashtag`, `bot_command`, `phone_number` — is a plain-text run Telegram only
 * highlights; they are rendered as accented text rather than dropped, so a
 * tester can see that the bot's own command echo was recognised as one. */
const WRAPPERS = new Set([
  "bold",
  "italic",
  "underline",
  "strikethrough",
  "spoiler",
  "code",
  "pre",
  "blockquote",
  "expandable_blockquote",
  "text_link",
  "url",
  "text_mention",
  "mention",
  "hashtag",
  "cashtag",
  "bot_command",
  "email",
  "phone_number",
  "custom_emoji",
]);

interface Node {
  entity: MessageEntity;
  children: MessageEntity[];
}

/** Group the entities that start at or after `from` and end at or before `to`
 * into top-level runs plus the entities nested inside each. */
function nest(entities: MessageEntity[], from: number, to: number): Node[] {
  const nodes: Node[] = [];
  let cursor = from;
  for (const entity of entities) {
    const start = entity.offset;
    const end = entity.offset + entity.length;
    if (start < cursor || end > to) continue; // already consumed by an outer node
    nodes.push({
      entity,
      children: entities.filter(
        (candidate) =>
          candidate !== entity &&
          candidate.offset >= start &&
          candidate.offset + candidate.length <= end,
      ),
    });
    cursor = end;
  }
  return nodes;
}

function wrap(entity: MessageEntity, children: ReactNode, key: number): ReactNode {
  switch (entity.type) {
    case "bold":
      return <b key={key} className="font-semibold">{children}</b>;
    case "italic":
      return <i key={key}>{children}</i>;
    case "underline":
      return <u key={key}>{children}</u>;
    case "strikethrough":
      return <s key={key}>{children}</s>;
    case "spoiler":
      // Telegram hides a spoiler until it is clicked; a blur that clears on
      // hover is the closest thing that still reads as "this was hidden".
      return (
        <span key={key} className="rounded bg-black/40 blur-[3px] transition hover:blur-none">
          {children}
        </span>
      );
    case "code":
      return (
        <code key={key} className="rounded bg-black/30 px-1 font-mono text-[13px]">
          {children}
        </code>
      );
    case "pre":
      return (
        <pre key={key} className="my-1 overflow-x-auto rounded bg-black/30 p-2 font-mono text-[12px]">
          <code>{children}</code>
        </pre>
      );
    case "blockquote":
    case "expandable_blockquote":
      return (
        <blockquote key={key} className="my-1 border-l-2 border-tg-accent/70 pl-2 text-tg-text/90">
          {children}
        </blockquote>
      );
    case "text_link":
    case "url":
    case "email":
      // `title` carries the destination: a UAT tester needs to check where a
      // link points without navigating away from the workbench.
      return (
        <span key={key} className="text-tg-accent underline" title={entity.url ?? undefined}>
          {children}
        </span>
      );
    default:
      return (
        <span key={key} className="text-tg-accent">
          {children}
        </span>
      );
  }
}

function build(text: string, entities: MessageEntity[], from: number, to: number): ReactNode[] {
  const out: ReactNode[] = [];
  let cursor = from;
  for (const [index, node] of nest(entities, from, to).entries()) {
    const start = node.entity.offset;
    const end = start + node.entity.length;
    if (start > cursor) out.push(<Fragment key={`t${cursor}`}>{text.slice(cursor, start)}</Fragment>);
    const inner = build(text, node.children, start, end);
    out.push(
      WRAPPERS.has(node.entity.type) ? wrap(node.entity, inner, index) : (
        <Fragment key={index}>{inner}</Fragment>
      ),
    );
    cursor = end;
  }
  if (cursor < to) out.push(<Fragment key={`t${cursor}`}>{text.slice(cursor, to)}</Fragment>);
  return out;
}

export function renderEntities(text: string, entities: MessageEntity[] | undefined): ReactNode {
  if (!entities || entities.length === 0) return text;
  // Outermost first, so a bold run that contains a link is walked before the
  // link it contains. Ties on offset go to the longer span for the same reason.
  const sorted = [...entities].sort((a, b) => a.offset - b.offset || b.length - a.length);
  return <>{build(text, sorted, 0, text.length)}</>;
}
