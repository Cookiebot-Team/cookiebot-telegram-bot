import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cookiebot Sandbox",
  description: "A local Telegram the real bot talks to",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="h-full antialiased">{children}</body>
    </html>
  );
}
