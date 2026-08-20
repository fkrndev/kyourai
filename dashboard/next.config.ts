import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Proxy API calls to FastAPI backend during dev
  // In production, set NEXT_PUBLIC_API_BASE to the backend URL
  async rewrites() {
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
