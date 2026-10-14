const path = require("path");

const isProd = process.env.NODE_ENV === "production";
// Origins the browser talks to besides itself: the API, and object storage (presigned
// PDF/image URLs). Set both for real deployments -- see .env.prod.example.
const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
const storageOrigin = process.env.NEXT_PUBLIC_STORAGE_ORIGIN || "http://localhost:9000";
// Bare hostname only -- no protocol, no port (that's all Next dev's origin
// check compares against). Since the API/frontend share one origin behind
// nginx, this is the same host the page is actually served from.
const devOriginHost = apiOrigin.replace(/^https?:\/\//, "").split(":")[0];

// Next's own hydration scripts are inline, so script-src keeps 'unsafe-inline'
// (a nonce-based policy would need a middleware and dynamic rendering for every page).
// Everything else is locked down: no plugins, no framing, no foreign forms or base tags,
// and no third-party script origins at all (the pdf.js worker is bundled, not CDN-loaded).
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isProd ? "" : " 'unsafe-eval'"}`,
  "style-src 'self' 'unsafe-inline'",
  `img-src 'self' data: blob: ${storageOrigin}`,
  "font-src 'self' data:",
  `connect-src 'self' ${apiOrigin} ${storageOrigin}${isProd ? "" : " ws: wss:"}`,
  "worker-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  ...(isProd ? ["upgrade-insecure-requests"] : []),
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), geolocation=(), microphone=(self)" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  ...(isProd ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }] : []),
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Hide the Next.js DevTools “N” badge in development.
  // Real compile/runtime errors still surface — fix those, or run `next start` after build.
  devIndicators: false,
  // Pin the workspace root so a stray lockfile in a parent directory can't change resolution.
  turbopack: { root: path.join(__dirname) },
  // Next dev refuses cross-origin requests by default (DNS-rebinding protection) --
  // without this, the app behind nginx on a real domain/IP stays a blank spinner
  // (requests silently rejected). No-op in production (`next start` ignores this).
  allowedDevOrigins: [devOriginHost],
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

module.exports = nextConfig;
