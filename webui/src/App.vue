<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'

// ── 状态 ──────────────────────────────────────────────
const state = reactive({
  enabled: false,
  frames: 0,
  hotspot: [24, 28],
  verdict: null,        // 适合 | 有风险 | 不适合
  score: null,
  issues: [],
  previews: [],         // base64 PNG 列表（每帧一张）
  srcSize: null,
})

const uploading = ref(false)
const busy = ref(false)
const error = ref('')
const dragging = ref(false)
const confirm = reactive({ show: false, title: '', body: [], action: null })

// 预览动画（多帧时轮播）
let animTimer = null
const animIndex = ref(0)
const previewCount = computed(() => state.previews.length)
const currentPreview = computed(() =>
  state.previews.length ? state.previews[animIndex.value % state.previews.length] : null)

function startAnim() {
  stopAnim()
  if (state.previews.length > 1) {
    animTimer = setInterval(() => { animIndex.value = (animIndex.value + 1) % state.previews.length }, 150)
  }
}
function stopAnim() { if (animTimer) { clearInterval(animTimer); animTimer = null } }

// ── API ───────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, opts)
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try { const j = await res.json(); if (j.error) msg = j.error } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json()
}

// ── 初始状态 ──────────────────────────────────────────
onMounted(async () => {
  // 前端运行时错误显示到界面（便于排查）
  window.addEventListener('error', (e) => {
    error.value = `前端错误: ${e.message}`
  })
  try {
    const s = await api('/api/state')
    Object.assign(state, s)
    if (s.frames > 0) {
      // 恢复上次的图片预览（服务端暂存的帧）
      const p = await api('/api/previews')
      state.previews = p.previews
      animIndex.value = 0
      startAnim()
    }
  } catch (e) {
    error.value = `无法连接后端服务: ${e.message}`
  }
})
onBeforeUnmount(stopAnim)

// ── 上传 ──────────────────────────────────────────────
function onFileInput(e) { if (e.target.files?.length) doUpload(e.target.files); e.target.value = '' }
function onDrop(e) {
  dragging.value = false
  if (e.dataTransfer?.files?.length) doUpload(e.dataTransfer.files)
}

async function doUpload(fileList) {
  const files = [...fileList]
  if (!files.length) return
  uploading.value = true
  error.value = ''
  try {
    const fd = new FormData()
    for (const f of files) fd.append('files', f)
    const r = await api('/api/upload', { method: 'POST', body: fd })
    state.previews = r.previews
    state.hotspot = r.hotspot
    state.frames = r.frames
    state.srcSize = r.src_size
    state.verdict = r.verdict
    state.score = r.score
    state.issues = r.issues
    animIndex.value = 0
    startAnim()

    if (r.verdict === '不适合') {
      confirm.title = '图片不适合做光标'
      confirm.body = r.issues.map(i => `• ${i.message}`)
      confirm.action = r.hard_reject ? null : () => apply()
      confirm.show = true
    } else if (r.verdict === '有风险') {
      confirm.title = '图片可能不适合'
      confirm.body = r.issues.map(i => `• ${i.message}`)
      confirm.action = () => apply()
      confirm.show = true
    } else {
      apply()
    }
  } catch (e) {
    error.value = `上传失败: ${e.message}`
  } finally {
    uploading.value = false
  }
}

// ── 启用 / 恢复 ───────────────────────────────────────
async function apply() {
  busy.value = true
  error.value = ''
  try {
    const r = await api('/api/apply', { method: 'POST' })
    state.enabled = r.enabled
  } catch (e) {
    error.value = `启用失败: ${e.message}`
  } finally { busy.value = false }
}

async function restore() {
  busy.value = true
  error.value = ''
  try {
    const r = await api('/api/restore', { method: 'POST' })
    state.enabled = r.enabled
  } catch (e) {
    error.value = `恢复失败: ${e.message}`
  } finally { busy.value = false }
}

