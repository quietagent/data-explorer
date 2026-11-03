'use strict';

/* The colour theme. This file is loaded first in <head>, before the style sheet, so that the page is
   never drawn in the wrong colours and then redrawn. It is a file and not an inline script because
   the page loads everything from /static (the Content-Security-Policy allows nothing else).

   The choice is "system" (follow the operating system; the default), "light" or "dark". It is kept in
   this browser only. <html data-theme="light|dark"> is what styles.css reads. */

(() => {
  const KEY = 'data-explorer.theme';
  const CHOICES = ['system', 'light', 'dark'];
  const root = document.documentElement;
  const dark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const listeners = [];

  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return CHOICES.includes(value) ? value : 'system';
    } catch (e) {
      return 'system';                      // storage may be blocked: follow the system
    }
  }

  const resolve = (choice) => (choice === 'system' ? (dark && dark.matches ? 'dark' : 'light') : choice);

  let choice = stored();

  function apply(tell) {
    root.setAttribute('data-theme', resolve(choice));
    root.setAttribute('data-theme-choice', choice);
    if (tell) for (const fn of listeners) fn(theme.resolved(), choice);
  }

  const theme = {
    choices: CHOICES,
    /** What was chosen: "system", "light" or "dark". */
    choice: () => choice,
    /** What is shown: "light" or "dark". */
    resolved: () => root.getAttribute('data-theme'),
    set(value) {
      choice = CHOICES.includes(value) ? value : 'system';
      try {
        localStorage.setItem(KEY, choice);
      } catch (e) { /* not remembered, but applied */ }
      apply(true);
    },
    /** fn(resolved, choice) is called whenever either changes: the switch, the system, another tab. */
    onChange(fn) { listeners.push(fn); },
  };

  apply(false);
  if (dark) {
    const follow = () => { if (choice === 'system') apply(true); };
    if (dark.addEventListener) dark.addEventListener('change', follow);
    else if (dark.addListener) dark.addListener(follow);
  }
  window.addEventListener('storage', (e) => {      // the theme was changed in another tab of this browser
    if (e.key !== KEY) return;
    choice = stored();
    apply(true);
  });

  window.dataExplorerTheme = theme;
})();
