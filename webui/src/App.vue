<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'

// ── 状态 ──────────────────────────────────────────────
const state = reactive({
  enabled: false,
  canvasSize: 64,
  frames: 0,
  hotspot: [24, 28],
  verdict: null,        // 适合 | 有风险 | 不适合
  score: null,
  issues: [],
  previews: [],         // base64 PNG 列表（每帧一张）
  srcSize: null,
  dataDir: '',
})

const tab = ref('console')          // console | gallery
const galTab = ref('cursors')       // uploads | cursors
const gallery = reactive({ uploads: [], cursors: [] })

const uploading = ref(false)
const busy = ref(false)
const error = ref('')
const dragging = ref(false)
const confirm = reactive({ show: false, title: '', body: [], action: null })

// 弹窗提示（Toast）
const toast = reactive({ show: false, text: '', kind: 'ok' })
let toastTimer = null
function showToast(text, kind = 'ok') {
  toast.text = text
  toast.kind = kind
  toast.show = true
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.show = false }, 3200)
}

// 命名 / 归类 模态框
const editModal = reactive({ show: false, title: '', kind: '', id: '', value: '', isName: false })
const categories = computed(() => {
  const set = new Set()
  for (const u of gallery.uploads) if (u.category) set.add(u.category)
  for (const c of gallery.cursors) if (c.category) set.add(c.category)
  return [...set]
})

// ── 预览动画（多帧时轮播）────────────────────────────
let animTimer = null
const animIndex = ref(0)
const previewCount = computed(() => state.previews.length)
const currentPreview = computed(() =>
  state.previews.length ? state.previews[animIndex.value % state.previews.length] : null)

// 预览画布像素尺寸（随光标大小自适应，最大约 256px）
const previewScale = computed(() => Math.max(2, Math.floor(256 / (state.canvasSize || 64))))
const previewPx = computed(() => (state.canvasSize || 64) * previewScale.value)

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
  window.addEventListener('error', (e) => {
    error.value = `前端错误: ${e.message}`
  })
  try {
    const s = await api('/api/state')
    state.enabled = s.enabled
    state.canvasSize = s.canvas_size
    state.frames = s.frames
    state.hotspot = s.hotspot
    state.verdict = s.verdict
    state.score = s.score
    state.issues = s.issues
    state.srcSize = s.src_size
    state.dataDir = s.data_dir || ''
    if (s.frames > 0) {
      const p = await api('/api/previews')
      state.previews = p.previews
      animIndex.value = 0
      startAnim()
    }
    await refreshGallery()
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
    state.canvasSize = r.canvas_size
    state.srcSize = r.src_size
    state.verdict = r.verdict
    state.score = r.score
    state.issues = r.issues
    state.dataDir = r.data_dir || state.dataDir
    animIndex.value = 0
    startAnim()
    await refreshGallery()

    const applyAndNotify = async () => {
      await apply()
      showToast(`已自动替换为新光标「${r.cursor_name}」`)
    }

    if (r.verdict === '不适合') {
      confirm.title = '图片不适合做光标'
      confirm.body = r.issues.map(i => `• ${i.message}`)
      confirm.action = r.hard_reject ? null : applyAndNotify
      confirm.show = true
    } else if (r.verdict === '有风险') {
      confirm.title = '图片可能不适合'
      confirm.body = r.issues.map(i => `• ${i.message}`)
      confirm.action = applyAndNotify
      confirm.show = true
    } else {
      await applyAndNotify()
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

// ── 旋转 90° ──────────────────────────────────────────
async function rotate(dir) {
  if (!state.frames) return
  busy.value = true
  error.value = ''
  try {
    const r = await api('/api/rotate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction: dir }),
    })
    state.previews = r.previews
    state.hotspot = r.hotspot
    state.enabled = r.enabled
    animIndex.value = 0
  } catch (e) {
    error.value = `旋转失败: ${e.message}`
  } finally { busy.value = false }
}

// ── 光标大小 ──────────────────────────────────────────
async function setSize(s) {
  if (s === state.canvasSize) return
  busy.value = true
  error.value = ''
  try {
    const r = await api('/api/size', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ size: s }),
    })
    state.canvasSize = r.canvas_size
    state.hotspot = r.hotspot
    state.enabled = r.enabled
    state.previews = r.previews
    animIndex.value = 0
    showToast(`光标大小已切换为 ${r.canvas_size}${state.frames ? '' : '（上传图片后生效）'}`)
  } catch (e) {
    error.value = `调整大小失败: ${e.message}`
  } finally { busy.value = false }
}