async function toggle() {
  if (state.enabled) await restore()
  else {
    if (!state.frames) { error.value = '请先上传图片再启用。'; return }
    await apply()
  }
}

// ── 水平翻转 ──────────────────────────────────────────
async function flipH() {
  if (!state.frames) return
  busy.value = true
  error.value = ''
  try {
    const r = await api('/api/flip', { method: 'POST' })
    state.previews = r.previews
    state.hotspot = r.hotspot
    state.enabled = r.enabled
    animIndex.value = 0
  } catch (e) {
    error.value = `翻转失败: ${e.message}`
  } finally { busy.value = false }
}

// ── 热点 ──────────────────────────────────────────────
const CANVAS_SIZE = 48          // 光标画布逻辑尺寸（与服务端一致）
const PREVIEW_SCALE = 4         // 预览放大倍率（画布实际 192×192）
const canvasRef = ref(null)
function onCanvasClick(e) {
  const c = canvasRef.value
  if (!c || !state.frames) return
  const rect = c.getBoundingClientRect()
  const x = Math.min(47, Math.max(0, Math.floor((e.clientX - rect.left) / rect.width * 48)))
  const y = Math.min(47, Math.max(0, Math.floor((e.clientY - rect.top) / rect.height * 48)))
  state.hotspot = [x, y]
  api('/api/hotspot', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x, y }) })
    .then(r => { state.enabled = r.enabled })
    .catch(e => { error.value = `热点更新失败: ${e.message}` })
}

// 绘制预览画布（棋盘格 + 当前帧 + 热点十字，全部按画布真实尺寸 192×192）
function drawPreview() {
  const c = canvasRef.value
  if (!c) return
  const ctx = c.getContext('2d')
  const CS = CANVAS_SIZE * PREVIEW_SCALE   // 192
  ctx.clearRect(0, 0, c.width, c.height)
  // 棋盘格铺满整个画布
  const cell = 12
  for (let y = 0; y < CS; y += cell)
    for (let x = 0; x < CS; x += cell) {
      ctx.fillStyle = ((x / cell + y / cell) % 2 === 0) ? '#e8e8e8' : '#ffffff'
      ctx.fillRect(x, y, cell, cell)
    }
  // 图案（服务端已按 4 倍放大到 192×192，直接 1:1 绘制，保持像素锐利）
  if (currentPreview.value) {
    const img = new Image()
    img.onload = () => {
      ctx.imageSmoothingEnabled = false
      ctx.drawImage(img, 0, 0, CS, CS)
      drawHotspot(ctx, CS)
    }
    img.src = currentPreview.value
  } else {
    drawHotspot(ctx, CS)
  }
}
function drawHotspot(ctx, CS) {
  const [hx, hy] = state.hotspot
  const px = hx * PREVIEW_SCALE
  const py = hy * PREVIEW_SCALE
  const half = 4 * PREVIEW_SCALE
  ctx.strokeStyle = '#ff3030'
  ctx.lineWidth = 1.2
  ctx.beginPath()
  ctx.moveTo(px - half, py); ctx.lineTo(px + half, py)
  ctx.moveTo(px, py - half); ctx.lineTo(px, py + half)
  ctx.stroke()
  ctx.beginPath(); ctx.arc(px, py, 3 * PREVIEW_SCALE, 0, Math.PI * 2); ctx.stroke()
}
watch([currentPreview, () => state.hotspot.join(',')], drawPreview, { flush: 'post' })

