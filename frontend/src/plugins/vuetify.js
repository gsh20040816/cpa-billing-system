import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'

export default createVuetify({
  components,
  directives,
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
          'on-primary': '#FFFFFF',
          secondary: '#315EA8',
          'on-secondary': '#FFFFFF',
          tertiary: '#A85D00',
          'on-tertiary': '#FFFFFF',
          error: '#B3261E',
          'on-error': '#FFFFFF',
          success: '#1D765F',
          'on-success': '#FFFFFF',
          warning: '#A85D00',
          'on-warning': '#FFFFFF',
          info: '#315EA8',
          'on-info': '#FFFFFF',
          'on-background': '#202427',
          'on-surface': '#202427',
          'on-surface-variant': '#3F4946',
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
