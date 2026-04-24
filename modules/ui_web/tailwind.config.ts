import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// Dark theme is the default (see index.css), light is supported via the
// `light` class on <html>. Colors use CSS variables so shadcn primitives
// inherit from a single source of truth.
export default {
  darkMode: ["class", "[data-theme='dark']"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    container: { center: true, padding: "1rem", screens: { "2xl": "1440px" } },
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        border:      "hsl(var(--border))",
        input:       "hsl(var(--input))",
        ring:        "hsl(var(--ring))",
        background:  "hsl(var(--background))",
        foreground:  "hsl(var(--foreground))",
        primary:     { DEFAULT: "hsl(var(--primary))",     foreground: "hsl(var(--primary-foreground))" },
        secondary:   { DEFAULT: "hsl(var(--secondary))",   foreground: "hsl(var(--secondary-foreground))" },
        muted:       { DEFAULT: "hsl(var(--muted))",       foreground: "hsl(var(--muted-foreground))" },
        accent:      { DEFAULT: "hsl(var(--accent))",      foreground: "hsl(var(--accent-foreground))" },
        destructive: { DEFAULT: "hsl(var(--destructive))", foreground: "hsl(var(--destructive-foreground))" },
        card:        { DEFAULT: "hsl(var(--card))",        foreground: "hsl(var(--card-foreground))" },
        popover:     { DEFAULT: "hsl(var(--popover))",     foreground: "hsl(var(--popover-foreground))" },

        // Bus module colors (see docs/ui/web_ui_overview_v1.md "Цветовая разметка модулей").
        // Consumed via `bg-module-telegram` / `text-module-history` etc.
        module: {
          telegram:       "hsl(var(--module-telegram))",
          history:        "hsl(var(--module-history))",
          transcription:  "hsl(var(--module-transcription))",
          description:    "hsl(var(--module-description))",
          auth:           "hsl(var(--module-auth))",
          worker_manager: "hsl(var(--module-worker-manager))",
          autochat:       "hsl(var(--module-autochat))",
        },

        // Status colors — success/error/in_progress/stopped.
        status: {
          success:      "hsl(var(--status-success))",
          error:        "hsl(var(--status-error))",
          in_progress:  "hsl(var(--status-in-progress))",
          stopped:      "hsl(var(--status-stopped))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        "flash-new": {
          "0%":   { backgroundColor: "hsl(var(--accent) / 0.5)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
      animation: {
        "flash-new": "flash-new 800ms ease-out",
      },
    },
  },
  plugins: [animate],
} satisfies Config;
