// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { describe, expect, it } from 'vitest'
import vuetify from './vuetify'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub

const Harness = defineComponent({
  template: `
    <v-app>
      <v-text-field label="API Key" />
      <v-btn type="submit" color="primary">登录</v-btn>
    </v-app>
  `,
})

describe('Vuetify plugin', () => {
  it('renders registered Material components instead of unknown custom elements', () => {
    const wrapper = mount(Harness, { global: { plugins: [vuetify] } })

    expect(wrapper.find('.v-application').exists()).toBe(true)
    expect(wrapper.find('.v-text-field .v-field').exists()).toBe(true)
    expect(wrapper.find('button.v-btn[type="submit"]').exists()).toBe(true)
    expect(wrapper.find('v-text-field').exists()).toBe(false)
    expect(wrapper.find('v-btn').exists()).toBe(false)
  })
})
