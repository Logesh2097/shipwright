import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('@/components/AppShell.vue'),
    children: [
      { path: '', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
      { path: 'projects', name: 'projects', component: () => import('@/views/ProjectsView.vue') },
      { path: 'assistant', name: 'assistant', component: () => import('@/views/AssistantView.vue') },
      { path: 'tasks/:id', name: 'task', component: () => import('@/views/TaskDetailView.vue'), props: true },
      { path: 'code', name: 'code', component: () => import('@/views/CodeAnalysisView.vue') },
      { path: 'changes', name: 'changes', component: () => import('@/views/ChangesView.vue') },
      { path: 'changes/:id', name: 'change', component: () => import('@/views/ChangeReviewView.vue'), props: true },
      { path: 'approvals', name: 'approvals', component: () => import('@/views/ApprovalsView.vue') },
      { path: 'tests', name: 'tests', component: () => import('@/views/TestsView.vue') },
      { path: 'mcp', name: 'mcp', component: () => import('@/views/McpView.vue') },
      { path: 'github', name: 'github', component: () => import('@/views/GitHubView.vue') },
      { path: 'history', name: 'history', component: () => import('@/views/HistoryView.vue') },
      { path: 'settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
    ],
  },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFoundView.vue'), meta: { public: true } },
]

export const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.ready) await auth.restore()
  if (!to.meta.public && !auth.isAuthenticated) return { name: 'login', query: to.fullPath !== '/' ? { next: to.fullPath } : undefined }
  if (to.name === 'login' && auth.isAuthenticated) return { name: 'dashboard' }
  return true
})
