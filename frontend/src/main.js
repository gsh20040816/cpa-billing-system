import '@fontsource-variable/ibm-plex-sans'
import '@fontsource/jetbrains-mono/400.css'
import '@mdi/font/css/materialdesignicons.css'
import 'vuetify/styles'
import './styles/main.css'

import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import vuetify from './plugins/vuetify'

createApp(App).use(router).use(vuetify).mount('#app')
