document.querySelectorAll('[data-sidebar-toggle]').forEach((button) => {
  button.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
});

document.querySelectorAll('.flash').forEach((item) => {
  window.setTimeout(() => item.classList.add('fade'), 5000);
});
