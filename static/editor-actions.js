/* Make the non-destructive return action visually separate from discard/apply. */
(() => {
  const actions = document.querySelector('.editor-head > div:last-child');
  const returnButton = document.querySelector('#returnReader');
  const discardButton = document.querySelector('#discard');
  const applyButton = document.querySelector('#apply');
  if (!actions || !returnButton || !discardButton || !applyButton) return;

  actions.classList.add('editor-actions');
  const returnRow = document.createElement('div');
  returnRow.className = 'editor-return-action';
  const commitRow = document.createElement('div');
  commitRow.className = 'editor-commit-actions';
  returnButton.textContent = '回到閱讀';
  returnRow.append(returnButton);
  commitRow.append(discardButton, applyButton);
  actions.replaceChildren(returnRow, commitRow);
})();
