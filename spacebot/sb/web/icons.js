/* Inline SVG icons. Every one is a bare viewBox with no width/height — app.css sizes them
   (`button svg { width:16px }`), so an icon is never larger than its button. */

export const LOGO =
  '<svg viewBox="0 0 32 32" fill="none" aria-hidden="true">' +
  '<ellipse cx="16" cy="16" rx="14.2" ry="6.4" transform="rotate(-32 16 16)"' +
  ' stroke="currentColor" stroke-width="1.9" opacity=".5"/>' +
  '<circle cx="16" cy="16" r="5.3" fill="currentColor"/>' +
  '<circle cx="28" cy="8.5" r="2.4" fill="currentColor"/></svg>';

/** The gradient tile + mark, used in the sidebar, hero and login. */
export const logo = (size = '') => `<div class="logo${size ? ' logo--' + size : ''}">${LOGO}</div>`;

const s = (body, w = 2) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${w}"` +
  ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

export const ICON = {
  plus:  s('<path d="M12 5v14M5 12h14"/>'),
  send:  s('<path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/>', 2.2),
  stop:  '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
         '<rect x="7" y="7" width="10" height="10" rx="2.5"/></svg>',
  trash: s('<path d="M4 7h16M10 11v6M14 11v6M5 7l1 13a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1l1-13M9 7V4h6v3"/>', 1.7),
  pencil: s('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>', 1.7),
  chat:  s('<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.9-.9L3 20l1.3-3.9A8.4 8.4 0 0 1 3 11.5a8.5 8.5 0 0 1 9-8.4 8.4 8.4 0 0 1 9 8.4z"/>', 1.7),
  book:  s('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>', 1.7),
  gear:  s('<circle cx="12" cy="12" r="3.1"/><path d="M19.1 14.6a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.1a2 2 0 1 1-4 0v-.2a1.6 1.6 0 0 0-1-1.4 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.6 1.6 0 0 0 1.4-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.6 1.6 0 0 0 1 1.4 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1h.1a2 2 0 1 1 0 4h-.2a1.6 1.6 0 0 0-1.4 1z"/>', 1.6),
  sun:   s('<circle cx="12" cy="12" r="4.1"/><path d="M12 1.8v2.1M12 20.1v2.1M4.3 4.3l1.5 1.5M18.2 18.2l1.5 1.5M1.8 12h2.1M20.1 12h2.1M4.3 19.7l1.5-1.5M18.2 5.8l1.5-1.5"/>', 1.9),
  moon:  s('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>', 1.9),
  menu:  s('<path d="M4 7h16M4 12h16M4 17h16"/>'),
  out:   s('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>', 1.7),
  copy:  s('<rect x="9" y="9" width="12" height="12" rx="2.2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>', 1.7),
  redo:  s('<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1M20.5 4v5h-5"/>', 1.8),
  spark: s('<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>', 1.8),
  up:    s('<path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1zM7 11l4.2-8a2 2 0 0 1 2.8 2.4L13 9h5.6a2 2 0 0 1 2 2.5l-1.7 7A2 2 0 0 1 17 20H7"/>', 1.6),
  down:  s('<path d="M17 13V4h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1zM17 13l-4.2 8a2 2 0 0 1-2.8-2.4L11 15H5.4a2 2 0 0 1-2-2.5l1.7-7A2 2 0 0 1 7 4h10"/>', 1.6),
  check: s('<path d="M20 6 9 17l-5-5"/>', 2.2),
};
