(function () {
  // 这些标记不是真正的编程语言，按纯文本处理，避免高亮器误判
  const TEXT_LANGS = new Set(['text', 'txt', 'plain', 'plaintext', 'mermaid', 'diagram', 'output', 'log']);

  function prepare(block) {
    const cls = Array.from(block.classList).find(c => c.startsWith('language-'));
    if (!cls) return; // 未标注语言时交给自动识别

    let lang = cls.slice('language-'.length).toLowerCase();
    if (TEXT_LANGS.has(lang)) {
      lang = 'plaintext';
    } else if (lang !== 'plaintext' && !window.hljs.getLanguage(lang)) {
      lang = 'plaintext';
    }
    block.classList.remove(cls);
    block.classList.add('language-' + lang);
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!window.hljs) return;
    document.querySelectorAll('pre code').forEach(block => {
      prepare(block);
      window.hljs.highlightElement(block);
    });
  });
})();
