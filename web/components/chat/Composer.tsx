"use client";

// The text box at the bottom of the conversation. Enter sends (Shift+Enter
// for a newline, matching Telegram Desktop's default); a reply target set
// from a message's hover action shows as a cancellable strip above the
// input; a media-kind row covers every kind `SendMessageRequest.media`
// accepts (`control_api.py`); and a repeat count sends the same thing that
// many times in one click — sticker-spam moderation is only exercisable by
// sending past a flood limit, and retyping a sticker send five times to test
// it is exactly the friction this control removes.
//
// Media can be sent two ways, and both are useful:
//
//   with a file    pick a real picture and the bot receives real bytes with
//                  real dimensions and a real mime type. This is what makes an
//                  image feature testable — a handler that resizes, sniffs,
//                  stores or rejects an image has nothing to act on otherwise.
//   without one    just the kind. Six stickers past a flood limit do not need
//                  to be six actual stickers, and forcing a file picker on
//                  that flow would make the cheap test expensive.
//
// The upload happens once, on pick, not once per send — so a repeat count of
// twenty re-sends the same `file_id`, which is also what real Telegram does
// when a bot forwards a file it already has.

import { useEffect, useRef, useState } from "react";
import type { SandboxFile, SandboxMessage, SandboxUser, SendMediaKind } from "@/types";
import { uploadFile } from "@/lib/api";
import { displayName, truncate } from "@/lib/format";

export interface ComposerSubmission {
  text?: string;
  media?: SendMediaKind;
  mediaFileId?: string;
  mediaCaption?: string;
  anonymous: boolean;
  repeat: number;
}

/** Which media kind a picked file obviously is, so choosing "Photo" and then
 * attaching an mp4 corrects itself instead of sending a video labelled as a
 * photo — a mislabelled attachment would be testing the sandbox's bookkeeping
 * rather than the bot. A `.webp` stays whatever kind is selected: it is both
 * the sticker format and a perfectly ordinary image, and only the tester
 * knows which one they meant. */
function kindForFile(file: File, current: SendMediaKind | null): SendMediaKind {
  const type = file.type || "";
  if (type === "image/gif") return "animation";
  if (type === "image/webp") return current === "sticker" ? "sticker" : "photo";
  if (type.startsWith("image/")) return current === "sticker" ? "sticker" : "photo";
  if (type.startsWith("video/")) return "video";
  if (type.startsWith("audio/")) return current === "voice" ? "voice" : "audio";
  return "document";
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsDataURL(file);
  });
}

interface ComposerProps {
  disabled: boolean;
  disabledReason?: string;
  replyTo: SandboxMessage | null;
  replyToSender?: SandboxUser;
  /** Whether the acting user currently has anonymity switched on for this
   * chat (`Membership.anonymous`) — the "post as the group" toggle only ever
   * appears when it would actually be honoured by the server. */
  canSendAnonymously: boolean;
  onCancelReply: () => void;
  onSend: (submission: ComposerSubmission) => Promise<void> | void;
}

const MEDIA_KINDS: { value: SendMediaKind; label: string; icon: string }[] = [
  { value: "photo", label: "Photo", icon: "🖼" },
  { value: "sticker", label: "Sticker", icon: "🙂" },
  { value: "video", label: "Video", icon: "🎬" },
  { value: "animation", label: "GIF", icon: "🎞" },
  { value: "document", label: "File", icon: "📄" },
  { value: "audio", label: "Audio", icon: "🎵" },
  { value: "voice", label: "Voice", icon: "🎤" },
];

