import '@mdi/font/css/materialdesignicons.css'
import './style.css'

import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { initColorMode } from './lib/colorMode'
import { router } from './router'

initColorMode()
createApp(App).use(createPinia()).use(router).mount('#app')
