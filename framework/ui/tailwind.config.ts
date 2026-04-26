import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Match the nsc-dashboard / application-research dark palette
        ink: {
          50:  '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
          950: '#020617',
        },
        glow: {
          running:  '#38bdf8',  // cyan
          success:  '#10b981',  // green
          failure:  '#ef4444',  // red
          blocked:  '#f59e0b',  // amber
          starting: '#a855f7',  // purple
          idle:     '#94a3b8',  // gray
        },
      },
      animation: {
        'glow-running':  'glow-pulse 2s ease-in-out infinite',
        'glow-failure':  'glow-pulse 1.2s ease-in-out infinite',
        'glow-blocked':  'glow-pulse 3s ease-in-out infinite',
        'glow-starting': 'glow-pulse 1.5s ease-in-out infinite',
      },
      keyframes: {
        // Same shape as application-research/frontend/src/pages/MarketResearch.tsx
        // — borrowed verbatim, parameterized via CSS vars.
        'glow-pulse': {
          '0%, 100%': {
            'border-color': 'rgb(var(--glow-color) / 0.27)',
            'box-shadow': '0 0 8px rgb(var(--glow-color) / 0.13)',
          },
          '50%': {
            'border-color': 'rgb(var(--glow-color) / 1.0)',
            'box-shadow': '0 0 16px rgb(var(--glow-color) / 0.27)',
          },
        },
      },
    },
  },
  plugins: [],
} satisfies Config
