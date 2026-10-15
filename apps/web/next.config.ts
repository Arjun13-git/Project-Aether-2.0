import type { NextConfig } from "next";

// The web app is UI only. All data comes from the FastAPI process via this local proxy
// (ARCHITECTURE §3). The API binds to loopback; nothing here holds secrets.
const API_ORIGIN = process.env.AETHER_API_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
};

export default nextConfig;
