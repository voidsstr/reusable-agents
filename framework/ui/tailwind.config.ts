import type { Config } from 'tailwindcss'

// Light-theme tokens — informed by Linear / Vercel / Stripe / Notion. White
// surfaces on near-white page bg, subtle slate borders, soft shadows, blue
// accent, semantic status palette with paired bg/fg pairs for chips/pills.

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Page surfaces
        surface: {
          page:    '#f8fafc',  // slate-50 — page bg
          card:    '#ffffff',  // card surface
          subtle:  '#f1f5f9',  // slate-100 — section header / hover bg
          divider: '#e2e8f0',  // slate-200 — borders / dividers
        },
        // Text scale (light theme — primary is dark, surface is light)
        ink: {
          50:  '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',  // muted / placeholder
          500: '#64748b',  // secondary
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',  // primary headings
          950: '#020617',
        },
        // Brand / primary action
        accent: {
          50:  '#eff6ff',
          100: '#dbeafe',
          500: '#3b82f6',
          600: '#2563eb',  // primary
          700: '#1d4ed8',  // primary-hover
        },
        // Status palette — fg/bg pairs for chips + glow color base
        status: {
          'running-fg':   '#1e40af',
          'running-bg':   '#dbeafe',
          'running-glow': '#3b82f6',
          'success-fg':   '#065f46',
          'success-bg':   '#d1fae5',
          'success-glow': '#10b981',
          'failure-fg':   '#991b1b',
          'failure-bg':   '#fee2e2',
          'failure-glow': '#ef4444',
          'blocked-fg':   '#92400e',
          'blocked-bg':   '#fef3c7',
          'blocked-glow': '#f59e0b',
          'starting-fg':  '#5b21b6',
          'starting-bg':  '#ede9fe',
          'starting-glow':'#8b5cf6',
          'idle-fg':      '#475569',
          'idle-bg':      '#f1f5f9',
          'idle-glow':    '#94a3b8',
        },
        // Legacy `glow` aliases retained for components not yet migrated
        glow: {
          running:  '#3b82f6',
          success:  '#10b981',
          failure:  '#ef4444',
          blocked:  '#f59e0b',
          starting: '#8b5cf6',
          idle:     '#94a3b8',
        },
      },
      boxShadow: {
        // Soft cards (Linear / Notion style)
        card:       '0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 3px 0 rgb(15 23 42 / 0.05)',
        'card-hover': '0 4px 12px -2px rgb(15 23 42 / 0.08), 0 2px 4px -1px rgb(15 23 42 / 0.04)',
      },
      animation: {
        'glow-running':  'glow-pulse-light 2s ease-in-out infinite',
        'glow-failure':  'glow-pulse-light 1.4s ease-in-out infinite',
        'glow-blocked':  'glow-pulse-light 3s ease-in-out infinite',
        'glow-starting': 'glow-pulse-light 1.6s ease-in-out infinite',
        'subtle-spin':   'spin 1.6s linear infinite',
      },
      keyframes: {
        // Light-theme glow: bright color halo + colored border that
        // pulses; keep box-shadow small so cards don't overpower.
        'glow-pulse-light': {
          '0%, 100%': {
            'border-color': 'rgb(var(--glow-color) / 0.45)',
            'box-shadow':
              '0 0 0 1px rgb(var(--glow-color) / 0.15), ' +
              '0 4px 12px -2px rgb(var(--glow-color) / 0.18)',
          },
          '50%': {
            'border-color': 'rgb(var(--glow-color) / 0.95)',
            'box-shadow':
              '0 0 0 2px rgb(var(--glow-color) / 0.25), ' +
              '0 6px 18px -2px rgb(var(--glow-color) / 0.30)',
          },
        },
      },
      fontFamily: {
        sans: [
          'Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI',
          'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif',
        ],
      },
    },
  },
  plugins: [],
} satisfies Config
