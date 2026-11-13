<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { api } from '@/api'
import { colorMode, setColorMode } from '@/lib/colorMode'
import { usePolling } from '@/lib/useAsync'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'

// The shell: a bar across the top, the rail on the left (folds to icons; a sheet from the
// right on a phone, opened by the More tab of the bottom bar), and the page between them.

const MINI_KEY = 'asea.navMini'

const auth = useAuthStore()
const workspace = useWorkspaceStore()
const route = useRoute()
const router = useRouter()
const menuOpen = ref(false)
const userMenuOpen = ref(false)
const userMenu = ref<HTMLElement | null>(null)
const mini = ref(readMini())
const pendingApprovals = ref<number | null>(null)

interface NavItem {
  to: string
  label: string
  short: string
  icon: string
  badge?: boolean
}

const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: 'Overview',
    items: [
      { to: '/', label: 'Dashboard', short: 'Home', icon: 'mdi-view-dashboard-outline' },
      { to: '/projects', label: 'Projects & repositories', short: 'Projects', icon: 'mdi-folder-multiple-outline' },
    ],
  },
  {
    title: 'Develop',
    items: [
      { to: '/assistant', label: 'Developer assistant', short: 'Ask', icon: 'mdi-message-text-outline' },
      { to: '/code', label: 'Code analysis', short: 'Code', icon: 'mdi-code-braces' },
      { to: '/changes', label: 'Change review', short: 'Changes', icon: 'mdi-file-compare' },
      { to: '/approvals', label: 'Approvals', short: 'Approvals', icon: 'mdi-check-decagram-outline', badge: true },
      { to: '/tests', label: 'Tests & checks', short: 'Tests', icon: 'mdi-test-tube' },
    ],
  },
  {
    title: 'Activity',
    items: [
      { to: '/mcp', label: 'MCP & tool activity', short: 'Tools', icon: 'mdi-tools' },
      { to: '/github', label: 'Git & GitHub', short: 'Git', icon: 'mdi-github' },
      { to: '/history', label: 'Execution history', short: 'History', icon: 'mdi-history' },
    ],
  },
  {
    title: 'System',
    items: [{ to: '/settings', label: 'Settings & permissions', short: 'Settings', icon: 'mdi-cog-outline' }],
  },
]
const ALL_ITEMS = NAV_GROUPS.flatMap((group) => group.items)
// The four tabs of the phone's bottom bar; everything else is under "More".
const MOBILE_TABS = ['/', '/assistant', '/changes', '/approvals'].map((to) => ALL_ITEMS.find((item) => item.to === to)!)

const initials = computed(() => {
  const source = auth.user?.full_name?.trim() || auth.user?.email?.split('@')[0] || ''
  const parts = source.split(/[\s._-]+/).filter(Boolean)
  const letters = parts.length > 1 ? parts[0]![0]! + parts[1]![0]! : source.slice(0, 2)
  return letters.toUpperCase() || '?'
})

function readMini(): boolean {
  try {
    return localStorage.getItem(MINI_KEY) === '1'
  } catch {
    return false
  }
}

function toggleMini(): void {
  mini.value = !mini.value
  try {
    localStorage.setItem(MINI_KEY, mini.value ? '1' : '0')
  } catch {
    // private browsing: the choice lasts for this visit only
  }
}

async function refreshBadge(): Promise<void> {
  try {
    pendingApprovals.value = (await api.approvals({ status: 'pending', limit: 1 })).total
  } catch {
    pendingApprovals.value = null
  }
}

function closeOnOutside(event: MouseEvent): void {
  if (userMenuOpen.value && userMenu.value && !userMenu.value.contains(event.target as Node)) userMenuOpen.value = false
}

function closeOnEscape(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  userMenuOpen.value = false
  menuOpen.value = false
}

