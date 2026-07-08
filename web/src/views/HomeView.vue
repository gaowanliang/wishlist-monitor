<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import EChart from '../components/EChart.vue'
import echarts from '../echarts'

const C = {
  green: '#4a7c2f',
  gold: '#c9a227',
  deep: '#2d5016',
  rose: '#b0603f',
  plum: '#8a6d9e',
  teal: '#3f7d74',
}

// shared Art Nouveau chart theme (parchment + gold filigree + foliage)
const CHART = {
  ink: '#4a3f24',
  muted: '#6b5d33',
  axis: 'rgba(201,162,39,0.32)',
  split: 'rgba(74,60,20,0.09)',
  tipBg: 'rgba(245,240,225,0.97)',
  tipBorder: 'rgba(201,162,39,0.55)',
  font: '"EB Garamond", Georgia, serif',
}

const games = ref([])
const chart = ref([])
const countries = ref([])
const history = ref([])
const selectedId = ref(null)
const range = ref('1m')
const loading = ref(true)
const busy = ref(false)
const message = ref('')

const ranges = [
  ['7d', '7D'],
  ['1m', '1M'],
  ['3m', '3M'],
  ['1y', '1Y'],
  ['all', 'ALL'],
]

const selectedGame = computed(() => games.value.find((g) => g.app_id === selectedId.value) || games.value[0] || null)

function num(value) {
  return Number(value || 0)
}

function formatNumber(value) {
  return new Intl.NumberFormat('en-US').format(num(value))
}

function formatCompact(value) {
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(num(value))
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// ---- derived single-game metrics ----
const latestPoint = computed(() => chart.value.at(-1) || null)
const netToday = computed(() => (latestPoint.value ? num(latestPoint.value.adds) - num(latestPoint.value.deletes) : 0))

const funnel = computed(() => {
  const g = selectedGame.value || {}
  const adds = num(g.total_adds)
  const retained = Math.max(adds - num(g.total_deletes), 0)
  const current = Math.max(num(g.current_wishlists), 0)
  const purchases = num(g.total_purchases)
  const base = Math.max(adds, 1)
  return [
    { key: 'adds', label: '累计新增', value: adds, pct: 100, tone: 'cyan' },
    { key: 'retained', label: '删除后留存', value: retained, pct: (retained / base) * 100, tone: 'blue' },
    { key: 'current', label: '当前愿望单', value: current, pct: (current / base) * 100, tone: 'violet' },
    { key: 'purchases', label: '完成购买', value: purchases, pct: (purchases / base) * 100, tone: 'amber' },
  ]
})

const conversionRate = computed(() => {
  const g = selectedGame.value || {}
  const adds = num(g.total_adds)
  if (!adds) return 0
  return Math.min(100, (num(g.total_purchases) / adds) * 100)
})

const topCountries = computed(() => countries.value.slice(0, 8))
const countryPeak = computed(() => Math.max(...topCountries.value.map((c) => num(c.adds)), 1))

const platform = computed(() => {
  const g = selectedGame.value || {}
  return [
    { name: 'Windows', value: num(g.adds_windows), color: C.green },
    { name: 'macOS', value: num(g.adds_mac), color: C.plum },
    { name: 'Linux', value: num(g.adds_linux), color: C.gold },
  ]
})
const platformTotal = computed(() => platform.value.reduce((a, p) => a + p.value, 0))

// ---- echarts options ----
function grad(from, to, vertical = true) {
  return new echarts.graphic.LinearGradient(0, vertical ? 0 : 0, vertical ? 0 : 1, vertical ? 1 : 0, [
    { offset: 0, color: from },
    { offset: 1, color: to },
  ])
}

const netFlowOption = computed(() => {
  const points = chart.value
  const labels = points.map((p, i) => {
    const raw = p.label || p.date
    if (!raw) return `#${i + 1}`
    const d = new Date(raw)
    return Number.isNaN(d.getTime()) ? String(raw).slice(5, 10) : d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  })
  const net = points.map((p) => num(p.adds) - num(p.deletes))
  let running = 0
  const cumulative = net.map((v) => (running += v))
  return {
    backgroundColor: 'transparent',
    grid: { left: 6, right: 6, top: 30, bottom: 6, containLabel: true },
    legend: {
      top: 0,
      right: 0,
      itemWidth: 16,
      itemHeight: 4,
      textStyle: { color: CHART.muted, fontSize: 13, fontFamily: CHART.font },
      data: ['单日净值', '期间累计'],
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: CHART.tipBg,
      borderColor: CHART.tipBorder,
      borderWidth: 1,
      textStyle: { color: CHART.ink, fontFamily: CHART.font },
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: CHART.axis } },
      axisTick: { show: false },
      axisLabel: { color: CHART.muted, fontSize: 12, fontFamily: CHART.font, hideOverlap: true },
    },
    yAxis: [
      { type: 'value', splitLine: { lineStyle: { color: CHART.split } }, axisLabel: { color: CHART.muted, fontSize: 12, fontFamily: CHART.font } },
      { type: 'value', splitLine: { show: false }, axisLabel: { color: CHART.muted, fontSize: 12, fontFamily: CHART.font } },
    ],
    series: [
      {
        name: '单日净值',
        type: 'bar',
        barMaxWidth: 20,
        data: net.map((v) => ({
          value: v,
          itemStyle: {
            borderRadius: [6, 6, 0, 0],
            color: v >= 0 ? grad('#6fae4a', 'rgba(74,124,47,0.2)') : grad('rgba(176,96,63,0.25)', '#b0603f'),
          },
        })),
      },
      {
        name: '期间累计',
        type: 'line',
        yAxisIndex: 1,
        smooth: 0.35,
        showSymbol: false,
        lineStyle: { width: 3, color: C.gold, shadowColor: 'rgba(201,162,39,0.5)', shadowBlur: 12 },
        data: cumulative,
      },
    ],
  }
})

