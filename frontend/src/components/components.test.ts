import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api'
import { ApiError } from '@/api/client'
import type { Approval, Project, Repository } from '@/api/types'
import { useWorkspaceStore } from '@/stores/workspace'

import ApprovalPanel from './ApprovalPanel.vue'
import AsyncBlock from './AsyncBlock.vue'
import DiffViewer from './DiffViewer.vue'
import MarkdownView from './MarkdownView.vue'
import RunTimeline from './RunTimeline.vue'
import StatusBadge from './StatusBadge.vue'
import TestCounts from './TestCounts.vue'

const DIFF = '--- a/app/pricing.py\n+++ b/app/pricing.py\n@@ -10,3 +10,3 @@\n context\n-    return price - price * percent\n+    return price - price * percent / 100\n more\n'

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: 'a1', repository_id: 'r1', task_id: 't1', run_id: 'run1', change_set_id: 'c1', action_type: 'apply_changes',
    title: 'Apply changes: Fix discount', payload: { files: [{ path: 'app/pricing.py' }], deletes: [] }, status: 'pending',
    requested_by_user_id: null, decided_by: null, decided_at: null, comment: null, result: null,
    expires_at: new Date(Date.now() + 3_600_000).toISOString(), created_at: new Date().toISOString(),
    ...overrides,
  }
}

function setRole(role: Project['my_role']): void {
  const workspace = useWorkspaceStore()
  workspace.projects = [{ id: 'p1', name: 'P', description: '', owner_id: 'u', settings: {}, created_at: '', my_role: role, repository_count: 1 }]
  workspace.projectId = 'p1'
  workspace.repositories = [{ id: 'r1', project_id: 'p1', name: 'shop' } as Repository]
  workspace.repositoryId = 'r1'
}

beforeEach(() => setActivePinia(createPinia()))

describe('DiffViewer', () => {
  it('shows added and removed lines with their numbers and totals', () => {
    const wrapper = mount(DiffViewer, { props: { diff: DIFF, path: 'app/pricing.py', changeType: 'modify' } })
    expect(wrapper.text()).toContain('app/pricing.py')
    expect(wrapper.text()).toContain('+1')
    expect(wrapper.text()).toContain('−1')
    const added = wrapper.findAll('tr.bg-diff-add')
    const removed = wrapper.findAll('tr.bg-diff-del')
    expect(added).toHaveLength(1)
    expect(removed).toHaveLength(1)
    expect(added[0]!.text()).toContain('return price - price * percent / 100')
    expect(removed[0]!.findAll('td')[0]!.text()).toBe('11')
    expect(added[0]!.findAll('td')[1]!.text()).toBe('11')
  })

  it('labels new and deleted files and handles empty diffs', () => {
    expect(mount(DiffViewer, { props: { diff: '--- /dev/null\n+++ b/n.md\n@@ -0,0 +1 @@\n+x\n', path: 'n.md', changeType: 'create' } }).text()).toContain('new file')
    expect(mount(DiffViewer, { props: { diff: '', path: 'e.py', changeType: 'delete' } }).text()).toContain('No textual changes')
  })

  it('renders file content as text, not HTML', () => {
    const wrapper = mount(DiffViewer, { props: { diff: '--- a/x.html\n+++ b/x.html\n@@ -1 +1 @@\n-<b>old</b>\n+<script>alert(1)</script>\n', path: 'x.html' } })
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.text()).toContain('<script>alert(1)</script>')
  })
})