onMounted(() => {
  if (!workspace.loaded) void workspace.load()
  void refreshBadge()
  document.addEventListener('click', closeOnOutside)
  document.addEventListener('keydown', closeOnEscape)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', closeOnOutside)
  document.removeEventListener('keydown', closeOnEscape)
})
usePolling(refreshBadge, 10_000)
watch(() => route.fullPath, () => {
  menuOpen.value = false
  userMenuOpen.value = false
  void refreshBadge()
  // Projects can appear after sign-in (added by someone else or by the seed script).
  if (!workspace.projects.length && !workspace.loading) void workspace.load()
})

function isActive(to: string): boolean {
  return to === '/' ? route.path === '/' : route.path === to || route.path.startsWith(to + '/')
}

function logout(): void {
  auth.logout()
  workspace.reset()
  void router.push('/login')
}
</script>

<template>
  <div class="shell" :class="{ 'shell--mini': mini }">
    <a href="#main" class="sr-only focus:not-sr-only focus:fixed focus:z-[1100] focus:m-2 focus:rounded-md focus:bg-surface focus:p-2">Skip to content</a>

    <header class="appbar">
      <RouterLink to="/" class="appbar__brand">
        <span class="appbar__mark mdi mdi-sail-boat" aria-hidden="true"></span>
        <span class="appbar__name">Shipwright</span>
      </RouterLink>
      <button class="icon-btn appbar__fold" type="button" aria-label="Fold or open the navigation" @click="toggleMini">
        <span class="mdi" :class="mini ? 'mdi-menu' : 'mdi-menu-open'" aria-hidden="true"></span>
      </button>

      <div class="appbar__place">
        <label class="place">
          <span class="place__label">Project</span>
          <select
            class="input input-sm place__select"
            :value="workspace.projectId ?? ''"
            :disabled="!workspace.projects.length"
            @change="workspace.selectProject(($event.target as HTMLSelectElement).value || null)"
          >
            <option v-if="!workspace.projects.length" value="">No projects</option>
            <option v-for="project in workspace.projects" :key="project.id" :value="project.id">{{ project.name }}</option>
          </select>
        </label>
        <label class="place">
          <span class="place__label">Repository</span>
          <select
            class="input input-sm place__select"
            :value="workspace.repositoryId ?? ''"
            :disabled="!workspace.repositories.length"
            @change="workspace.selectRepository(($event.target as HTMLSelectElement).value || null)"
          >
            <option v-if="!workspace.repositories.length" value="">No repositories</option>
            <option v-for="repository in workspace.repositories" :key="repository.id" :value="repository.id">{{ repository.name }}</option>
          </select>
        </label>
      </div>

      <div ref="userMenu" class="user">
        <button
          class="user__trigger"
          type="button"
          aria-haspopup="true"
          :aria-expanded="userMenuOpen"
          :aria-label="`Account: ${auth.user?.email ?? ''}`"
          @click="userMenuOpen = !userMenuOpen"
        >
          <span class="user__avatar">{{ initials }}</span>
        </button>
        <Transition name="fade">
          <div v-if="userMenuOpen" class="user__menu">
            <div class="user__head">
              <p class="truncate font-semibold">{{ auth.user?.full_name || auth.user?.email }}</p>
              <p class="truncate text-xs text-muted">{{ auth.user?.email }} · {{ auth.user?.role }}</p>
            </div>
            <div class="user__theme">
              <span class="text-xs font-medium">Theme</span>
              <div class="seg" role="group" aria-label="Theme">
                <button class="seg__btn" type="button" aria-label="Light theme" :aria-pressed="colorMode === 'light'" @click="setColorMode('light')">
                  <span class="mdi mdi-white-balance-sunny text-lg" aria-hidden="true"></span>
                </button>
                <button class="seg__btn" type="button" aria-label="Dark theme" :aria-pressed="colorMode === 'dark'" @click="setColorMode('dark')">
                  <span class="mdi mdi-weather-night text-lg" aria-hidden="true"></span>
                </button>
              </div>
            </div>
            <div class="user__list">
              <button class="user__item" type="button" @click="logout">
                <span class="mdi mdi-logout text-xl text-faint" aria-hidden="true"></span>
                Sign out
              </button>
            </div>
          </div>
        </Transition>
      </div>
    </header>

    <Transition name="fade">
      <div v-if="menuOpen" class="scrim" @click="menuOpen = false"></div>
    </Transition>
    <aside class="rail" :class="{ 'rail--open': menuOpen }">
      <nav class="rail__list" aria-label="Main">
        <template v-for="(group, index) in NAV_GROUPS" :key="group.title">
          <div v-if="index" class="rail__divider"></div>
          <p class="rail__subheader">{{ group.title }}</p>
          <RouterLink
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            class="rail__item"
            :class="{ 'rail__item--active': isActive(item.to) }"
            :aria-current="isActive(item.to) ? 'page' : undefined"
            :title="item.label"
          >
            <span class="rail__icon mdi" :class="item.icon" aria-hidden="true"></span>
            <span class="rail__label">{{ item.label }}</span>
            <span v-if="item.badge && pendingApprovals" class="rail__badge" :aria-label="`${pendingApprovals} pending`">{{ pendingApprovals }}</span>
          </RouterLink>
        </template>
      </nav>
      <p class="rail__footer">Shipwright · AI coding agent</p>
    </aside>

    <main id="main" class="page">
      <div class="page__content">
        <RouterView :key="route.name === 'task' ? route.fullPath : undefined" />
      </div>
    </main>

    <nav class="mnav" aria-label="Primary">
      <RouterLink
        v-for="tab in MOBILE_TABS"
        :key="tab.to"
        :to="tab.to"
        class="mnav__tab"
        :class="{ 'mnav__tab--active': isActive(tab.to) }"
        :aria-current="isActive(tab.to) ? 'page' : undefined"
      >
        <span class="mnav__icon">
          <span class="mdi" :class="tab.icon" aria-hidden="true"></span>
          <span v-if="tab.badge && pendingApprovals" class="mnav__dot" :aria-label="`${pendingApprovals} pending`"></span>
        </span>
        <span class="mnav__label">{{ tab.short }}</span>
      </RouterLink>
      <button type="button" class="mnav__tab" :aria-expanded="menuOpen" aria-label="More screens" @click="menuOpen = !menuOpen">
        <span class="mnav__icon"><span class="mdi mdi-dots-horizontal" aria-hidden="true"></span></span>
        <span class="mnav__label">More</span>
      </button>
    </nav>
  </div>