export default function Composer({
  disabled,
  disabledReason,
  replyTo,
  replyToSender,
  canSendAnonymously,
  onCancelReply,
  onSend,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [media, setMedia] = useState<SendMediaKind | null>(null);
  const [attachment, setAttachment] = useState<SandboxFile | null>(null);
  const [attachmentPreview, setAttachmentPreview] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [anonymous, setAnonymous] = useState(false);
  const [repeat, setRepeat] = useState(1);
  const [sending, setSending] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (replyTo) textareaRef.current?.focus();
  }, [replyTo]);

  // The toggle must never silently stay on for a user who can no longer post
  // anonymously (switched chat, switched acting user) — a stale "on" would
  // send as GroupAnonymousBot without the tester meaning to.
  useEffect(() => {
    if (!canSendAnonymously) setAnonymous(false);
  }, [canSendAnonymously]);

  // Switching away from media drops the attachment: a file kept across a
  // switch to Text would silently ride along on the next send.
  useEffect(() => {
    if (media === null) clearAttachment();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- clearAttachment is stable for this component's lifetime
  }, [media]);

  function clearAttachment() {
    setAttachment(null);
    setAttachmentPreview(null);
    setUploadError(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function handlePick(file: File | undefined) {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      const dataUrl = await readAsDataUrl(file);
      const stored = await uploadFile({
        filename: file.name,
        content_type: file.type || undefined,
        data: dataUrl,
      });
      setAttachment(stored);
      // Preview from the local data URL rather than re-fetching the bytes we
      // just sent: same picture, one fewer round trip, and it shows instantly.
      setAttachmentPreview(stored.mime_type.startsWith("image/") ? dataUrl : null);
      setMedia((current) => kindForFile(file, current));
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
      clearAttachment();
    } finally {
      setUploading(false);
    }
  }

  const trimmed = text.trim();
  const canSubmit = media !== null ? true : trimmed.length > 0;

  async function submit() {
    if (!canSubmit || sending || disabled) return;
    setSending(true);
    try {
      await onSend({
        text: media === null ? trimmed : undefined,
        media: media ?? undefined,
        mediaFileId: attachment?.file_id,
        mediaCaption: media !== null && trimmed.length > 0 ? trimmed : undefined,
        anonymous,
        repeat: Math.min(Math.max(1, repeat), 50),
      });
      setText("");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="shrink-0 border-t border-tg-divider bg-tg-panel">
      {replyTo && (
        <div className="flex items-center justify-between gap-2 border-b border-tg-divider px-4 py-1.5 text-[13px]">
          <div className="min-w-0">
            <div className="font-medium text-tg-accent">Reply to {displayName(replyToSender)}</div>
            <div className="truncate text-tg-muted">
              {replyTo.deleted ? "Message deleted" : truncate(replyTo.text ?? replyTo.media_caption ?? "Media", 100)}
            </div>
          </div>
          <button
            type="button"
            onClick={onCancelReply}
            className="shrink-0 rounded px-2 py-1 text-tg-muted hover:bg-tg-hover hover:text-tg-text"
            aria-label="Cancel reply"
          >
            ✕
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 border-b border-tg-divider px-3 py-1.5">
        <button
          type="button"
          onClick={() => setMedia(null)}
          className={`rounded px-2 py-0.5 text-[11px] font-medium ${
            media === null ? "bg-tg-accent text-white" : "bg-tg-hover text-tg-muted hover:text-tg-text"
          }`}
        >
          Text
        </button>
        {MEDIA_KINDS.map((k) => (
          <button
            key={k.value}
            type="button"
            onClick={() => setMedia(k.value)}
            className={`rounded px-2 py-0.5 text-[11px] font-medium ${
              media === k.value ? "bg-tg-accent text-white" : "bg-tg-hover text-tg-muted hover:text-tg-text"
            }`}
          >
            {k.icon} {k.label}
          </button>
        ))}

        {media !== null && (
          <>
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              onChange={(e) => void handlePick(e.target.files?.[0])}
            />
            <button
              type="button"
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
              title="Attach a real file. The bot receives the actual bytes, with real dimensions and a real mime type — which is the only way an image-handling feature has anything to act on."
              className="rounded bg-tg-hover px-2 py-0.5 text-[11px] font-medium text-tg-muted hover:text-tg-text disabled:opacity-50"
            >
              {uploading ? "Uploading…" : attachment ? "Replace file" : "📎 Attach file"}
            </button>
          </>
        )}

        <div className="ml-auto flex items-center gap-2">
          {canSendAnonymously && (
            <label
              className={`flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold ${
                anonymous ? "bg-tg-amber text-black" : "bg-tg-hover text-tg-muted"
              }`}
              title="Post as GroupAnonymousBot instead of this account — requires anonymity already switched on for this member."
            >
              <input type="checkbox" className="hidden" checked={anonymous} onChange={(e) => setAnonymous(e.target.checked)} />
              🕶 anon
            </label>
          )}
          <label className="flex items-center gap-1 text-[11px] text-tg-muted" title="Send this exact message this many times in a row — cheap flood testing.">
            <span>×</span>
            <input
              type="number"
              min={1}
              max={50}
              value={repeat}
              onChange={(e) => setRepeat(Number(e.target.value) || 1)}
              className="w-12 rounded bg-tg-hover px-1 py-0.5 text-center text-tg-text"
            />
          </label>
        </div>
      </div>

      {(attachment || uploadError || (media !== null && !uploading)) && (
        <div className="flex items-center gap-2 border-b border-tg-divider px-3 py-1.5 text-[11px]">
          {attachment ? (
            <>
              {attachmentPreview ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={attachmentPreview}
                  alt=""
                  className="h-10 w-10 shrink-0 rounded object-cover"
                />
              ) : (
                <span className="text-lg">📎</span>
              )}
              <span className="min-w-0 flex-1 truncate text-tg-text/90">
                {attachment.file_name}
                <span className="text-tg-muted">
                  {" "}
                  · {attachment.mime_type} · {Math.max(1, Math.round(attachment.size / 1024))} KB
                  {attachment.width > 0 && ` · ${attachment.width}×${attachment.height}`}
                </span>
              </span>
              <button
                type="button"
                onClick={clearAttachment}
                className="shrink-0 rounded px-1.5 py-0.5 text-tg-muted hover:bg-tg-hover hover:text-tg-text"
              >
                Remove
              </button>
            </>
          ) : uploadError ? (
            <span className="text-tg-red">Upload failed: {uploadError}</span>
          ) : (
            // Said plainly rather than left to be discovered: a media send with
            // no file is a legitimate, useful thing (flood tests), but a tester
            // checking what the bot *does with an image* would otherwise send
            // six empty photos and conclude the feature is broken.
            <span className="text-tg-muted">
              No file attached — the bot will receive a {media} with no contents. Attach one to
              test what it does with the actual image.
            </span>
          )}
        </div>
      )}

      <div className="flex items-end gap-2 px-3 py-2">
        <textarea
          ref={textareaRef}
          value={text}
          disabled={disabled}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }}
          placeholder={
            disabled
              ? (disabledReason ?? "Pick a user and a chat first")
              : media === null
                ? "Type a message — try /rules"
                : "Caption (optional)"
          }
          rows={1}
          className="max-h-32 min-h-[38px] flex-1 resize-none rounded-lg border border-tg-divider bg-tg-in px-3 py-2 text-[14px] text-tg-text placeholder:text-tg-muted focus:border-tg-accent focus:outline-none disabled:opacity-50"
        />
        <button
          type="button"
          disabled={disabled || sending || !canSubmit}
          onClick={() => void submit()}
          className="h-[38px] shrink-0 rounded-lg bg-tg-accent px-4 text-sm font-medium text-white transition-opacity disabled:opacity-40"
        >
          {sending ? "…" : repeat > 1 ? `Send ×${repeat}` : "Send"}
        </button>
      </div>
    </div>
  );
}
