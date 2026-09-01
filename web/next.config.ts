import type { NextConfig } from "next";

// connect-src 'self' https: already covers production (NEXT_PUBLIC_API_URL
// is same-origin, served over HTTPS behind Traefik - see docker-compose.prod.yml).
// Local dev's default http://localhost:8010 is neither, so it's explicitly
// added here - otherwise the browser silently blocks every API call in dev
// with a bare "Failed to fetch", no console detail pointing at the CSP.
const apiOrigin = new URL(process.env.NEXT_PUBLIC_API_URL || "http://localhost:8010", "http://localhost").origin;

const securityHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
  { key: "Content-Security-Policy", value: `default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https: ${apiOrigin}; object-src 'none'; base-uri 'self'; frame-ancestors 'none'` },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" }
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  }
};

export default nextConfig;
