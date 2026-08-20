import type { NextConfig } from "next";

const isStaticExport = process.env.BUILD_STATIC === "1";

const nextConfig: NextConfig = {
  // Static export for FastAPI serving (BUILD_STATIC=1 npm run build)
  ...(isStaticExport
    ? {
        output: "export",
        images: { unoptimized: true },
        trailingSlash: true,
      }
    : {}),
  // Proxy API calls to FastAPI backend during dev
  // (skipped for static export — API calls go to same origin)
  async rewrites() {
    if (isStaticExport) return [];
    const backend = process.env.KYOURAI_BACKEND || "http://localhost:8000";
    return [
      {
        source: "/v1/:path*",
        destination: `${backend}/v1/:path*`,
      },
      {
        source: "/health",
        destination: `${backend}/health`,
      },
    ];
  },
};

export default nextConfig;
