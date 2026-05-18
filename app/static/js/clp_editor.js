// TODO: Replace CDN TipTap imports with bundled local assets before production hardening.
import { Editor, Node, mergeAttributes } from 'https://esm.sh/@tiptap/core@2.11.5'
import StarterKit from 'https://esm.sh/@tiptap/starter-kit@2.11.5'
import Highlight from 'https://esm.sh/@tiptap/extension-highlight@2.11.5'
import Placeholder from 'https://esm.sh/@tiptap/extension-placeholder@2.11.5'
import Table from 'https://esm.sh/@tiptap/extension-table@2.11.5'
import TableRow from 'https://esm.sh/@tiptap/extension-table-row@2.11.5'
import TableCell from 'https://esm.sh/@tiptap/extension-table-cell@2.11.5'
import TableHeader from 'https://esm.sh/@tiptap/extension-table-header@2.11.5'
import TextAlign from 'https://esm.sh/@tiptap/extension-text-align@2.11.5'
import Underline from 'https://esm.sh/@tiptap/extension-underline@2.11.5'
import Link from 'https://esm.sh/@tiptap/extension-link@2.11.5'
import Image from 'https://esm.sh/@tiptap/extension-image@2.11.5'

const root = document.getElementById('clp-editor-root')
const documentId = root.dataset.documentId
const titleInput = document.getElementById('document-title')
const saveButton = document.getElementById('save-document')
const saveVersionButton = document.getElementById('save-version')
const showVersionsButton = document.getElementById('show-versions')
const importDocxButton = document.getElementById('import-docx-button')
const importDocxFile = document.getElementById('import-docx-file')
const exportDocxButton = document.getElementById('export-docx')
const exportPdfButton = document.getElementById('export-pdf')
const generateWeeklyButton = document.getElementById('generate-weekly-outline')
const generateSectionButton = document.getElementById('generate-custom-section')
const alignTableButton = document.getElementById('align-table')
const debugOutput = document.getElementById('debug-output')
const saveStatus = document.getElementById('save-status')
const rewriteButton = document.getElementById('rewrite-selection')

let editorJson = {}
let saving = false

function showStatus(message) {
  saveStatus.textContent = message
}

function showError(message) {
  alert(message)
}

function showDebug(value) {
  debugOutput.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  debugOutput.classList.remove('hidden')
}

function cleanImportWarnings(warnings) {
  const seen = new Set()
  return (warnings || [])
    .map(warning => String(warning || '').trim())
    .filter(warning => warning && warning !== 'No text content found in run')
    .filter(warning => {
      if (seen.has(warning)) return false
      seen.add(warning)
      return true
    })
}

function countTiptapNodes(node) {
  if (!node || typeof node !== 'object') return 0
  return 1 + (node.content || []).reduce((total, child) => total + countTiptapNodes(child), 0)
}

function flattenTiptapText(node, lines = []) {
  if (!node || typeof node !== 'object') return lines
  if (node.type === 'text' && node.text) {
    lines.push(node.text)
  }
  ;(node.content || []).forEach(child => flattenTiptapText(child, lines))
  return lines
}

function plainTextFallbackDoc(tiptapJson) {
  const text = flattenTiptapText(tiptapJson).join('\n').trim()
  if (!text) return { type: 'doc', content: [{ type: 'paragraph' }] }
  return {
    type: 'doc',
    content: text.split(/\n+/).map(line => ({
      type: 'paragraph',
      content: [{ type: 'text', text: line }]
    }))
  }
}

function setEditorContentSafely(tiptapJson, sourceLabel = 'document') {
  const incomingText = flattenTiptapText(tiptapJson).join(' ').trim()
  try {
    editor.commands.setContent(tiptapJson)
  } catch (error) {
    console.error('TipTap setContent failed:', error)
    editor.commands.setContent(plainTextFallbackDoc(tiptapJson))
    showStatus(`${sourceLabel} loaded with plain-text fallback`)
    return
  }

  if (incomingText && !editor.getText().trim()) {
    editor.commands.setContent(plainTextFallbackDoc(tiptapJson))
    showStatus(`${sourceLabel} loaded with plain-text fallback`)
  }
}

function debounce(fn, wait) {
  let timeout
  return (...args) => {
    clearTimeout(timeout)
    timeout = setTimeout(() => fn(...args), wait)
  }
}

