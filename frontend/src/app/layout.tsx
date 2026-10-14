import type { Metadata } from "next";
// Fonts are bundled with the app (no request to Google at build or run time), so the image builds and runs offline.
import "@fontsource-variable/inter/wght.css";
import "@fontsource/cormorant-garamond/latin-400.css";
import "@fontsource/cormorant-garamond/latin-500.css";
import "@fontsource/cormorant-garamond/latin-600.css";
import "../styles/globals.css";

export const metadata: Metadata = {
  title: "ALAIN",
  description: "Document intelligence workspace.",
  icons: {
    icon: [{ url: "/alain-logo.svg", type: "image/svg+xml" }],
    shortcut: "/alain-logo.svg",
    apple: "/alain-logo.svg",
  },
};

/** Applies stored theme (default dark) before first paint to avoid a flash. */
const themeInit = `(function(){try{var t=localStorage.getItem('alain-theme');if(t!=='light'&&t!=='dark')t='dark';var r=document.documentElement;r.classList.toggle('dark',t==='dark');r.classList.toggle('light',t==='light')}catch(e){document.documentElement.classList.add('dark')}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="icon" href="/alain-logo.svg" type="image/svg+xml" />
        <link rel="shortcut icon" href="/alain-logo.svg" />
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
