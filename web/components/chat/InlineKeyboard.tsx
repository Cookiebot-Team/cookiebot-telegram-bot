"use client";

// Real, clickable buttons for a message's `reply_markup.inline_keyboard` —
// the whole point of the sandbox is that pressing one drives the actual bot
// handler, not a mock. Each button tracks its own pending state so a click
// visibly "settles" (spinner clears) as soon as the callback round-trip
// finishes, whether it succeeded or failed, independent of whatever the SSE
// stream does afterwards.

import { useState } from "react";
import type { InlineButton } from "@/types";

interface InlineKeyboardProps {
  rows: InlineButton[][];
  disabled?: boolean;
  onPress: (button: InlineButton) => Promise<void> | void;
}

export default function InlineKeyboard({ rows, disabled, onPress }: InlineKeyboardProps) {
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);

  if (rows.length === 0) return null;

  async function handleClick(button: InlineButton, key: string) {
    if (disabled || pendingKey !== null) return;
    setPendingKey(key);
    setFailedKey(null);
    try {
      await onPress(button);
    } catch {
      setFailedKey(key);
    } finally {
      setPendingKey(null);
    }
  }

  return (
    <div className="mt-1.5 flex flex-col gap-1">
      {rows.map((row, rowIndex) => (
        <div key={rowIndex} className="flex gap-1">
          {row.map((button, colIndex) => {
            const key = `${rowIndex}.${colIndex}.${button.text}`;
            const pending = pendingKey === key;
            const failed = failedKey === key;
            const commonClasses =
              "flex-1 truncate rounded-md border px-2.5 py-1.5 text-center text-[13px] leading-tight transition-colors";

            if (button.url && !button.callback_data) {
              return (
                <a
                  key={key}
                  href={button.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className={`${commonClasses} border-tg-accent/30 bg-tg-panel text-tg-accent hover:bg-tg-hover`}
                  title={button.url}
                >
                  {button.text} ↗
                </a>
              );
            }

            return (
              <button
                key={key}
                type="button"
                disabled={disabled || pending}
                onClick={() => void handleClick(button, key)}
                className={`${commonClasses} ${
                  failed
                    ? "border-tg-red/50 bg-tg-red/10 text-tg-red"
                    : "border-tg-accent/30 bg-tg-panel text-tg-accent hover:bg-tg-hover"
                } disabled:cursor-wait disabled:opacity-60`}
              >
                {pending ? (
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 animate-spin rounded-full border-2 border-tg-accent/40 border-t-tg-accent" />
                    {button.text}
                  </span>
                ) : failed ? (
                  `${button.text} (failed — retry)`
                ) : (
                  button.text
                )}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