// ── 关闭窗口 → 请求退出 ───────────────────────────────
function quitApp() {
  api('/api/quit', { method: 'POST' }).catch(() => {})
}
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="logo">🖱️</span>
        <div>
          <h1>自定义光标控制器</h1>
          <p class="sub">Custom Cursor Controller</p>
        </div>
      </div>
      <div class="status-pill" :class="state.enabled ? 'on' : 'off'">
        <span class="dot"></span>{{ state.enabled ? '自定义光标已启用' : '系统默认光标' }}
      </div>
    </header>

    <div v-if="error" class="error-banner" @click="error = ''">
      ⚠ {{ error }} <span class="dismiss">×</span>
    </div>

    <main class="layout">
      <!-- 左列: 预览 -->
      <section class="card preview-card">
        <h2>预览 <small>点击画面设置热点（点击点位置）</small></h2>
        <canvas ref="canvasRef" :width="CANVAS_SIZE * PREVIEW_SCALE" :height="CANVAS_SIZE * PREVIEW_SCALE"
                class="preview-canvas" @click="onCanvasClick"
                :class="{ empty: !state.frames }"></canvas>
        <div class="meta-row">
          <span class="tag" v-if="state.frames > 1">动画 {{ state.frames }} 帧</span>
          <span class="tag" v-else-if="state.frames === 1">静态光标</span>
          <span class="tag" v-if="state.srcSize">原图 {{ state.srcSize[0] }}×{{ state.srcSize[1] }}</span>
          <span class="tag accent">热点 ({{ state.hotspot[0] }}, {{ state.hotspot[1] }})</span>
        </div>
        <div class="preview-actions">
          <button class="btn small" :disabled="!state.frames || busy" @click="flipH">⇄ 水平翻转</button>
        </div>
      </section>

      <!-- 右列: 上传 + 分析 -->
      <section class="right-col">
        <div class="card">
          <h2>上传图片</h2>
          <div class="drop-zone" :class="{ over: dragging, busy: uploading }"
               @dragover.prevent="dragging = true" @dragleave="dragging = false"
               @drop.prevent="onDrop" @click="$refs.fileInput.click()">
            <input ref="fileInput" type="file" accept=".png,.jpg,.jpeg,.bmp,.gif,.webp" multiple hidden
                   @change="onFileInput" />
            <div class="dz-icon">{{ uploading ? '⏳' : '📤' }}</div>
            <p>{{ uploading ? '正在分析图片…' : '拖拽图片到这里，或点击选择' }}</p>
            <small>PNG / JPG / BMP / GIF / WebP · 多选 = 动画帧</small>
          </div>
        </div>

        <div class="card" v-if="state.verdict">
          <h2>适合度分析</h2>
          <div class="verdict-row">
            <span class="verdict-badge" :class="state.verdict">{{ state.verdict }}</span>
            <div class="score-bar">
              <div class="score-fill" :class="state.verdict"
                   :style="{ width: (state.score || 0) + '%' }"></div>
            </div>
            <span class="score-num">{{ state.score }}/100</span>
          </div>
          <ul class="issue-list">
            <li v-for="(it, i) in state.issues" :key="i" :class="it.level">
              <span class="lvl">{{ it.level === 'error' ? '严重' : '提示' }}</span>{{ it.message }}
            </li>
            <li v-if="!state.issues.length" class="ok">✓ 未发现问题，图片可以直接使用</li>
          </ul>
        </div>
      </section>
    </main>

    <footer class="actionbar">
      <button class="btn primary" :disabled="busy || uploading || !state.frames" @click="toggle">
        {{ state.enabled ? '停用自定义光标' : '启用自定义光标' }}
      </button>
      <button class="btn" :disabled="busy" @click="restore">恢复默认光标</button>
      <button class="btn danger" @click="quitApp">退出（恢复光标）</button>
      <span class="hint">程序在后台运行时光标保持生效，退出自动恢复系统默认</span>
    </footer>

    <!-- 确认对话框 -->
    <div v-if="confirm.show" class="modal-mask" @click.self="confirm.show = false">
      <div class="modal">
        <h3>{{ confirm.title }}</h3>
        <ul class="issue-list">
          <li v-for="(t, i) in confirm.body" :key="i"><span class="lvl">风险</span>{{ t }}</li>
        </ul>
        <div class="modal-actions">
          <button class="btn" @click="confirm.show = false">取消</button>
          <button v-if="confirm.action" class="btn primary" @click="confirm.action(); confirm.show = false">仍然使用</button>
        </div>
      </div>
    </div>
  </div>
</template>
