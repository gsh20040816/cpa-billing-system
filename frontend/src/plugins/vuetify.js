import { createVuetify } from 'vuetify'

export default createVuetify({
  theme: {
    defaultTheme: 'cpaLight',
    themes: {
      cpaLight: {
        dark: false,
        colors: {
          background: '#F4F7F6',
          surface: '#FFFFFF',
          'surface-variant': '#E7ECEA',
          primary: '#006C67',
          secondary: '#315EA8',
          tertiary: '#A85D00',
          error: '#B3261E',
          success: '#1D765F',
          warning: '#A85D00',
          info: '#315EA8',
          'on-background': '#202427',
          'on-surface': '#202427',
        },
      },
    },
  },
  defaults: {
    VBtn: { rounded: 'sm', variant: 'flat' },
    VCard: { rounded: 'sm', elevation: 0 },
    VTextField: { density: 'compact', variant: 'outlined', hideDetails: 'auto' },
    VSelect: { density: 'compact', variant: 'outlined', hideDetails: 'auto' },
    VAutocomplete: { density: 'compact', variant: 'outlined', hideDetails: 'auto' },
    VTextarea: { density: 'compact', variant: 'outlined', hideDetails: 'auto' },
    VDataTable: { density: 'compact' },
    VDataTableServer: { density: 'compact' },
    VChip: { rounded: 'sm', size: 'small' },
  },
})
