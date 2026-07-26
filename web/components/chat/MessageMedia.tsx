"use client";

// The picture, actually shown.
//
// The sandbox stores real bytes for anything a tester attaches or the bot
// uploads (`cb_sandbox/files.py`), so a photo in the timeline is the photo,
// not a grey box with an emoji in it. That difference is the whole reason
// image-related features are validatable here at all: "did the bot delete the
// newcomer's photo", "did it read the caption off the right image", "is the
// sticker the one the flood filter counted" are questions you answer by
// looking, and a placeholder answers none of them.
//
// Three renderings, in order of what the message actually is:
//
//   real bytes    an <img>/<video>/<audio>, sized from the file's own
//                 dimensions so a tall image doesn't blow out the bubble
//   no bytes      a labelled placeholder card — "Photo · contents not stored
//                 here". Deliberately NOT a broken image: a bot re-sending a
//                 file_id minted by production is behaving correctly, and the
//                 UI must not make correct behaviour look like a failure
//   dice          the roll, which has no file at all: it is a number the
//                 server generated, and showing it is showing the whole thing
//
// Stickers render without a bubble background, as Telegram does — a sticker
// in a tinted rounded rectangle reads as an image attachment, which is a
// different thing from a sticker, and telling them apart matters to anyone
// testing a sticker-flood rule.

import { useState } from "react";
import type { MediaKind, SandboxMessage } from "@/types";
import { fileUrl } from "@/lib/api";

const MEDIA_LABEL: Record<MediaKind, string> = {
  photo: "Photo",
  sticker: "Sticker",
  video: "Video",
  animation: "GIF",
  document: "Document",
  audio: "Audio",
  voice: "Voice",
  dice: "Dice",
};

const MEDIA_ICON: Record<MediaKind, string> = {
  photo: "🖼",
  sticker: "🙂",
  video: "🎬",
  animation: "🎞",
  document: "📄",
  audio: "🎵",
  voice: "🎤",
  dice: "🎲",
};

/** Longest side of a rendered image, in px. Large enough to judge an image
 * by, small enough that a full-resolution upload doesn't take over the
 * timeline — and it is a CSS cap only, so the bytes the bot received are
 * still the full ones. */
const MAX_EDGE = 320;
const STICKER_EDGE = 160;

function Placeholder({ kind, note }: { kind: MediaKind; note?: string }) {
  return (
    <div className="flex h-28 w-48 max-w-full flex-col items-center justify-center gap-1 rounded-md bg-black/20 text-tg-muted">
      <span className="text-3xl">{MEDIA_ICON[kind]}</span>
      <span className="text-xs">{MEDIA_LABEL[kind]}</span>
      {note && <span className="px-2 text-center text-[10px] leading-tight">{note}</span>}
    </div>
  );
}

function Dice({ message }: { message: SandboxMessage }) {
  // `media_extra` is not on the web-facing message shape (it is server-side
  // detail), so the roll's value is not available to render — but the emoji a
  // dice message carries is the die itself, and showing it large is the
  // honest rendering of "the bot rolled something".
  return (
    <div className="flex h-24 w-24 items-center justify-center rounded-md bg-black/10 text-5xl">
      🎲
    </div>
  );
}

export default function MessageMedia({ message }: { message: SandboxMessage }) {
  const [failed, setFailed] = useState(false);
  const kind = message.media;
  if (!kind) return null;
  if (kind === "dice") return <Dice message={message} />;

  const fileId = message.media_file_id;
  if (!fileId) {
    return (
      <Placeholder
        kind={kind}
        note="no bytes stored — sent by file_id or seeded"
      />
    );
  }
  if (failed) {
    return <Placeholder kind={kind} note="stored file could not be loaded" />;
  }

  const src = fileUrl(fileId);
  const isSticker = kind === "sticker";
  const edge = isSticker ? STICKER_EDGE : MAX_EDGE;

  if (kind === "photo" || kind === "sticker" || kind === "animation") {
    return (
      // A plain <img>, not next/image: the bytes are served by the sandbox on
      // the same origin with an immutable cache header, and next/image's
      // optimiser would add a resize round trip that can only make a local
      // workbench slower and its rendering less faithful.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={src}
        alt={MEDIA_LABEL[kind]}
        onError={() => setFailed(true)}
        style={{ maxWidth: edge, maxHeight: edge }}
        className={isSticker ? "object-contain" : "rounded-md object-contain"}
      />
    );
  }

  if (kind === "video") {
    return (
      <video
        src={src}
        controls
        onError={() => setFailed(true)}
        style={{ maxWidth: MAX_EDGE, maxHeight: MAX_EDGE }}
        className="rounded-md"
      />
    );
  }

  if (kind === "audio" || kind === "voice") {
    return (
      <audio src={src} controls onError={() => setFailed(true)} className="w-56 max-w-full" />
    );
  }

  // A document: a chip that downloads. Rendering the contents would mean
  // guessing at a viewer for every mime type, and "the bot sent a file, here
  // it is" is the whole assertion anyway.
  return (
    <a
      href={src}
      download
      className="flex items-center gap-2 rounded-md bg-black/20 px-3 py-2 text-[13px] hover:bg-black/30"
    >
      <span className="text-2xl">{MEDIA_ICON.document}</span>
      <span className="min-w-0">
        <span className="block truncate">{MEDIA_LABEL[kind]}</span>
        <span className="block text-[11px] text-tg-muted">click to download</span>
      </span>
    </a>
  );
}
