<script lang="ts">
  import { onMount } from 'svelte';

  export let initialName = '';
  export let editing = false;
  export let onSave: (name: string) => string | null;
  export let onClose: () => void;

  let dialog: HTMLDialogElement;
  let input: HTMLInputElement;
  let name = initialName;
  let error = '';

  onMount(() => {
    dialog.showModal();
    input.focus();
    if (editing) input.select();
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    error = onSave(name) || '';
    if (error) input.focus();
  }

  function trapFocus(event: KeyboardEvent) {
    if (event.key !== 'Tab') return;
    const controls = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)'));
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
</script>

<dialog bind:this={dialog} aria-labelledby="folder-dialog-title" onkeydown={trapFocus} oncancel={(event) => { event.preventDefault(); onClose(); }} onclick={(event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose(); } }}>
  <form onsubmit={submit}>
    <header><h2 id="folder-dialog-title">{editing ? 'Rename folder' : 'New folder'}</h2><button class="close" type="button" aria-label="Close folder dialog" onclick={onClose}>×</button></header>
    <label for="folder-name">Folder name</label>
    <input bind:this={input} id="folder-name" bind:value={name} maxlength="80" autocomplete="off" aria-invalid={Boolean(error)} aria-describedby={error ? 'folder-error' : undefined} oninput={() => error = ''} />
    {#if error}<p class="folder-error" id="folder-error" role="alert">{error}</p>{/if}
    <footer><button class="secondary compact" type="button" onclick={onClose}>Cancel</button><button class="primary compact" type="submit">{editing ? 'Save name' : 'Create folder'}</button></footer>
  </form>
</dialog>

<style>
  dialog { width: min(440px, calc(100vw - 32px)); max-height: calc(100dvh - 40px); padding: 28px; margin: auto; color: var(--text); background: var(--surface); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 18px 70px #0003; animation: open-folder 450ms cubic-bezier(.22,.61,.36,1); }
  dialog::backdrop { background: #07111c80; backdrop-filter: blur(3px); animation: fade-backdrop 450ms ease; }
  header { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 28px; }
  h2 { font-size: 24px; margin: 0; letter-spacing: -.025em; }
  .close { position: static; width: 34px; height: 34px; padding: 0; flex: 0 0 auto; font-size: 24px; border-radius: 8px; }
  label { display: block; color: var(--muted); font-size: 13px; font-weight: 600; margin-bottom: 8px; }
  input { width: 100%; padding: 12px 14px; color: var(--text); background: var(--surface-soft); border: 1px solid var(--line); border-radius: 8px; font: inherit; }
  input[aria-invalid="true"] { border-color: var(--orange); }
  .folder-error { color: var(--text); font-size: 13px; line-height: 1.5; margin: 10px 0 0; }
  footer { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; margin-top: 28px; }
  @keyframes open-folder { from { opacity: 0; translate: 0 8px; } to { opacity: 1; translate: 0 0; } }
  @keyframes fade-backdrop { from { opacity: 0; } to { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { dialog, dialog::backdrop { animation: none; } }
</style>