</template>

<style scoped>
/* -- the bar ---------------------------------------------------------------------------
   A plain surface with a hairline under it and a soft brand wash behind the mark that is
   gone by 300 px. No block: the bar stays one piece. */
.appbar {
  position: fixed;
  inset: 0 0 auto 0;
  z-index: 1007;
  display: flex;
  align-items: center;
  gap: 4px;
  height: var(--app-bar-h);
  padding: 0 8px;
  background-color: var(--color-surface);
  border-bottom: 1px solid var(--hairline);
  background-image: linear-gradient(
    100deg,
    var(--color-logo-wash) 0,
    color-mix(in srgb, var(--color-logo-wash) 39%, transparent) 190px,
    transparent 300px
  );
}
.appbar__brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  min-height: var(--control-h);
  margin-left: 8px;
  color: inherit;
  text-decoration: none;
}
.appbar__mark {
  font-size: 26px;
  line-height: 1;
  color: var(--color-brand);
}
.appbar__name {
  font-size: var(--text-lg);
  font-weight: 600;
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  border-radius: var(--radius-md);
  font-size: 20px;
  color: var(--color-fg);
  cursor: pointer;
  transition: background-color var(--dur-fast) var(--ease-out);
}
.icon-btn:hover {
  background: var(--fill-2);
}
.appbar__fold {
  margin-left: 4px;
}
.appbar__place {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  margin-left: auto;
}
.place {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.place__label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--color-muted);
}
.place__select {
  width: clamp(120px, 18vw, 220px);
}