describe('small components', () => {
  it('StatusBadge shows a readable label with a tone', () => {
    const badge = mount(StatusBadge, { props: { status: 'awaiting_approval' } })
    expect(badge.text()).toBe('Awaiting approval')
    expect(badge.classes()).toContain('text-warn')
    expect(mount(StatusBadge, { props: { status: 'not_executed' } }).classes()).toContain('text-warn')
    expect(mount(StatusBadge, { props: { status: 'running' } }).find('.animate-pulse').exists()).toBe(true)
  })

  it('TestCounts always lists all five outcomes', () => {
    const text = mount(TestCounts, { props: { passed: 4, failed: 0, skipped: 1, errors: 0, notExecuted: 2 } }).text()
    for (const part of ['4 passed', '0 failed', '1 skipped', '0 errors', '2 not executed']) expect(text).toContain(part)
  })

  it('AsyncBlock switches between loading, error, empty and content', async () => {
    const wrapper = mount(AsyncBlock, { props: { loading: true, error: null }, slots: { default: '<p>content</p>' } })
    expect(wrapper.text()).toContain('Loading')
    await wrapper.setProps({ loading: false, error: 'Boom' })
    expect(wrapper.find('[role="alert"]').text()).toContain('Boom')
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
    await wrapper.setProps({ error: null, empty: true, emptyTitle: 'Nothing' })
    expect(wrapper.text()).toContain('Nothing')
    await wrapper.setProps({ empty: false })
    expect(wrapper.text()).toBe('content')
  })

  it('MarkdownView does not inject markup from the report', () => {
    const wrapper = mount(MarkdownView, { props: { source: '# Result\n\n<img src=x onerror=alert(1)> **done**' } })
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('strong').text()).toBe('done')
  })

  it('RunTimeline describes events and flags failures and security notices', () => {
    const at = new Date().toISOString()
    const wrapper = mount(RunTimeline, {
      props: {
        live: true,
        events: [
          { id: 1, run_id: 'r', type: 'run_started', node: null, payload: {}, created_at: at },
          { id: 2, run_id: 'r', type: 'node_started', node: 'analyze', payload: {}, created_at: at },
          { id: 3, run_id: 'r', type: 'tool_call', node: 'analyze', payload: { tool: 'fs_delete_file', ok: false, error_code: 'tool_not_allowed', arguments: { path: 'a.py' } }, created_at: at },
          { id: 4, run_id: 'r', type: 'security_notice', node: 'analyze', payload: { notice: 'fs_read_file: content resembling instructions' }, created_at: at },
          { id: 5, run_id: 'r', type: 'llm_call', node: 'analyze', payload: {}, created_at: at },
          { id: 6, run_id: 'r', type: 'test_run', node: 'verify', payload: { framework: 'pytest', kind: 'unit', status: 'not_executed', totals: { passed: 0, failed: 0, skipped: 0, error: 0, not_executed: 3 } }, created_at: at },
        ],
      },
    })
    const text = wrapper.text()
    expect(text).toContain('Analysing the code')
    expect(text).toContain('Tool: fs_delete_file — tool_not_allowed')
    expect(text).toContain('Security notice')
    expect(text).toContain('pytest unit: not_executed')
    expect(text).toContain('3 not executed')
    expect(text).toContain('Working…')
    expect(wrapper.findAll('li')).toHaveLength(6) // llm_call is not shown; the live row is
  })
})

describe('ApprovalPanel', () => {
  it('approves with a comment and reports the decision', async () => {
    setRole('developer')
    const decided = approval({ status: 'approved' })
    const approve = vi.spyOn(api, 'approve').mockResolvedValue(decided)
    const wrapper = mount(ApprovalPanel, { props: { approval: approval() } })
    await wrapper.find('textarea').setValue('looks right')
    await wrapper.find('button.btn-primary').trigger('click')
    await flushPromises()
    expect(approve).toHaveBeenCalledWith('a1', 'looks right', false)
    expect(wrapper.emitted('decided')![0]).toEqual([decided])
  })

  it('requires explicit confirmation before approving deletions', async () => {
    setRole('owner')
    const approve = vi.spyOn(api, 'approve').mockResolvedValue(approval({ status: 'approved' }))
    const wrapper = mount(ApprovalPanel, { props: { approval: approval({ payload: { files: [], deletes: ['tests/test_auth.py'] } }) } })
    const button = wrapper.find('button.btn-primary')
    expect(wrapper.text()).toContain('This change deletes files')
    expect(button.attributes('disabled')).toBeDefined()
    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(button.attributes('disabled')).toBeUndefined()
    await button.trigger('click')
    await flushPromises()
    expect(approve).toHaveBeenCalledWith('a1', '', true)
  })

  it('rejects, and shows server errors without emitting', async () => {
    setRole('developer')
    vi.spyOn(api, 'reject').mockRejectedValue(new ApiError(409, 'approval_already_decided', 'This request was already approved'))
    const wrapper = mount(ApprovalPanel, { props: { approval: approval() } })
    await wrapper.find('button.btn-danger').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('already approved')
    expect(wrapper.emitted('decided')).toBeUndefined()
  })

  it('hides the decision controls from viewers and for decided or expired requests', () => {
    setRole('viewer')
    const viewer = mount(ApprovalPanel, { props: { approval: approval() } })
    expect(viewer.find('button').exists()).toBe(false)
    expect(viewer.text()).toContain('read-only access')

    setRole('developer')
    expect(mount(ApprovalPanel, { props: { approval: approval({ status: 'rejected', comment: 'no' }) } }).find('button').exists()).toBe(false)
    const expired = mount(ApprovalPanel, { props: { approval: approval({ expires_at: new Date(Date.now() - 1000).toISOString() }) } })
    expect(expired.find('button').exists()).toBe(false)
    expect(expired.text()).toContain('expired')
  })

  it('warns when files changed after the proposal', () => {
    setRole('developer')
    const wrapper = mount(ApprovalPanel, { props: { approval: approval(), staleFiles: [{ path: 'app/pricing.py', reason: 'file was modified after the proposal' }] } })
    expect(wrapper.text()).toContain('These files changed after the proposal was made')
    expect(wrapper.text()).toContain('never overwritten')
  })
})