const ClpSection = Node.create({
  name: 'clpSection',
  group: 'block',
  content: 'block+',
  defining: true,
  addAttributes() {
    return {
      sectionKey: { default: null },
      sectionId: { default: null },
      title: { default: '' }
    }
  },
  parseHTML() {
    return [{ tag: 'section[data-clp-section]' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['section', mergeAttributes(HTMLAttributes, {
      'data-clp-section': HTMLAttributes.sectionKey
    }), 0]
  }
})

const SemanticTableCell = TableCell.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      tableId: { default: null },
      rowId: { default: null },
      columnId: { default: null },
      contentPath: { default: null }
    }
  }
})

const editor = new Editor({
  element: document.getElementById('editor'),
  extensions: [
    StarterKit,
    Underline,
    Highlight.configure({ multicolor: true }),
    Placeholder.configure({
      placeholder: 'Write your Course Learning Plan...'
    }),
    TextAlign.configure({ types: ['heading', 'paragraph'] }),
    Link.configure({ openOnClick: false }),
    Image,
    Table.configure({ resizable: true, allowTableNodeSelection: true }),
    TableRow,
    TableHeader,
    SemanticTableCell,
    ClpSection
  ],
  content: {
    type: 'doc',
    content: []
  },
  onSelectionUpdate: ({ editor }) => {
    updateRewriteButton(editor)
  },
  onUpdate: debounce(() => {
    showStatus('Unsaved changes')
  }, 500)
})

async function loadDocument() {
  const response = await fetch(`/api/clp-documents/${documentId}`, {
    credentials: 'same-origin'
  })

  const data = await response.json()
  if (!data.ok) throw new Error(data.error || 'Failed to load document.')

  titleInput.value = data.document.title || ''
  editorJson = data.document.editor_json || {}

  const cachedProjection = editorJson.editor_state?.doc || editorJson.editor_state?.tiptap_json
  const tiptapJson = data.document.tiptap_json || cachedProjection || {
    type: 'doc',
    content: []
  }

  setEditorContentSafely(tiptapJson, 'Document')
  showStatus(`Loaded ${countTiptapNodes(tiptapJson)} nodes, ${flattenTiptapText(tiptapJson).length} text runs`)
}

async function saveDocument() {
  if (saving) return

  const tiptapJson = editor.getJSON()

  if (!tiptapJson || tiptapJson.type !== 'doc') {
    showError('Invalid editor content. Save canceled.')
    return
  }

  saving = true
  showStatus('Saving...')

  try {
    editorJson.editor_state = editorJson.editor_state || {}
    editorJson.editor_state.schema_version = 'clp_tiptap_v1'
    editorJson.editor_state.doc = tiptapJson

    const response = await fetch(`/api/clp-documents/${documentId}`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': window.CSRF_TOKEN
      },
      body: JSON.stringify({
        title: titleInput.value,
        editor_json: editorJson,
        tiptap_json: tiptapJson
      })
    })

    const data = await response.json()
    if (!data.ok) throw new Error(data.error || 'Save failed.')

    showStatus(`Saved ${new Date().toLocaleTimeString()}`)
  } catch (error) {
    showError(error.message)
    showStatus('Save failed')
  } finally {
    saving = false
  }
}

async function saveVersion() {
  await saveDocument()
  const response = await fetch(`/api/clp-documents/${documentId}/versions`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN
    },
    body: JSON.stringify({ change_summary: 'Manual feasibility snapshot' })
  })
  const data = await response.json()
  if (!data.ok) throw new Error(data.error || 'Version save failed.')
  showStatus(`Version ${data.version_number} saved`)
}

async function showVersions() {
  const response = await fetch(`/api/clp-documents/${documentId}/versions`, { credentials: 'same-origin' })
  const data = await response.json()
  if (!data.ok) throw new Error(data.error || 'Version load failed.')
  showDebug(data)
}

async function importDocx(file) {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(`/api/clp-documents/${documentId}/import-docx`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'X-CSRFToken': window.CSRF_TOKEN
    },
    body: form
  })
  const data = await response.json()
  if (!data.ok) throw new Error(data.error || 'DOCX import failed.')
  editorJson = data.document.editor_json || {}
  const importedDoc = data.document.tiptap_json || editorJson.editor_state?.doc || { type: 'doc', content: [] }
  setEditorContentSafely(importedDoc, 'DOCX')
  const visibleWarnings = cleanImportWarnings(data.warnings)
  if (visibleWarnings.length) {
    showDebug({
      import_warnings: visibleWarnings.slice(0, 20),
      warning_count: visibleWarnings.length,
      suppressed_warning_count: (data.warnings || []).length - visibleWarnings.length
    })
    showStatus(`DOCX imported with warnings: ${countTiptapNodes(importedDoc)} nodes, ${flattenTiptapText(importedDoc).length} text runs`)
  } else {
    debugOutput.classList.add('hidden')
    showStatus(`DOCX imported: ${countTiptapNodes(importedDoc)} nodes, ${flattenTiptapText(importedDoc).length} text runs`)
  }
}

