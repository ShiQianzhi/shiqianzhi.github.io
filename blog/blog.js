
const menuBtn = document.getElementById('menuBtn');
const navLinks = document.getElementById('navLinks');
if (menuBtn && navLinks) {
  menuBtn.addEventListener('click', () => {
    const open = navLinks.classList.toggle('open');
    menuBtn.classList.toggle('open', open);
    menuBtn.setAttribute('aria-expanded', String(open));
  });
  navLinks.querySelectorAll('a').forEach(a => a.addEventListener('click', () => {
    navLinks.classList.remove('open');
    menuBtn.classList.remove('open');
    menuBtn.setAttribute('aria-expanded', 'false');
  }));
}

const treeQ = document.getElementById('treeQ');
const sideToggle = document.getElementById('sideToggle');
const blogSide = document.querySelector('.blog-side');
const folderOpenState = new Map();
document.querySelectorAll('.tree-folder').forEach(folder => folderOpenState.set(folder, folder.open));
if (sideToggle && blogSide) {
  sideToggle.addEventListener('click', () => {
    const open = blogSide.classList.toggle('open');
    sideToggle.setAttribute('aria-expanded', String(open));
    sideToggle.textContent = open ? '收起' : '目录';
  });
}
if (treeQ) {
  treeQ.addEventListener('input', () => {
    const kw = treeQ.value.trim().toLowerCase();
    document.querySelectorAll('.tree-link').forEach(a => {
      const hit = !kw || a.textContent.toLowerCase().includes(kw) || (a.title || '').toLowerCase().includes(kw);
      a.classList.toggle('hidden', !hit);
    });
    const folders = [...document.querySelectorAll('.tree-folder')].reverse();
    folders.forEach(folder => {
      const visible = folder.querySelectorAll('.tree-link:not(.hidden)').length > 0;
      folder.classList.toggle('hidden', !visible);
      folder.open = kw ? visible : (folderOpenState.get(folder) || false);
    });
    document.querySelectorAll('.tree-label').forEach(label => {
      const list = label.nextElementSibling;
      if (list && list.classList.contains('tree-link')) {
        label.classList.toggle('hidden', !folderVisibleAfter(label));
      }
    });
    function folderVisibleAfter(label) {
      let node = label.nextElementSibling;
      while (node && node.classList.contains('tree-link')) {
        if (!node.classList.contains('hidden')) return true;
        node = node.nextElementSibling;
      }
      return false;
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if (window.renderMathInElement) {
    renderMathInElement(document.querySelector('.prose') || document.body, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true }
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
      throwOnError: false
    });
  }
});
