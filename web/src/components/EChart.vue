<script setup>
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import echarts from '../echarts'

const props = defineProps({
  option: { type: Object, default: () => ({}) },
  height: { type: String, default: '320px' },
})

const el = ref(null)
const chart = shallowRef(null)

function render() {
  if (chart.value && props.option) chart.value.setOption(props.option, true)
}

function resize() {
  chart.value?.resize()
}

onMounted(() => {
  chart.value = echarts.init(el.value, null, { renderer: 'canvas' })
  render()
  window.addEventListener('resize', resize)
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div ref="el" class="echart" :style="{ height }"></div>
</template>

<style scoped>
.echart {
  width: 100%;
}
</style>