/* -- the account menu ------------------------------------------------------------------ */
.user {
  position: relative;
  flex: 0 0 auto;
  margin-left: 8px;
}
.user__trigger {
  padding: 4px;
  margin-right: 4px;
  border-radius: var(--radius-md);
  cursor: pointer;
}
.user__trigger:hover {
  background: var(--fill-2);
}
.user__avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  background: var(--color-accent-soft);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.02em;
}
.user__menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 4px;
  z-index: 1020;
  width: min(320px, calc(100vw - 16px));
  overflow: hidden;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  box-shadow: var(--overlay-shadow);
}
.user__head {
  padding: 16px;
  background: var(--color-accent-soft);
  border-bottom: 1px solid var(--hairline);
}
.user__theme {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--hairline);
}
.user__list {
  padding: 4px;
}
.user__item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-height: 40px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  text-align: left;
  cursor: pointer;
}
.user__item:hover {
  background: var(--fill-1);
}

/* -- the rail --------------------------------------------------------------------------
   Text on the card ground, no slab. The active row wears the brand wash and its icon the
   brand as words; the bar above already says the brand once. */
.rail {
  position: fixed;
  top: var(--app-bar-h);
  bottom: 0;
  left: 0;
  z-index: 1004;
  display: flex;
  flex-direction: column;
  width: var(--rail-w);
  background: var(--color-surface);
  border-right: 1px solid var(--hairline);
  transition:
    width var(--dur-fast) var(--ease-out),
    transform var(--dur) var(--ease-out);
}
.rail__list {
  flex: 1;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: 4px 8px 8px;
}
.rail__divider {
  height: 1px;
  margin: 6px 4px;
  background: var(--hairline);
}
.rail__subheader {
  padding: 6px 12px 4px;
  overflow: hidden;
  font-size: var(--text-micro);
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  white-space: nowrap;
  color: var(--color-muted);
}
.rail__item {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 34px;
  margin-bottom: 2px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  font-weight: 500;
  line-height: 1.3;
  white-space: nowrap;
  color: var(--color-muted);
  text-decoration: none;
}
.rail__icon {
  flex: 0 0 auto;
  font-size: 20px;
  line-height: 1;
  color: var(--color-faint);
}
.rail__label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rail__item:hover {
  background: var(--fill-1);
  color: var(--color-fg);
}
.rail__item:hover .rail__icon {
  color: var(--color-muted);
}
.rail__item--active,
.rail__item--active:hover {
  background: var(--color-accent-soft);
  color: var(--color-fg);
  font-weight: 600;
}
.rail__item--active .rail__icon,
.rail__item--active:hover .rail__icon {
  color: var(--color-accent);
}
.rail__badge {
  min-width: 20px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--color-warn-hue);
  color: #111827;
  font-size: var(--text-micro);
  font-weight: 600;
  line-height: 20px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.rail__footer {
  padding: 8px 16px;
  overflow: hidden;
  font-size: var(--text-micro);
  text-align: center;
  white-space: nowrap;
  color: var(--color-muted);
}

/* -- the page -------------------------------------------------------------------------- */
.page {
  min-height: 100dvh;
  padding-top: var(--app-bar-h);
  padding-left: var(--rail-w);
  transition: padding-left var(--dur-fast) var(--ease-out);
}
.page__content {
  box-sizing: border-box;
  max-width: 1440px;
  margin-inline: auto;
  padding: 16px 24px;
}