function downloadFromPost(url) {
  const form = document.createElement('form')
  form.method = 'POST'
  form.action = url
  const csrf = document.createElement('input')
  csrf.type = 'hidden'
  csrf.name = 'csrf_token'
  csrf.value = window.CSRF_TOKEN
  form.appendChild(csrf)
  document.body.appendChild(form)
  form.submit()
  form.remove()
}

async function generateSection(sectionKey, instruction) {
  await saveDocument()
  const response = await fetch('/api/ai/generate-section', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN
    },
    body: JSON.stringify({
      document_id: Number(documentId),
      section_key: sectionKey,
      instruction
    })
  })
  const data = await response.json()
  if (!data.ok) throw new Error(data.error || 'Section generation failed.')
  showDebug(data)
  await loadDocument()
}

async function alignTable() {
  const rowsText = prompt('Paste CLO-PLO rows as JSON array, or leave blank for a sample row.')
  const rows = rowsText
    ? JSON.parse(rowsText)
    : [{ source_clo_id: 'CLO 1', target_plo_id: 'PLO 1', mapping_value: '', statement: 'Demonstrate course competency.' }]
  const response = await fetch('/api/ai/align-table', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN
    },
    body: JSON.stringify({
      document_id: Number(documentId),
      rows,
      allowed_values: ['I', 'R', 'M', 'A', '']
    })
  })
  const data = await response.json()
  if (!data.ok) throw new Error(data.error || 'Alignment failed.')
  showDebug(data)
}

function getSelectedText() {
  const { from, to } = editor.state.selection
  return editor.state.doc.textBetween(from, to, '\n').trim()
}

function updateRewriteButton(editorInstance) {
  const selectedText = getSelectedText()

  if (!selectedText) {
    rewriteButton.classList.add('hidden')
    return
  }

  const { from } = editorInstance.state.selection
  const coords = editorInstance.view.coordsAtPos(from)

  rewriteButton.style.left = `${coords.left}px`
  rewriteButton.style.top = `${Math.max(72, coords.top - 46)}px`
  rewriteButton.classList.remove('hidden')
}

async function rewriteSelection() {
  const { from, to } = editor.state.selection
  const selectedText = getSelectedText()

  if (!selectedText) return

  rewriteButton.disabled = true
  rewriteButton.textContent = 'Rewriting...'

  try {
    const response = await fetch('/api/ai/rewrite', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': window.CSRF_TOKEN
      },
      body: JSON.stringify({
        document_id: Number(documentId),
        text: selectedText
      })
    })

    const data = await response.json()
    if (!data.ok) throw new Error(data.error || 'Rewrite failed.')

    if (!data.text || typeof data.text !== 'string') {
      throw new Error('AI returned an invalid response.')
    }

    editor.chain().focus().insertContentAt({ from, to }, data.text).run()
    showStatus('AI rewrite inserted. Save to persist changes.')
  } catch (error) {
    showError(error.message)
  } finally {
    rewriteButton.disabled = false
    rewriteButton.textContent = 'Rewrite'
    rewriteButton.classList.add('hidden')
  }
}

saveButton.addEventListener('click', saveDocument)
saveVersionButton.addEventListener('click', () => saveVersion().catch(error => showError(error.message)))
showVersionsButton.addEventListener('click', () => showVersions().catch(error => showError(error.message)))
importDocxButton.addEventListener('click', () => importDocxFile.click())
importDocxFile.addEventListener('change', () => {
  const file = importDocxFile.files?.[0]
  if (file) importDocx(file).catch(error => showError(error.message))
})
exportDocxButton.addEventListener('click', () => downloadFromPost(`/api/clp-documents/${documentId}/export-docx`))
exportPdfButton.addEventListener('click', () => downloadFromPost(`/api/clp-documents/${documentId}/export-pdf`))
generateWeeklyButton.addEventListener('click', () => generateSection('weekly_outline', 'Generate a practical 18-week course outline using the current course context.').catch(error => showError(error.message)))
generateSectionButton.addEventListener('click', () => {
  const sectionKey = prompt('Section key:', 'custom_section')
  const instruction = prompt('Instruction:', 'Generate a concise academic section for this CLP.')
  if (sectionKey && instruction) generateSection(sectionKey, instruction).catch(error => showError(error.message))
})
alignTableButton.addEventListener('click', () => alignTable().catch(error => showError(error.message)))
rewriteButton.addEventListener('click', rewriteSelection)

loadDocument().catch(error => {
  showError(error.message)
  showStatus('Load failed')
})