const platformOption = computed(() => ({
  backgroundColor: 'transparent',
  tooltip: {
    trigger: 'item',
    backgroundColor: CHART.tipBg,
    borderColor: CHART.tipBorder,
    borderWidth: 1,
    textStyle: { color: CHART.ink, fontFamily: CHART.font },
    formatter: '{b}: {c} ({d}%)',
  },
  series: [
    {
      type: 'pie',
      radius: ['58%', '82%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: false,
      itemStyle: { borderColor: '#f5f0e1', borderWidth: 3 },
      label: { show: false },
      labelLine: { show: false },
      data: platform.value.map((p) => ({ name: p.name, value: p.value, itemStyle: { color: p.color } })),
    },
  ],
}))

const gaugeOption = computed(() => ({
  backgroundColor: 'transparent',
  series: [
    {
      type: 'gauge',
      startAngle: 220,
      endAngle: -40,
      min: 0,
      max: 100,
      radius: '92%',
      center: ['50%', '56%'],
      progress: { show: true, width: 12, roundCap: true, itemStyle: { color: grad('#4a7c2f', '#c9a227', false) } },
      axisLine: { lineStyle: { width: 12, color: [[1, 'rgba(74,60,20,0.12)']] } },
      pointer: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      anchor: { show: false },
      title: { show: false },
      detail: {
        valueAnimation: true,
        offsetCenter: [0, 0],
        formatter: (v) => `${v.toFixed(1)}%`,
        color: '#2d5016',
        fontSize: 32,
        fontFamily: '"Cormorant Garamond", serif',
        fontWeight: 600,
      },
      data: [{ value: Number(conversionRate.value.toFixed(1)) }],
    },
  ],
}))

// ---- data loading ----
async function loadDashboard() {
  loading.value = true
  message.value = ''
  try {
    const payload = await api.wishlist()
    games.value = payload.games || []
    if (!selectedId.value && games.value.length) selectedId.value = games.value[0].app_id
    if (selectedGame.value) await loadGameSignals()
  } catch (error) {
    message.value = error.message
  } finally {
    loading.value = false
  }
}

async function loadGameSignals() {
  const game = selectedGame.value
  if (!game) return
  busy.value = true
  try {
    const [chartPayload, countriesPayload, historyPayload] = await Promise.all([
      api.chart(game.app_id, range.value),
      api.countries(game.app_id, range.value),
      api.history(game.app_id),
    ])
    chart.value = chartPayload.points || []
    countries.value = countriesPayload.countries || []
    history.value = historyPayload.entries || []
  } catch (error) {
    message.value = error.message
  } finally {
    busy.value = false
  }
}

function selectGame(appId) {
  selectedId.value = appId
  loadGameSignals()
}

function changeRange(value) {
  range.value = value
  loadGameSignals()
}

onMounted(loadDashboard)
</script>

<template>
  <section class="deck">
    <div v-if="!selectedGame && !loading" class="lockout">
      <span class="lockout-badge">静待花开</span>
      <h1>暂无展示目标</h1>
      <p>还没有可展示的游戏数据。请在 config.yaml 中配置 steam_api_key 与 games。</p>
    </div>

    <template v-else-if="selectedGame">
      <!-- 电影感封面 Hero -->
      <header class="cover" :style="selectedGame.image_url ? { backgroundImage: `url(${selectedGame.image_url})` } : {}">
        <div class="cover-shade"></div>
        <div class="cover-body">
          <div class="cover-id">
            <span class="eyebrow"><i class="tick"></i>Wishlist Intelligence · 应用 #{{ selectedGame.app_id }}</span>
            <h1>{{ selectedGame.name }}</h1>
            <div class="cover-tags">
              <span class="ptag" :class="{ off: !num(selectedGame.adds_windows) }">Windows</span>
              <span class="ptag" :class="{ off: !num(selectedGame.adds_mac) }">macOS</span>
              <span class="ptag" :class="{ off: !num(selectedGame.adds_linux) }">Linux</span>
            </div>
          </div>
          <div class="cover-core">
            <span class="core-label">当前愿望单</span>
            <strong class="core-value">{{ formatNumber(selectedGame.current_wishlists) }}</strong>
            <span class="core-delta" :class="netToday >= 0 ? 'up' : 'down'">
              {{ netToday >= 0 ? '▲' : '▼' }} {{ formatNumber(Math.abs(netToday)) }} 近一期净值
            </span>
          </div>
        </div>
        <div v-if="games.length > 1" class="cover-switch">
          <button v-for="g in games" :key="g.app_id" :class="{ active: g.app_id === selectedGame.app_id }" @click="selectGame(g.app_id)">{{ g.name }}</button>
        </div>
      </header>

      <!-- 转化漏斗 -->
      <section class="panel funnel">
        <div class="panel-head">
          <div><span class="hud-label">Conversion Funnel</span><h2>愿望单转化漏斗</h2></div>
        </div>
        <div class="funnel-flow">
          <div v-for="(step, i) in funnel" :key="step.key" class="funnel-step" :class="`t-${step.tone}`">
            <div class="funnel-bar"><i :style="{ width: `${Math.max(step.pct, 3)}%` }"></i></div>
            <div class="funnel-meta">
              <span class="funnel-name">{{ step.label }}</span>
              <b class="funnel-val">{{ formatNumber(step.value) }}</b>
              <em class="funnel-pct">{{ step.pct.toFixed(1) }}%</em>
            </div>
            <span v-if="i < funnel.length - 1" class="funnel-arrow">▶</span>
          </div>
        </div>
      </section>

      <!-- 转化率仪表盘 -->
      <aside class="panel gauge">
        <span class="hud-label">Purchase Rate</span>
        <EChart :option="gaugeOption" height="180px" />
        <div class="gauge-foot">
          <div><span>购买</span><b>{{ formatCompact(selectedGame.total_purchases) }}</b></div>
          <div><span>新增</span><b>{{ formatCompact(selectedGame.total_adds) }}</b></div>
          <div><span>赠礼</span><b>{{ formatCompact(selectedGame.total_gifts) }}</b></div>
        </div>
      </aside>

      <!-- 净流量趋势 -->
      <section class="panel netflow">
        <div class="panel-head">
          <div><span class="hud-label">Net Flow</span><h2>净流量趋势</h2></div>
          <div class="segmented">
            <button v-for="item in ranges" :key="item[0]" :class="{ active: range === item[0] }" @click="changeRange(item[0])">{{ item[1] }}</button>
          </div>
        </div>
        <div class="chart-wrap" :class="{ muted: busy }">
          <EChart :option="netFlowOption" height="300px" />
          <div v-if="!chart.length" class="panel-empty">暂无趋势数据</div>
        </div>
      </section>

      <!-- 平台环形 -->
      <aside class="panel platform">
        <span class="hud-label">Platform Split</span>
        <div class="donut-wrap">
          <EChart :option="platformOption" height="200px" />
          <div class="donut-center">
            <b>{{ formatCompact(platformTotal) }}</b>
            <span>平台新增</span>
          </div>
        </div>
        <div class="platform-legend">
          <div v-for="p in platform" :key="p.name"><i :style="{ background: p.color }"></i>{{ p.name }}<b>{{ formatNumber(p.value) }}</b></div>
        </div>
      </aside>

      <!-- 地区排行 -->
      <section class="panel geo">
        <span class="hud-label">Region Leaderboard</span>
        <div class="geo-list">
          <div v-for="(country, index) in topCountries" :key="country.country_code" class="geo-row">
            <span class="rank" :class="`r${index + 1}`">{{ index + 1 }}</span>
            <span class="geo-code">{{ country.country_code || 'N/A' }}</span>
            <span class="geo-bar"><i :style="{ width: `${(num(country.adds) / countryPeak) * 100}%` }"></i></span>
            <b>{{ formatNumber(country.adds) }}</b>
          </div>
          <div v-if="!topCountries.length" class="panel-empty static">暂无国家数据</div>
        </div>
      </section>

      <!-- 异常活动日志 -->
      <section class="panel logbook">
        <span class="hud-label">Activity Log</span>
        <div class="log-list">
          <div v-for="entry in history" :key="entry.snapshot_id" class="log-row" :class="{ alert: entry.is_anomaly }">
            <span class="log-time">{{ formatDate(entry.fetched_at || entry.date) }}</span>
            <b class="pos">+{{ formatNumber(entry.adds) }}</b>
            <b class="neg">-{{ formatNumber(entry.deletes) }}</b>
            <b class="buy">{{ formatNumber(entry.purchases) }} 购买</b>
            <i v-if="entry.is_anomaly">⚠ 异常</i>
          </div>
          <div v-if="!history.length" class="panel-empty static">暂无快照</div>
        </div>
      </section>

      <p v-if="message" class="notice deck-notice">{{ message }}</p>
    </template>
  </section>
</template>