/* folded: icons only, until the pointer rests on the rail; then it floats over the page */
@media (min-width: 1024px) {
  .shell--mini .rail {
    width: var(--rail-mini-w);
  }
  .shell--mini .page {
    padding-left: var(--rail-mini-w);
  }
  .shell--mini .rail:not(:hover) .rail__subheader,
  .shell--mini .rail:not(:hover) .rail__footer {
    visibility: hidden;
    height: 0;
    padding-block: 0;
  }
  .shell--mini .rail:not(:hover) .rail__item {
    justify-content: center;
    padding-inline: 8px;
  }
  .shell--mini .rail:not(:hover) .rail__label,
  .shell--mini .rail:not(:hover) .rail__badge {
    display: none;
  }
  .shell--mini .rail:hover {
    width: var(--rail-w);
    box-shadow: var(--overlay-shadow);
  }
}

.scrim,
.mnav {
  display: none;
}

/* -- a phone or a tablet: the rail is a sheet from the right, opened by the More tab ---- */
@media (max-width: 1023.98px) {
  .appbar {
    flex-wrap: wrap;
    height: auto;
    min-height: var(--app-bar-h);
    padding-bottom: 6px;
    row-gap: 2px;
    border-bottom: 0;
    background-image: linear-gradient(
      100deg,
      var(--color-logo-wash) 0,
      color-mix(in srgb, var(--color-logo-wash) 55%, transparent) 55%,
      color-mix(in srgb, var(--color-logo-wash) 28%, transparent) 100%
    );
  }
  .appbar__brand {
    min-height: calc(var(--app-bar-h) - 6px);
  }
  .appbar__fold,
  .place__label {
    display: none;
  }
  /* the account button stays at the end of the first row */
  .user {
    order: 2;
    margin-left: auto;
  }
  /* the two selectors take a row of their own under the mark */
  .appbar__place {
    order: 3;
    flex: 1 1 100%;
    margin: 0 4px;
    gap: 8px;
  }
  .place {
    flex: 1 1 0;
  }
  .place__select {
    width: 100%;
  }
  .rail {
    top: 0;
    left: auto;
    right: 0;
    z-index: 1010;
    width: min(var(--rail-w), 86vw);
    padding-bottom: calc(var(--mobile-nav-h) + env(safe-area-inset-bottom));
    border-right: 0;
    border-left: 1px solid var(--hairline);
    transform: translateX(100%);
    visibility: hidden;
  }
  .rail--open {
    transform: translateX(0);
    visibility: visible;
    box-shadow: var(--overlay-shadow);
  }
  .rail__item {
    min-height: var(--control-h);
  }
  .scrim {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 1009;
    background: rgba(16, 17, 20, 0.45);
  }
  .page {
    padding-top: calc(var(--app-bar-h) + 44px);
    padding-left: 0;
  }
  .page__content {
    padding: 12px 12px calc(var(--mobile-nav-h) + 16px + env(safe-area-inset-bottom));
  }

  .mnav {
    position: fixed;
    inset: auto 0 0 0;
    z-index: 1011;
    display: flex;
    align-items: stretch;
    height: calc(var(--mobile-nav-h) + env(safe-area-inset-bottom));
    padding-bottom: env(safe-area-inset-bottom);
    background: var(--color-surface);
    border-top: 1px solid var(--hairline);
  }
  .mnav__tab {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    padding: 6px 2px;
    color: var(--color-muted);
    text-decoration: none;
    cursor: pointer;
  }
  .mnav__icon {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 2px;
    padding: 2px 16px;
    border-radius: 999px;
    font-size: 22px;
    line-height: 1;
    transition: background-color var(--dur-fast) var(--ease-out);
  }
  .mnav__tab--active {
    color: var(--color-brand);
  }
  .mnav__tab--active .mnav__label {
    color: var(--color-accent);
  }
  .mnav__tab--active .mnav__icon {
    background: color-mix(in srgb, var(--color-brand) 14%, transparent);
  }
  .mnav__tab:active .mnav__icon {
    background: var(--fill-2);
  }
  .mnav__dot {
    position: absolute;
    top: 0;
    right: 10px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--color-warn-hue);
  }
  .mnav__label {
    max-width: 100%;
    overflow: hidden;
    font-size: var(--text-micro);
    font-weight: 500;
    line-height: 1.1;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