// ── 热点 ──────────────────────────────────────────────
const canvasRef = ref(null)
function onCanvasClick(e) {
  const c = canvasRef.value
  if (!c || !state.frames) return
  const rect = c.getBoundingClientRect()
  const n = state.canvasSize - 1
  const x = Math.min(n, Math.max(0, Math.floor((e.clientX - rect.left) / rect.width * state.canvasSize)))
  const y = Math.min(n, Math.max(0, Math.floor((e.clientY - rect.top) / rect.height * state.canvasSize)))
  state.hotspot = [x, y]
  api('/api/hotspot', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ x, y }) })
    .then(r => { state.enabled = r.enabled })
    .catch(e => { error.value = `热点更新失败: ${e.message}` })
}

// 绘制预览画布（棋盘格 + 当前帧 + 热点十字，全部按画布真实尺寸）
function drawPreview() {
  const c = canvasRef.value
  if (!c) return
  const ctx = c.getContext('2d')
  const CS = previewPx.value
  ctx.clearRect(0, 0, c.width, c.height)
  const cell = 12
  for (let y = 0; y < CS; y += cell)
    for (let x = 0; x < CS; x += cell) {
      ctx.fillStyle = ((x / cell + y / cell) % 2 === 0) ? '#e8e8e8' : '#ffffff'
      ctx.fillRect(x, y, cell, cell)
    }
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
  const s = previewScale.value
  const px = hx * s
  const py = hy * s
  const half = 4 * s
  ctx.strokeStyle = '#ff3030'
  ctx.lineWidth = 1.2
  ctx.beginPath()
  ctx.moveTo(px - half, py); ctx.lineTo(px + half, py)
  ctx.moveTo(px, py - half); ctx.lineTo(px, py + half)
  ctx.stroke()
  ctx.beginPath(); ctx.arc(px, py, 3 * s, 0, Math.PI * 2); ctx.stroke()
}
watch([currentPreview, () => state.hotspot.join(','), previewPx], drawPreview, { flush: 'post' })

// ── 图库 ──────────────────────────────────────────────
async function refreshGallery() {
  try {
    const g = await api('/api/gallery')
    gallery.uploads = g.uploads
    gallery.cursors = g.cursors
  } catch { /* 忽略 */ }
}

async function applyCursor(c) {
  busy.value = true
  error.value = ''
  try {
    const r = await api(`/api/gallery/cursors/${c.id}/apply`, { method: 'POST' })
    state.enabled = r.enabled
    state.canvasSize = r.canvas_size
    state.hotspot = r.hotspot
    const p = await api('/api/previews')
    state.previews = p.previews
    state.frames = r.frames
    state.verdict = null
    state.issues = []
    animIndex.value = 0
    startAnim()
    showToast(`已应用光标「${r.name}」`)
    tab.value = 'console'
  } catch (e) {
    error.value = `应用失败: ${e.message}`
  } finally { busy.value = false }
}

function openRename(c) {
  editModal.show = true
  editModal.title = '重命名光标'
  editModal.kind = 'cursors'
  editModal.id = c.id
  editModal.value = c.name
  editModal.isName = true
}

function openCategory(kind, item) {
  editModal.show = true
  editModal.title = kind === 'cursors' ? '给光标归类' : '给图片归类'
  editModal.kind = kind
  editModal.id = item.id
  editModal.value = item.category
  editModal.isName = false
}

