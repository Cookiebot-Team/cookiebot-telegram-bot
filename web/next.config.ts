import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The sandbox server is the only backend. Proxying keeps the browser on one
  // origin, so no CORS preflight sits between a click and the bot.
  async rewrites() {
    const target = process.env.SANDBOX_URL ?? "http://127.0.0.1:8083";
    return [{ source: "/api/:path*", destination: `${target}/api/:path*` }];
  },
};

export default nextConfig;
