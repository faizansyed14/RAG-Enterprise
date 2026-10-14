import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        surface: "var(--surface)",
        "surface-raised": "var(--surface-raised)",
        "surface-hover": "var(--surface-hover)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",
        muted: "var(--muted)",
        accent: "var(--accent)",
        "accent-fg": "var(--accent-fg)",
        "accent-soft": "var(--accent-soft)",
        "accent-muted": "var(--accent-muted)",
        success: "var(--success)",
        "success-soft": "var(--success-soft)",
        danger: "var(--danger)",
        "danger-soft": "var(--danger-soft)",
        warning: "var(--warning)",
        "warning-soft": "var(--warning-soft)",
        sidebar: "var(--sidebar)",
        "sidebar-fg": "var(--sidebar-fg)",
        "sidebar-muted": "var(--sidebar-muted)",
        "sidebar-hover": "var(--sidebar-hover)",
        "sidebar-active": "var(--sidebar-active)",
        "sidebar-border": "var(--sidebar-border)",
        composer: "var(--composer)",
        "bubble-user": "var(--bubble-user)",
        "bubble-user-fg": "var(--bubble-user-fg)",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "Georgia", "serif"],
      },
      boxShadow: {
        soft: "var(--shadow-sm)",
        panel: "var(--shadow-md)",
      },
      borderRadius: {
        xl: "var(--radius)",
        "2xl": "var(--radius-lg)",
      },
    },
  },
  plugins: [],
};

export default config;