async function saveEdit() {
  const v = editModal.value.trim()
  if (!v) { error.value = editModal.isName ? '名称不能为空' : '归类不能为空'; return }
  try {
    const url = editModal.isName
      ? `/api/gallery/cursors/${editModal.id}/rename`
      : `/api/gallery/${editModal.kind}/${editModal.id}/category`
    await api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(editModal.isName ? { name: v } : { category: v }),
    })
    await refreshGallery()
    showToast(editModal.isName ? '已重命名' : '已设置归类')
    editModal.show = false
  } catch (e) {
    error.value = `保存失败: ${e.message}`
  }
}

function delItem(kind, item) {
  confirm.title = kind === 'cursors' ? `删除光标「${item.name}」？` : `删除图片「${item.original}」？`
  confirm.body = ['删除后不可恢复。']
  confirm.action = async () => {
    try {
      await api(`/api/gallery/${kind}/${item.id}/delete`, { method: 'POST' })
      await refreshGallery()
      showToast('已删除')
    } catch (e) {
      error.value = `删除失败: ${e.message}`
    }
  }
  confirm.show = true
}

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

    <nav class="tabs">
      <button class="tab" :class="{ active: tab === 'console' }" @click="tab = 'console'">🎛 控制台</button>
      <button class="tab" :class="{ active: tab === 'gallery' }" @click="tab = 'gallery'">🗂 图库</button>
    </nav>

    <div v-if="error" class="error-banner" @click="error = ''">
      ⚠ {{ error }} <span class="dismiss">×</span>
    </div>

    <!-- ── 控制台 ── -->
    <main v-if="tab === 'console'" class="layout">
      <section class="card preview-card">
        <h2>预览 <small>点击画面设置热点（点击点位置）</small></h2>
        <canvas ref="canvasRef" :width="previewPx" :height="previewPx"
                :style="{ width: previewPx + 'px', height: previewPx + 'px' }"
                class="preview-canvas" @click="onCanvasClick"
                :class="{ empty: !state.frames }"></canvas>
        <div class="size-row">
          <span class="size-lbl">光标大小</span>
          <button v-for="s in [48, 64, 96]" :key="s" class="size-btn"
                  :class="{ active: state.canvasSize === s }" :disabled="busy"
                  @click="setSize(s)">{{ s }}</button>
        </div>
        <div class="meta-row">
          <span class="tag" v-if="state.frames > 1">动画 {{ state.frames }} 帧</span>
          <span class="tag" v-else-if="state.frames === 1">静态光标</span>
          <span class="tag" v-if="state.srcSize">原图 {{ state.srcSize[0] }}×{{ state.srcSize[1] }}</span>
          <span class="tag accent">热点 ({{ state.hotspot[0] }}, {{ state.hotspot[1] }})</span>
        </div>
        <div class="preview-actions">
          <button class="btn small" :disabled="!state.frames || busy" @click="rotate('ccw')">↺ 逆时针 90°</button>
          <button class="btn small" :disabled="!state.frames || busy" @click="rotate('cw')">↻ 顺时针 90°</button>
        </div>
      </section>

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

    <!-- ── 图库 ── -->
    <main v-else class="gallery">
      <div class="gal-tabs">
        <button class="tab small" :class="{ active: galTab === 'cursors' }"
                @click="galTab = 'cursors'">生成的光标 ({{ gallery.cursors.length }})</button>
        <button class="tab small" :class="{ active: galTab === 'uploads' }"
                @click="galTab = 'uploads'">上传的图片 ({{ gallery.uploads.length }})</button>
        <span v-if="state.dataDir" class="gal-dir">存储目录: {{ state.dataDir }}</span>
      </div>

      <div v-if="galTab === 'cursors'" class="grid">
        <div v-for="c in gallery.cursors" :key="c.id" class="gcard">
          <img class="gthumb" :src="`/api/gallery/cursors/${c.id}/preview`" alt="" />
          <div class="gname" :title="c.name">{{ c.name }}</div>
          <div class="gmeta">
            <span v-if="c.animated">动画 {{ c.frames }} 帧</span>
            <span v-else>静态</span> · {{ c.size }}px
            <span v-if="c.category" class="gcat">#{{ c.category }}</span>
          </div>
          <div class="gactions">
            <button class="btn tiny primary" @click="applyCursor(c)">应用</button>
            <button class="btn tiny" @click="openRename(c)">重命名</button>
            <button class="btn tiny" @click="openCategory('cursors', c)">归类</button>
            <button class="btn tiny danger" @click="delItem('cursors', c)">删除</button>
          </div>
        </div>
        <div v-if="!gallery.cursors.length" class="empty-hint">还没有生成的光标。上传图片后会自动保存到这里。</div>
      </div>

      <div v-else class="grid">
        <div v-for="u in gallery.uploads" :key="u.id" class="gcard">
          <img class="gthumb" :src="`/api/gallery/uploads/${u.id}/thumb`" alt="" />
          <div class="gname" :title="u.original">{{ u.original }}</div>
          <div class="gmeta">
            {{ u.size[0] }}×{{ u.size[1] }} · 判定 {{ u.verdict }} ({{ u.score }}分)
            <span v-if="u.category" class="gcat">#{{ u.category }}</span>
          </div>
          <div class="gactions">
            <button class="btn tiny" @click="openCategory('uploads', u)">归类</button>
            <button class="btn tiny danger" @click="delItem('uploads', u)">删除</button>
          </div>
        </div>
        <div v-if="!gallery.uploads.length" class="empty-hint">还没有上传的图片。</div>
      </div>
    </main>

    <footer class="actionbar">
      <template v-if="tab === 'console'">
        <button class="btn primary" :disabled="busy || uploading || !state.frames" @click="toggle">
          {{ state.enabled ? '停用自定义光标' : '启用自定义光标' }}
        </button>
        <button class="btn" :disabled="busy" @click="restore">恢复默认光标</button>
        <button class="btn danger" @click="quitApp">退出（恢复光标）</button>
        <span class="hint">上传新图片会自动替换光标；退出时自动恢复系统默认</span>
      </template>
      <template v-else>
        <span class="hint">上传的图片与生成的光标保存在程序目录的 data 文件夹中，可随时应用 / 重命名 / 归类 / 删除</span>
      </template>
    </footer>

    <!-- Toast 弹窗 -->
    <transition name="toast">
      <div v-if="toast.show" class="toast" :class="toast.kind">{{ toast.text }}</div>
    </transition>

    <!-- 确认对话框 -->
    <div v-if="confirm.show" class="modal-mask" @click.self="confirm.show = false">
      <div class="modal">
        <h3>{{ confirm.title }}</h3>
        <ul class="issue-list">
          <li v-for="(t, i) in confirm.body" :key="i"><span class="lvl">提示</span>{{ t }}</li>
        </ul>
        <div class="modal-actions">
          <button class="btn" @click="confirm.show = false">取消</button>
          <button v-if="confirm.action" class="btn primary" @click="confirm.action(); confirm.show = false">确定</button>
        </div>
      </div>
    </div>

    <!-- 重命名 / 归类 模态框 -->
    <div v-if="editModal.show" class="modal-mask" @click.self="editModal.show = false">
      <div class="modal">
        <h3>{{ editModal.title }}</h3>
        <input class="edit-input" v-model="editModal.value" :placeholder="editModal.isName ? '输入光标名称' : '输入归类（如：常用/动物/箭头）'"
               @keyup.enter="saveEdit" list="cat-list" />
        <datalist id="cat-list">
          <option v-for="c in categories" :key="c" :value="c"></option>
        </datalist>
        <div class="modal-actions">
          <button class="btn" @click="editModal.show = false">取消</button>
          <button class="btn primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
